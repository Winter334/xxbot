"""Discord 锻造私有面板。"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Protocol

import discord
from sqlalchemy.orm import Session, sessionmaker

from application.character.panel_query_service import CharacterPanelQueryService, CharacterPanelQueryServiceError
from application.character.profile_panel_query_service import SkillPanelSkillSlotSnapshot
from application.equipment.equipment_service import (
    ArtifactNurtureApplicationResult,
    EquipmentDismantleApplicationResult,
    EquipmentEnhancementApplicationResult,
    EquipmentItemSnapshot,
    EquipmentReforgeApplicationResult,
    EquipmentResourceLedgerEntry,
    EquipmentService,
    EquipmentServiceError,
    EquipmentUnequipApplicationResult,
    EquipmentWashApplicationResult,
)
from application.equipment.forge_query_service import (
    ForgeCardSnapshot,
    ForgeFilterId,
    ForgeOperationCostSnapshot,
    ForgeOperationId,
    ForgeOperationPreviewSnapshot,
    ForgePanelQueryService,
    ForgePanelQueryServiceError,
    ForgePanelSnapshot,
    ForgeTargetKind,
    ForgeTargetSnapshot,
)
from infrastructure.config.static import get_static_config
from infrastructure.db.session import session_scope
from infrastructure.discord.character_panel import (
    DiscordInteractionVisibilityResponder,
    PanelMessagePayload,
    PanelVisibility,
)

_PANEL_TIMEOUT_SECONDS = 20 * 60
_MAX_SELECT_OPTIONS = 25
_STAT_NAME_BY_ID = {
    "max_hp": "气血",
    "attack_power": "攻力",
    "guard_power": "护体",
    "speed": "迅捷",
    "crit_rate_permille": "暴击",
    "crit_damage_bonus_permille": "暴伤",
    "hit_rate_permille": "命中",
    "dodge_rate_permille": "闪避",
    "damage_bonus_permille": "增伤",
    "damage_reduction_permille": "减伤",
    "counter_rate_permille": "反击",
    "control_bonus_permille": "控势",
    "control_resist_permille": "定心",
    "healing_power_permille": "疗愈",
    "shield_power_permille": "护盾",
    "penetration_permille": "穿透",
}
_OPERATION_NAME_BY_ID = {
    ForgeOperationId.ENHANCE: "强化",
    ForgeOperationId.WASH: "洗炼",
    ForgeOperationId.REFORGE: "重铸",
    ForgeOperationId.NURTURE: "法宝培养",
    ForgeOperationId.DISMANTLE: "分解",
    ForgeOperationId.UNEQUIP: "卸下装备",
}
_FILTER_LABEL_BY_ID = {
    ForgeFilterId.ALL: "全部",
    ForgeFilterId.WEAPON: "武器",
    ForgeFilterId.ARMOR: "护甲",
    ForgeFilterId.ACCESSORY: "饰品",
    ForgeFilterId.ARTIFACT: "法宝",
    ForgeFilterId.SKILL: "功法",
}
_SKILL_ACTION_DISABLED_TEXT = "当前功法仅支持查看详情，培养写操作尚未开放。"


class ForgePanelServiceBundle(Protocol):
    """锻造面板所需的最小服务集合。"""

    character_panel_query_service: CharacterPanelQueryService
    forge_panel_query_service: ForgePanelQueryService
    equipment_service: EquipmentService


class ForgePendingAction(StrEnum):
    """锻造面板待确认动作。"""

    NONE = "none"
    WASH = "wash"
    REFORGE = "reforge"
    DISMANTLE = "dismantle"


@dataclass(frozen=True, slots=True)
class ForgeActionNote:
    """锻造动作反馈。"""

    title: str
    lines: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ForgePanelState:
    """锻造面板显式状态。"""

    filter_id: ForgeFilterId = ForgeFilterId.ALL
    page: int = 1
    selected_target_id: str | None = None
    pending_action: ForgePendingAction = ForgePendingAction.NONE
    selected_locked_affix_positions: tuple[int, ...] = ()
    action_note: ForgeActionNote | None = None


class ForgePanelPresenter:
    """负责把锻造快照投影为 Discord Embed。"""

    @classmethod
    def build_embed(
        cls,
        *,
        snapshot: ForgePanelSnapshot,
        state: ForgePanelState,
    ) -> discord.Embed:
        embed = discord.Embed(
            title=f"{snapshot.character_name}｜锻造",
            description="仅操作者可见",
            color=discord.Color.dark_gold(),
        )
        embed.add_field(name="📌 目标状态", value=cls._build_target_status_block(snapshot=snapshot), inline=False)
        embed.add_field(name="✨ 目标卡", value=cls._build_target_detail_block(snapshot=snapshot), inline=False)
        embed.add_field(name="💰 本次消耗", value=cls._build_operation_cost_block(snapshot=snapshot), inline=False)
        embed.add_field(name="🧪 结果预览", value=cls._build_operation_preview_block(snapshot=snapshot), inline=False)
        if state.action_note is not None and state.action_note.lines:
            embed.add_field(name=state.action_note.title, value="\n".join(state.action_note.lines), inline=False)
        embed.set_footer(text="锻造：目标、消耗、预览")
        return embed

    @staticmethod
    def _build_operation_cost_block(*, snapshot: ForgePanelSnapshot) -> str:
        if not snapshot.operation_costs:
            return "当前操作无需消耗资源。"
        lines = [
            f"{entry.resource_name} {entry.required_quantity} / 持有 {entry.owned_quantity}"
            for entry in snapshot.operation_costs
        ]
        return "```\n" + "\n".join(lines) + "\n```"

    @classmethod
    def _build_target_status_block(cls, *, snapshot: ForgePanelSnapshot) -> str:
        selected_target = snapshot.selected_target
        selected_label = "无"
        if selected_target is not None:
            location_label = "已装" if selected_target.equipped else "背包"
            selected_label = f"{selected_target.display_name}｜{location_label}"
        current_page_count = len(snapshot.targets)
        helper_line = "当前页暂无可选目标。" if current_page_count <= 0 else f"当前页可选：{current_page_count} 项，请使用下拉框切换目标。"
        current_operation = snapshot.current_operation_name or "无"
        return "\n".join(
            (
                f"当前筛选：{_FILTER_LABEL_BY_ID[snapshot.filter_id]}",
                f"页码：第 {snapshot.page}/{snapshot.total_pages} 页｜共 {snapshot.total_items} 项",
                f"当前目标：{selected_label}",
                f"当前操作：{current_operation}",
                helper_line,
            )
        )

    @classmethod
    def _build_target_detail_block(cls, *, snapshot: ForgePanelSnapshot) -> str:
        if snapshot.selected_target_card is None:
            return "当前没有可选锻造目标。"
        return cls._format_card(snapshot.selected_target_card)

    @classmethod
    def _build_operation_preview_block(cls, *, snapshot: ForgePanelSnapshot) -> str:
        preview = snapshot.operation_preview
        if preview is None:
            return "当前没有可展示的结果预览。"
        lines = [preview.title, "```"]
        lines.extend(preview.lines)
        lines.append("```")
        return cls._truncate_lines(tuple(lines), limit=1000)

    @classmethod
    def _format_card(cls, card: ForgeCardSnapshot) -> str:
        lines = [card.name, f"```\n{card.badge_line}"]
        if card.growth_line:
            lines.append(card.growth_line)
        lines.extend(card.stat_lines[:4])
        lines.append("```")
        if card.keyword_lines:
            lines.append("词条：")
            lines.extend(card.keyword_lines[:3])
        else:
            lines.append("词条：无")
        return cls._truncate_lines(tuple(lines), limit=1000)

    @staticmethod
    def _format_equipment_head(item: EquipmentItemSnapshot) -> str:
        nurture = f"｜祭炼 {item.artifact_nurture_level}" if item.is_artifact else ""
        return f"[{item.quality_name}·{item.rank_name}] {item.display_name}｜强化 +{item.enhancement_level}{nurture}"

    @classmethod
    def _format_primary_stat_lines(cls, item: EquipmentItemSnapshot, *, limit: int) -> str:
        stats = item.resolved_stats if item.resolved_stats else item.base_attributes
        if not stats:
            return "无"
        parts: list[str] = []
        for stat in stats[:limit]:
            parts.append(f"{cls._format_stat_name(stat.stat_id)} {cls._format_stat_value(stat.stat_id, stat.value)}")
        return "｜".join(parts)

    @classmethod
    def _format_affix_line(cls, affix) -> str:
        head = f"{affix.affix_name}({affix.tier_name})" if affix.tier_name else affix.affix_name
        if affix.affix_kind == "special_effect" or affix.special_effect is not None or not affix.stat_id.strip():
            return head
        return f"{head} {cls._format_stat_name(affix.stat_id)} {cls._format_stat_value(affix.stat_id, affix.value)}"

    @classmethod
    def _format_affix_detail_lines(cls, affixes) -> tuple[str, ...]:
        lines: list[str] = []
        for index, affix in enumerate(affixes[:6], start=1):
            position = affix.position or index
            header_parts = [f"{position}. {affix.affix_name}"]
            if affix.tier_name:
                header_parts.append(affix.tier_name)
            header_parts.append("特殊词条" if affix.affix_kind == "special_effect" or affix.special_effect is not None else "数值词条")
            effect_value = cls._format_affix_effect_value(affix)
            if effect_value:
                header_parts.append(effect_value)
            scope_tags: list[str] = []
            if getattr(affix, "is_pve_specialized", False):
                scope_tags.append("PVE专精")
            if getattr(affix, "is_pvp_specialized", False):
                scope_tags.append("PVP专精")
            if scope_tags:
                header_parts.append(" / ".join(scope_tags))
            description = cls._format_affix_description(affix)
            if description:
                header_parts.append(f"说明：{description}")
            lines.append("｜".join(header_parts))
        return tuple(lines)

    @classmethod
    def _format_affix_effect_value(cls, affix) -> str:
        if affix.affix_kind == "special_effect" or affix.special_effect is not None or not affix.stat_id.strip():
            if affix.special_effect is None:
                return "特殊效果"
            return f"触发：{cls._format_trigger_event(affix.special_effect.trigger_event)}"
        return f"{cls._format_stat_name(affix.stat_id)} {cls._format_stat_value(affix.stat_id, affix.value)}"

    @classmethod
    def _format_affix_description(cls, affix) -> str:
        static_config = get_static_config()
        affix_definition = static_config.equipment.get_affix(affix.affix_id)
        parts: list[str] = []
        if affix_definition is not None and affix_definition.summary:
            parts.append(str(affix_definition.summary))
        if affix.special_effect is not None:
            special_effect_text = cls._format_special_effect_summary(affix.special_effect)
            if special_effect_text:
                parts.append(special_effect_text)
        if not parts:
            return ""
        deduplicated_parts: list[str] = []
        for part in parts:
            if not part or part in deduplicated_parts:
                continue
            deduplicated_parts.append(part)
        return "；".join(deduplicated_parts)

    @classmethod
    def _format_special_effect_summary(cls, special_effect) -> str:
        static_effect = get_static_config().equipment.get_special_effect(special_effect.effect_id)
        parts: list[str] = []
        if static_effect is not None and static_effect.summary:
            parts.append(str(static_effect.summary))
        parts.append(f"触发：{cls._format_trigger_event(special_effect.trigger_event)}")
        payload_parts = cls._format_special_effect_payload_parts(special_effect.payload)
        if payload_parts:
            parts.append("参数：" + "、".join(payload_parts))
        return "；".join(part for part in parts if part)

    @classmethod
    def _format_special_effect_payload_parts(cls, payload) -> tuple[str, ...]:
        if not isinstance(payload, dict) or not payload:
            return ()
        ordered_keys = (
            ("trigger_rate_permille", "触发率"),
            ("suppression_permille", "压制幅度"),
            ("dot_ratio_permille", "持续伤害系数"),
            ("guard_ratio_permille", "护盾系数"),
            ("damage_ratio_permille", "伤害转化系数"),
            ("attack_ratio_permille", "攻力系数"),
            ("hp_threshold_permille", "气血阈值"),
            ("duration_rounds", "持续回合"),
            ("cooldown_rounds", "冷却回合"),
            ("max_stacks", "最多层数"),
            ("max_triggers_per_round", "每回合触发上限"),
            ("max_triggers_per_battle", "每场触发上限"),
            ("requires_damage_resolved", "需造成伤害"),
            ("require_empty_shield", "需当前无护盾"),
        )
        parts: list[str] = []
        consumed_keys: set[str] = set()
        for key, label in ordered_keys:
            if key not in payload:
                continue
            consumed_keys.add(key)
            value = payload.get(key)
            formatted_value = cls._format_effect_payload_value(key=key, value=value)
            parts.append(f"{label} {formatted_value}" if formatted_value else label)
        for key in sorted(str(raw_key) for raw_key in payload.keys() if str(raw_key) not in consumed_keys):
            parts.append(f"{key}={payload[key]}")
        return tuple(parts)

    @classmethod
    def _format_effect_payload_value(cls, *, key: str, value) -> str:
        if isinstance(value, bool):
            return "是" if value else "否"
        if isinstance(value, int):
            if key.endswith("_permille"):
                return f"{value / 10:.1f}%"
            if key.endswith("_rounds"):
                return f"{value} 回合"
            if key == "max_stacks":
                return f"{value} 层"
            if key.startswith("max_triggers_per_"):
                return f"{value} 次"
            return str(value)
        return str(value)

    @staticmethod
    def _format_trigger_event(trigger_event: str) -> str:
        return {
            "battle_start": "战斗开始时",
            "round_start": "回合开始时",
            "turn_start": "行动开始时",
            "before_action": "出手前",
            "after_action": "出手后",
            "damage_resolved": "造成伤害后",
            "damage_taken": "受到伤害后",
            "turn_end": "行动结束时",
            "round_end": "回合结束时",
            "battle_end": "战斗结束时",
        }.get(trigger_event, trigger_event)

    @staticmethod
    def _format_stat_name(stat_id: str) -> str:
        return _STAT_NAME_BY_ID.get(stat_id, stat_id)

    @staticmethod
    def _format_stat_value(stat_id: str, value: int) -> str:
        if stat_id.endswith("_permille"):
            return f"{value / 10:.1f}%"
        return str(value)

    @classmethod
    def _build_locked_affix_summary(
        cls,
        *,
        item: EquipmentItemSnapshot,
        locked_affix_positions: tuple[int, ...],
    ) -> str:
        if not locked_affix_positions:
            return "无"
        affix_by_position = {
            (affix.position or index): affix for index, affix in enumerate(item.affixes, start=1)
        }
        parts: list[str] = []
        for position in sorted(set(locked_affix_positions)):
            affix = affix_by_position.get(position)
            if affix is None:
                parts.append(f"{position}. 未知词条")
                continue
            parts.append(f"{position}. {cls._format_affix_line(affix)}")
        return "｜".join(parts)

    @staticmethod
    def _truncate_lines(lines: tuple[str, ...], *, limit: int) -> str:
        if not lines:
            return "无"
        result: list[str] = []
        current_length = 0
        for index, line in enumerate(lines):
            projected_length = current_length + len(line) + (1 if result else 0)
            if projected_length > limit:
                remaining = len(lines) - index
                if remaining > 0:
                    result.append(f"…其余 {remaining} 项请使用下拉框查看")
                break
            result.append(line)
            current_length = projected_length
        return "\n".join(result)


class ForgeFilterSelect(discord.ui.Select):
    """锻造筛选选择器。"""

    def __init__(self, *, selected_filter_id: ForgeFilterId) -> None:
        options = [
            discord.SelectOption(
                label=_FILTER_LABEL_BY_ID[filter_id],
                value=filter_id.value,
                default=filter_id is selected_filter_id,
            )
            for filter_id in (
                ForgeFilterId.ALL,
                ForgeFilterId.WEAPON,
                ForgeFilterId.ARMOR,
                ForgeFilterId.ACCESSORY,
                ForgeFilterId.ARTIFACT,
                ForgeFilterId.SKILL,
            )
        ]
        super().__init__(
            placeholder="选择锻造筛选",
            min_values=1,
            max_values=1,
            options=options,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, ForgePanelView):
            await interaction.response.defer()
            return
        await view.controller.change_filter(
            interaction,
            character_id=view.character_id,
            owner_user_id=view.owner_user_id,
            current_state=view.state,
            filter_id=ForgeFilterId(self.values[0]),
        )


class ForgeTargetSelect(discord.ui.Select):
    """锻造目标选择器。"""

    def __init__(self, *, snapshot: ForgePanelSnapshot, state: ForgePanelState) -> None:
        options = [
            discord.SelectOption(
                label=target.display_name[:100],
                value=target.target_id,
                description=_build_target_option_description(target=target)[:100],
                default=target.target_id == state.selected_target_id,
            )
            for target in snapshot.targets[:_MAX_SELECT_OPTIONS]
        ]
        super().__init__(
            placeholder="选择当前页培养目标",
            min_values=1,
            max_values=1,
            options=options,
            row=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, ForgePanelView):
            await interaction.response.defer()
            return
        await view.controller.select_target(
            interaction,
            character_id=view.character_id,
            owner_user_id=view.owner_user_id,
            current_state=view.state,
            target_id=self.values[0],
        )


class ForgeWashAffixSelect(discord.ui.Select):
    """洗炼锁定词条选择器。"""

    def __init__(
        self,
        *,
        item: EquipmentItemSnapshot,
        selected_locked_positions: tuple[int, ...],
    ) -> None:
        options: list[discord.SelectOption] = []
        for index, affix in enumerate(item.affixes[:_MAX_SELECT_OPTIONS], start=1):
            position = affix.position or index
            options.append(
                discord.SelectOption(
                    label=f"{position}号词条",
                    value=str(position),
                    description=ForgePanelPresenter._format_affix_line(affix)[:100],
                    default=position in selected_locked_positions,
                )
            )
        super().__init__(
            placeholder="选择要保留的词条，可留空",
            min_values=0,
            max_values=len(options),
            options=options,
            row=4,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, ForgePanelView):
            await interaction.response.defer()
            return
        await view.controller.update_locked_affixes(
            interaction,
            character_id=view.character_id,
            owner_user_id=view.owner_user_id,
            current_state=view.state,
            locked_affix_positions=tuple(sorted(int(value) for value in self.values)),
        )


class ForgePanelView(discord.ui.View):
    """锻造私有面板视图。"""

    def __init__(
        self,
        *,
        controller: ForgePanelController,
        owner_user_id: int,
        snapshot: ForgePanelSnapshot,
        state: ForgePanelState,
        timeout: float = _PANEL_TIMEOUT_SECONDS,
    ) -> None:
        super().__init__(timeout=timeout)
        self.controller = controller
        self.owner_user_id = owner_user_id
        self.character_id = snapshot.character_id
        self.snapshot = snapshot
        self.state = state
        self.add_item(ForgeFilterSelect(selected_filter_id=state.filter_id))
        if snapshot.targets:
            self.add_item(ForgeTargetSelect(snapshot=snapshot, state=state))
        selected_target = self._selected_target()
        show_nurture_button = (
            selected_target is not None
            and selected_target.target_kind is ForgeTargetKind.EQUIPMENT
            and selected_target.equipment_item is not None
            and ForgeOperationId.NURTURE in selected_target.supported_operations
            and state.pending_action is not ForgePendingAction.WASH
        )
        if not show_nurture_button:
            self.remove_item(self.nurture_target)
        if (
            selected_target is not None
            and selected_target.target_kind is ForgeTargetKind.EQUIPMENT
            and selected_target.equipment_item is not None
            and state.pending_action is ForgePendingAction.WASH
            and selected_target.equipment_item.affixes
        ):
            self.add_item(
                ForgeWashAffixSelect(
                    item=selected_target.equipment_item,
                    selected_locked_positions=state.selected_locked_affix_positions,
                )
            )
        self._sync_component_state()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_user_id:
            return True
        await self.controller.responder.send_private_error(interaction, message="该私有面板仅允许发起者操作。")
        return False

    def _selected_target(self) -> ForgeTargetSnapshot | None:
        return _resolve_selected_target(snapshot=self.snapshot, selected_target_id=self.state.selected_target_id)

    def _sync_component_state(self) -> None:
        selected_target = self._selected_target()
        has_equipment_target = (
            selected_target is not None
            and selected_target.target_kind is ForgeTargetKind.EQUIPMENT
            and selected_target.equipment_item is not None
        )
        can_enhance = has_equipment_target and ForgeOperationId.ENHANCE in selected_target.supported_operations
        can_wash = has_equipment_target and ForgeOperationId.WASH in selected_target.supported_operations
        can_reforge = has_equipment_target and ForgeOperationId.REFORGE in selected_target.supported_operations
        can_dismantle = has_equipment_target and ForgeOperationId.DISMANTLE in selected_target.supported_operations
        can_nurture = has_equipment_target and ForgeOperationId.NURTURE in selected_target.supported_operations
        can_unequip = has_equipment_target and ForgeOperationId.UNEQUIP in selected_target.supported_operations
        has_pending_action = self.state.pending_action is not ForgePendingAction.NONE

        self.previous_page.disabled = self.snapshot.page <= 1
        self.next_page.disabled = self.snapshot.page >= self.snapshot.total_pages or self.snapshot.total_items <= 0
        self.enhance_target.disabled = not (can_enhance and not has_pending_action)
        self.wash_target.disabled = not (
            can_wash
            and (self.state.pending_action is ForgePendingAction.NONE or self.state.pending_action is ForgePendingAction.WASH)
        )
        self.reforge_target.disabled = not (
            can_reforge
            and (self.state.pending_action is ForgePendingAction.NONE or self.state.pending_action is ForgePendingAction.REFORGE)
        )
        self.dismantle_target.disabled = not (
            can_dismantle
            and (
                self.state.pending_action is ForgePendingAction.NONE
                or self.state.pending_action is ForgePendingAction.DISMANTLE
            )
        )
        self.unequip_target.disabled = not (can_unequip and not has_pending_action)
        self.nurture_target.disabled = not (can_nurture and not has_pending_action)

        self.wash_target.label = "执行洗炼" if self.state.pending_action is ForgePendingAction.WASH else "洗炼"
        self.wash_target.style = (
            discord.ButtonStyle.success
            if self.state.pending_action is ForgePendingAction.WASH
            else discord.ButtonStyle.secondary
        )
        self.reforge_target.label = "确认重铸" if self.state.pending_action is ForgePendingAction.REFORGE else "重铸"
        self.reforge_target.style = (
            discord.ButtonStyle.danger
            if self.state.pending_action is ForgePendingAction.REFORGE
            else discord.ButtonStyle.secondary
        )
        self.dismantle_target.label = "确认分解" if self.state.pending_action is ForgePendingAction.DISMANTLE else "分解"
        self.dismantle_target.style = (
            discord.ButtonStyle.danger
            if self.state.pending_action is ForgePendingAction.DISMANTLE
            else discord.ButtonStyle.secondary
        )

    @discord.ui.button(label="上一页", style=discord.ButtonStyle.secondary, row=2)
    async def previous_page(  # type: ignore[override]
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        await self.controller.change_page(
            interaction,
            character_id=self.character_id,
            owner_user_id=self.owner_user_id,
            current_state=self.state,
            page_delta=-1,
        )

    @discord.ui.button(label="下一页", style=discord.ButtonStyle.secondary, row=2)
    async def next_page(  # type: ignore[override]
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        await self.controller.change_page(
            interaction,
            character_id=self.character_id,
            owner_user_id=self.owner_user_id,
            current_state=self.state,
            page_delta=1,
        )

    @discord.ui.button(label="刷新", style=discord.ButtonStyle.primary, row=2)
    async def refresh_panel(  # type: ignore[override]
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        await self.controller.refresh_panel(
            interaction,
            character_id=self.character_id,
            owner_user_id=self.owner_user_id,
            current_state=self.state,
        )

    @discord.ui.button(label="强化", style=discord.ButtonStyle.primary, row=3)
    async def enhance_target(  # type: ignore[override]
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        await self.controller.enhance_target(
            interaction,
            character_id=self.character_id,
            owner_user_id=self.owner_user_id,
            current_state=self.state,
        )

    @discord.ui.button(label="洗炼", style=discord.ButtonStyle.secondary, row=3)
    async def wash_target(  # type: ignore[override]
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        await self.controller.prepare_or_execute_wash(
            interaction,
            character_id=self.character_id,
            owner_user_id=self.owner_user_id,
            current_state=self.state,
        )

    @discord.ui.button(label="重铸", style=discord.ButtonStyle.secondary, row=3)
    async def reforge_target(  # type: ignore[override]
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        await self.controller.prepare_or_execute_reforge(
            interaction,
            character_id=self.character_id,
            owner_user_id=self.owner_user_id,
            current_state=self.state,
        )

    @discord.ui.button(label="分解", style=discord.ButtonStyle.danger, row=3)
    async def dismantle_target(  # type: ignore[override]
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        await self.controller.prepare_or_execute_dismantle(
            interaction,
            character_id=self.character_id,
            owner_user_id=self.owner_user_id,
            current_state=self.state,
        )

    @discord.ui.button(label="卸下装备", style=discord.ButtonStyle.secondary, row=3)
    async def unequip_target(  # type: ignore[override]
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        await self.controller.unequip_target(
            interaction,
            character_id=self.character_id,
            owner_user_id=self.owner_user_id,
            current_state=self.state,
        )

    @discord.ui.button(label="法宝培养", style=discord.ButtonStyle.primary, row=4)
    async def nurture_target(  # type: ignore[override]
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        await self.controller.nurture_target(
            interaction,
            character_id=self.character_id,
            owner_user_id=self.owner_user_id,
            current_state=self.state,
        )


class ForgePanelController:
    """组织锻造私有面板交互。"""

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        service_bundle_factory,
        responder: DiscordInteractionVisibilityResponder | None = None,
        panel_timeout: float = _PANEL_TIMEOUT_SECONDS,
    ) -> None:
        self._session_factory = session_factory
        self._service_bundle_factory = service_bundle_factory
        self.responder = responder or DiscordInteractionVisibilityResponder()
        self._panel_timeout = panel_timeout

    async def open_panel_by_discord_user_id(self, interaction: discord.Interaction) -> None:
        """按 Discord 用户标识打开锻造面板。"""
        initial_state = ForgePanelState()
        try:
            character_id = self._load_character_id_by_discord_user_id(discord_user_id=str(interaction.user.id))
            snapshot = self._load_snapshot(character_id=character_id, state=initial_state)
        except (CharacterPanelQueryServiceError, ForgePanelQueryServiceError) as exc:
            await self.responder.send_private_error(interaction, message=str(exc))
            return
        normalized_state = self._normalize_state(state=initial_state, snapshot=snapshot)
        await self._send_panel(
            interaction,
            snapshot=snapshot,
            owner_user_id=interaction.user.id,
            state=normalized_state,
        )

    async def open_panel(
        self,
        interaction: discord.Interaction,
        *,
        character_id: int,
        owner_user_id: int | None = None,
    ) -> None:
        """按角色标识打开锻造面板。"""
        initial_state = ForgePanelState()
        try:
            snapshot = self._load_snapshot(character_id=character_id, state=initial_state)
        except ForgePanelQueryServiceError as exc:
            await self.responder.send_private_error(interaction, message=str(exc))
            return
        normalized_state = self._normalize_state(state=initial_state, snapshot=snapshot)
        payload = self._build_payload(
            snapshot=snapshot,
            owner_user_id=interaction.user.id if owner_user_id is None else owner_user_id,
            state=normalized_state,
        )
        if owner_user_id is None:
            await self.responder.send_message(interaction, payload=payload, visibility=PanelVisibility.PRIVATE)
            return
        await self.responder.edit_message(interaction, payload=payload)

    async def change_filter(
        self,
        interaction: discord.Interaction,
        *,
        character_id: int,
        owner_user_id: int,
        current_state: ForgePanelState,
        filter_id: ForgeFilterId,
    ) -> None:
        next_state = replace(
            current_state,
            filter_id=filter_id,
            page=1,
            selected_target_id=None,
            pending_action=ForgePendingAction.NONE,
            selected_locked_affix_positions=(),
            action_note=None,
        )
        await self._refresh_and_edit(
            interaction,
            character_id=character_id,
            owner_user_id=owner_user_id,
            state=next_state,
        )

    async def change_page(
        self,
        interaction: discord.Interaction,
        *,
        character_id: int,
        owner_user_id: int,
        current_state: ForgePanelState,
        page_delta: int,
    ) -> None:
        next_state = replace(
            current_state,
            page=max(1, current_state.page + page_delta),
            selected_target_id=None,
            pending_action=ForgePendingAction.NONE,
            selected_locked_affix_positions=(),
            action_note=None,
        )
        await self._refresh_and_edit(
            interaction,
            character_id=character_id,
            owner_user_id=owner_user_id,
            state=next_state,
        )

    async def select_target(
        self,
        interaction: discord.Interaction,
        *,
        character_id: int,
        owner_user_id: int,
        current_state: ForgePanelState,
        target_id: str,
    ) -> None:
        next_state = replace(
            current_state,
            selected_target_id=target_id,
            pending_action=ForgePendingAction.NONE,
            selected_locked_affix_positions=(),
            action_note=None,
        )
        await self._refresh_and_edit(
            interaction,
            character_id=character_id,
            owner_user_id=owner_user_id,
            state=next_state,
        )

    async def refresh_panel(
        self,
        interaction: discord.Interaction,
        *,
        character_id: int,
        owner_user_id: int,
        current_state: ForgePanelState,
    ) -> None:
        await self._refresh_and_edit(
            interaction,
            character_id=character_id,
            owner_user_id=owner_user_id,
            state=current_state,
        )

    async def update_locked_affixes(
        self,
        interaction: discord.Interaction,
        *,
        character_id: int,
        owner_user_id: int,
        current_state: ForgePanelState,
        locked_affix_positions: tuple[int, ...],
    ) -> None:
        try:
            snapshot = self._load_snapshot(character_id=character_id, state=current_state)
        except ForgePanelQueryServiceError as exc:
            await self.responder.send_private_error(interaction, message=str(exc))
            return
        normalized_state = self._normalize_state(state=current_state, snapshot=snapshot)
        try:
            target = self._require_equipment_target(snapshot=snapshot, state=normalized_state)
            item = self._require_equipped_item(target=target)
        except ForgePanelQueryServiceError as exc:
            await self.responder.send_private_error(interaction, message=str(exc))
            return
        next_state = replace(
            normalized_state,
            pending_action=ForgePendingAction.WASH,
            selected_locked_affix_positions=tuple(sorted(set(locked_affix_positions))),
            action_note=self._build_wash_prepare_note(
                item=item,
                locked_affix_positions=tuple(sorted(set(locked_affix_positions))),
            ),
        )
        await self._edit_panel(
            interaction,
            snapshot=snapshot,
            owner_user_id=owner_user_id,
            state=next_state,
        )

    async def enhance_target(
        self,
        interaction: discord.Interaction,
        *,
        character_id: int,
        owner_user_id: int,
        current_state: ForgePanelState,
    ) -> None:
        try:
            snapshot = self._load_snapshot(character_id=character_id, state=current_state)
            normalized_state = self._normalize_state(state=current_state, snapshot=snapshot)
            target = self._require_equipment_target(snapshot=snapshot, state=normalized_state)
            item = self._require_equipped_item(target=target)
            result = self._enhance_equipment(character_id=character_id, equipment_item_id=item.item_id)
        except (ForgePanelQueryServiceError, EquipmentServiceError) as exc:
            await self.responder.send_private_error(interaction, message=str(exc))
            return
        next_state = replace(
            normalized_state,
            pending_action=ForgePendingAction.NONE,
            selected_locked_affix_positions=(),
            action_note=ForgeActionNote(title="强化结果", lines=self._build_enhancement_lines(result=result)),
        )
        await self._refresh_and_edit(
            interaction,
            character_id=character_id,
            owner_user_id=owner_user_id,
            state=next_state,
        )

    async def prepare_or_execute_wash(
        self,
        interaction: discord.Interaction,
        *,
        character_id: int,
        owner_user_id: int,
        current_state: ForgePanelState,
    ) -> None:
        try:
            snapshot = self._load_snapshot(character_id=character_id, state=current_state)
            normalized_state = self._normalize_state(state=current_state, snapshot=snapshot)
            target = self._require_equipment_target(snapshot=snapshot, state=normalized_state)
            item = self._require_equipped_item(target=target)
        except ForgePanelQueryServiceError as exc:
            await self.responder.send_private_error(interaction, message=str(exc))
            return

        if normalized_state.pending_action is not ForgePendingAction.WASH:
            next_state = replace(
                normalized_state,
                pending_action=ForgePendingAction.WASH,
                selected_locked_affix_positions=(),
                action_note=self._build_wash_prepare_note(item=item, locked_affix_positions=()),
            )
            await self._edit_panel(
                interaction,
                snapshot=snapshot,
                owner_user_id=owner_user_id,
                state=next_state,
            )
            return

        try:
            locked_affix_indices = self._normalize_locked_affix_indices(
                equipped_item=item,
                locked_affix_positions=normalized_state.selected_locked_affix_positions,
            )
            result = self._wash_equipment(
                character_id=character_id,
                equipment_item_id=item.item_id,
                locked_affix_indices=locked_affix_indices,
            )
        except (ForgePanelQueryServiceError, EquipmentServiceError) as exc:
            await self.responder.send_private_error(interaction, message=str(exc))
            return

        next_state = replace(
            normalized_state,
            pending_action=ForgePendingAction.NONE,
            selected_locked_affix_positions=(),
            action_note=ForgeActionNote(
                title="洗炼结果",
                lines=self._build_wash_lines(
                    item=item,
                    locked_affix_positions=normalized_state.selected_locked_affix_positions,
                    result=result,
                ),
            ),
        )
        await self._refresh_and_edit(
            interaction,
            character_id=character_id,
            owner_user_id=owner_user_id,
            state=next_state,
        )

    async def prepare_or_execute_reforge(
        self,
        interaction: discord.Interaction,
        *,
        character_id: int,
        owner_user_id: int,
        current_state: ForgePanelState,
    ) -> None:
        try:
            snapshot = self._load_snapshot(character_id=character_id, state=current_state)
            normalized_state = self._normalize_state(state=current_state, snapshot=snapshot)
            target = self._require_equipment_target(snapshot=snapshot, state=normalized_state)
            item = self._require_equipped_item(target=target)
        except ForgePanelQueryServiceError as exc:
            await self.responder.send_private_error(interaction, message=str(exc))
            return

        if normalized_state.pending_action is not ForgePendingAction.REFORGE:
            next_state = replace(
                normalized_state,
                pending_action=ForgePendingAction.REFORGE,
                selected_locked_affix_positions=(),
                action_note=self._build_confirmation_note(
                    title="重铸确认",
                    item=item,
                    confirm_label="确认重铸",
                ),
            )
            await self._edit_panel(
                interaction,
                snapshot=snapshot,
                owner_user_id=owner_user_id,
                state=next_state,
            )
            return

        try:
            result = self._reforge_equipment(character_id=character_id, equipment_item_id=item.item_id)
        except EquipmentServiceError as exc:
            await self.responder.send_private_error(interaction, message=str(exc))
            return

        next_state = replace(
            normalized_state,
            pending_action=ForgePendingAction.NONE,
            selected_locked_affix_positions=(),
            action_note=ForgeActionNote(title="重铸结果", lines=self._build_reforge_lines(result=result)),
        )
        await self._refresh_and_edit(
            interaction,
            character_id=character_id,
            owner_user_id=owner_user_id,
            state=next_state,
        )

    async def prepare_or_execute_dismantle(
        self,
        interaction: discord.Interaction,
        *,
        character_id: int,
        owner_user_id: int,
        current_state: ForgePanelState,
    ) -> None:
        try:
            snapshot = self._load_snapshot(character_id=character_id, state=current_state)
            normalized_state = self._normalize_state(state=current_state, snapshot=snapshot)
            target = self._require_equipment_target(snapshot=snapshot, state=normalized_state)
            item = self._require_equipped_item(target=target)
        except ForgePanelQueryServiceError as exc:
            await self.responder.send_private_error(interaction, message=str(exc))
            return

        if normalized_state.pending_action is not ForgePendingAction.DISMANTLE:
            next_state = replace(
                normalized_state,
                pending_action=ForgePendingAction.DISMANTLE,
                selected_locked_affix_positions=(),
                action_note=self._build_confirmation_note(
                    title="分解确认",
                    item=item,
                    confirm_label="确认分解",
                ),
            )
            await self._edit_panel(
                interaction,
                snapshot=snapshot,
                owner_user_id=owner_user_id,
                state=next_state,
            )
            return

        try:
            result = self._dismantle_equipment(character_id=character_id, equipment_item_id=item.item_id)
        except EquipmentServiceError as exc:
            await self.responder.send_private_error(interaction, message=str(exc))
            return

        next_state = replace(
            normalized_state,
            pending_action=ForgePendingAction.NONE,
            selected_locked_affix_positions=(),
            action_note=ForgeActionNote(title="分解结果", lines=self._build_dismantle_lines(result=result)),
        )
        await self._refresh_and_edit(
            interaction,
            character_id=character_id,
            owner_user_id=owner_user_id,
            state=next_state,
        )

    async def nurture_target(
        self,
        interaction: discord.Interaction,
        *,
        character_id: int,
        owner_user_id: int,
        current_state: ForgePanelState,
    ) -> None:
        try:
            snapshot = self._load_snapshot(character_id=character_id, state=current_state)
            normalized_state = self._normalize_state(state=current_state, snapshot=snapshot)
            target = self._require_equipment_target(snapshot=snapshot, state=normalized_state)
            item = self._require_equipped_item(target=target)
            if not item.is_artifact:
                raise ForgePanelQueryServiceError("当前目标不是可培养法宝。")
            result = self._nurture_artifact(character_id=character_id, equipment_item_id=item.item_id)
        except (ForgePanelQueryServiceError, EquipmentServiceError) as exc:
            await self.responder.send_private_error(interaction, message=str(exc))
            return
        next_state = replace(
            normalized_state,
            pending_action=ForgePendingAction.NONE,
            selected_locked_affix_positions=(),
            action_note=ForgeActionNote(title="法宝培养结果", lines=self._build_nurture_lines(result=result)),
        )
        await self._refresh_and_edit(
            interaction,
            character_id=character_id,
            owner_user_id=owner_user_id,
            state=next_state,
        )

    async def unequip_target(
        self,
        interaction: discord.Interaction,
        *,
        character_id: int,
        owner_user_id: int,
        current_state: ForgePanelState,
    ) -> None:
        try:
            snapshot = self._load_snapshot(character_id=character_id, state=current_state)
            normalized_state = self._normalize_state(state=current_state, snapshot=snapshot)
            target = self._require_equipment_target(snapshot=snapshot, state=normalized_state)
            item = self._require_equipped_item(target=target)
            if ForgeOperationId.UNEQUIP not in target.supported_operations or not target.equipped:
                raise ForgePanelQueryServiceError("当前目标未处于已装备状态，无法卸下。")
            result = self._unequip_item(character_id=character_id, equipped_slot_id=item.slot_id)
        except (ForgePanelQueryServiceError, EquipmentServiceError) as exc:
            await self.responder.send_private_error(interaction, message=str(exc))
            return
        next_state = replace(
            normalized_state,
            pending_action=ForgePendingAction.NONE,
            selected_locked_affix_positions=(),
            action_note=ForgeActionNote(title="卸下结果", lines=self._build_unequip_lines(result=result)),
        )
        await self._refresh_and_edit(
            interaction,
            character_id=character_id,
            owner_user_id=owner_user_id,
            state=next_state,
        )

    async def _refresh_and_edit(
        self,
        interaction: discord.Interaction,
        *,
        character_id: int,
        owner_user_id: int,
        state: ForgePanelState,
    ) -> None:
        try:
            snapshot = self._load_snapshot(character_id=character_id, state=state)
        except ForgePanelQueryServiceError as exc:
            await self.responder.send_private_error(interaction, message=str(exc))
            return
        normalized_state = self._normalize_state(state=state, snapshot=snapshot)
        await self._edit_panel(
            interaction,
            snapshot=snapshot,
            owner_user_id=owner_user_id,
            state=normalized_state,
        )

    @staticmethod
    def _normalize_state(*, state: ForgePanelState, snapshot: ForgePanelSnapshot) -> ForgePanelState:
        selected_target_id = state.selected_target_id
        valid_target_ids = {target.target_id for target in snapshot.targets}
        if selected_target_id not in valid_target_ids:
            selected_target_id = _resolve_default_target_id(snapshot=snapshot)
        selected_target = _resolve_selected_target(snapshot=snapshot, selected_target_id=selected_target_id)
        pending_action = state.pending_action
        selected_locked_affix_positions = state.selected_locked_affix_positions
        action_note = state.action_note
        if (
            selected_target is None
            or selected_target.target_kind is ForgeTargetKind.SKILL
            or selected_target.equipment_item is None
        ):
            pending_action = ForgePendingAction.NONE
            selected_locked_affix_positions = ()
        if state.selected_target_id != selected_target_id:
            selected_locked_affix_positions = ()
            if state.pending_action is not ForgePendingAction.NONE:
                pending_action = ForgePendingAction.NONE
                action_note = None
        return replace(
            state,
            filter_id=snapshot.filter_id,
            page=snapshot.page,
            selected_target_id=selected_target_id,
            pending_action=pending_action,
            selected_locked_affix_positions=selected_locked_affix_positions,
            action_note=action_note,
        )

    def _load_character_id_by_discord_user_id(self, *, discord_user_id: str) -> int:
        with session_scope(self._session_factory) as session:
            services = self._service_bundle_factory(session)
            overview = services.character_panel_query_service.get_overview_by_discord_user_id(
                discord_user_id=discord_user_id,
            )
            return overview.character_id

    def _load_snapshot(self, *, character_id: int, state: ForgePanelState) -> ForgePanelSnapshot:
        with session_scope(self._session_factory) as session:
            services = self._service_bundle_factory(session)
            return services.forge_panel_query_service.get_panel_snapshot(
                character_id=character_id,
                filter_id=state.filter_id,
                page=state.page,
                selected_target_id=state.selected_target_id,
                pending_action=(None if state.pending_action is ForgePendingAction.NONE else state.pending_action.value),
                locked_affix_positions=state.selected_locked_affix_positions,
            )

    def _enhance_equipment(
        self,
        *,
        character_id: int,
        equipment_item_id: int,
    ) -> EquipmentEnhancementApplicationResult:
        with session_scope(self._session_factory) as session:
            services = self._service_bundle_factory(session)
            return services.equipment_service.enhance_equipment(
                character_id=character_id,
                equipment_item_id=equipment_item_id,
            )

    def _wash_equipment(
        self,
        *,
        character_id: int,
        equipment_item_id: int,
        locked_affix_indices: tuple[int, ...],
    ) -> EquipmentWashApplicationResult:
        with session_scope(self._session_factory) as session:
            services = self._service_bundle_factory(session)
            return services.equipment_service.wash_equipment(
                character_id=character_id,
                equipment_item_id=equipment_item_id,
                locked_affix_indices=locked_affix_indices,
            )

    def _reforge_equipment(
        self,
        *,
        character_id: int,
        equipment_item_id: int,
    ) -> EquipmentReforgeApplicationResult:
        with session_scope(self._session_factory) as session:
            services = self._service_bundle_factory(session)
            return services.equipment_service.reforge_equipment(
                character_id=character_id,
                equipment_item_id=equipment_item_id,
            )

    def _nurture_artifact(
        self,
        *,
        character_id: int,
        equipment_item_id: int,
    ) -> ArtifactNurtureApplicationResult:
        with session_scope(self._session_factory) as session:
            services = self._service_bundle_factory(session)
            return services.equipment_service.nurture_artifact(
                character_id=character_id,
                equipment_item_id=equipment_item_id,
            )

    def _dismantle_equipment(
        self,
        *,
        character_id: int,
        equipment_item_id: int,
    ) -> EquipmentDismantleApplicationResult:
        with session_scope(self._session_factory) as session:
            services = self._service_bundle_factory(session)
            return services.equipment_service.dismantle_equipment(
                character_id=character_id,
                equipment_item_id=equipment_item_id,
            )

    def _unequip_item(
        self,
        *,
        character_id: int,
        equipped_slot_id: str,
    ) -> EquipmentUnequipApplicationResult:
        with session_scope(self._session_factory) as session:
            services = self._service_bundle_factory(session)
            return services.equipment_service.unequip_item(
                character_id=character_id,
                equipped_slot_id=equipped_slot_id,
            )

    @staticmethod
    def _require_equipment_target(*, snapshot: ForgePanelSnapshot, state: ForgePanelState) -> ForgeTargetSnapshot:
        target = _resolve_selected_target(snapshot=snapshot, selected_target_id=state.selected_target_id)
        if target is None:
            raise ForgePanelQueryServiceError("当前没有可操作的锻造目标。")
        if target.target_kind is ForgeTargetKind.SKILL:
            raise ForgePanelQueryServiceError(_SKILL_ACTION_DISABLED_TEXT)
        return target

    @staticmethod
    def _require_equipped_item(*, target: ForgeTargetSnapshot) -> EquipmentItemSnapshot:
        if target.equipment_item is None:
            raise ForgePanelQueryServiceError("当前选中目标已失效，请重新选择。")
        return target.equipment_item

    @staticmethod
    def _build_wash_prepare_note(
        *,
        item: EquipmentItemSnapshot,
        locked_affix_positions: tuple[int, ...],
    ) -> ForgeActionNote:
        lines = [f"目标：{item.display_name}"]
        if item.affixes:
            lines.append("锁定：" + ForgePanelPresenter._build_locked_affix_summary(item=item, locked_affix_positions=locked_affix_positions))
        return ForgeActionNote(title="洗炼", lines=tuple(lines))

    @staticmethod
    def _build_confirmation_note(*, title: str, item: EquipmentItemSnapshot, confirm_label: str) -> ForgeActionNote:
        return ForgeActionNote(title=title, lines=(f"目标：{item.display_name}", f"再次点击“{confirm_label}”后执行。"))

    @staticmethod
    def _normalize_locked_affix_indices(
        *,
        equipped_item: EquipmentItemSnapshot,
        locked_affix_positions: tuple[int, ...],
    ) -> tuple[int, ...]:
        if not locked_affix_positions:
            return ()
        valid_positions = {affix.position or index for index, affix in enumerate(equipped_item.affixes, start=1)}
        normalized_positions = tuple(sorted(set(locked_affix_positions)))
        invalid_positions = [str(position) for position in normalized_positions if position not in valid_positions]
        if invalid_positions:
            raise ForgePanelQueryServiceError("洗炼锁定词条已失效，请重新选择。")
        return tuple(position - 1 for position in normalized_positions)

    @classmethod
    def _build_enhancement_lines(
        cls,
        *,
        result: EquipmentEnhancementApplicationResult,
    ) -> tuple[str, ...]:
        lines = [
            f"{result.item.display_name}",
            f"强化 +{result.previous_level} → +{result.target_level}",
            f"{'成功' if result.success else '失败'}｜{float(result.success_rate) * 100:.1f}%",
        ]
        if result.added_affixes:
            lines.append("新增：" + "｜".join(cls._format_affix_brief(affix) for affix in result.added_affixes[:3]))
        lines.extend(cls._build_resource_change_lines(resource_changes=result.resource_changes))
        return tuple(lines)

    @classmethod
    def _build_wash_lines(
        cls,
        *,
        item: EquipmentItemSnapshot,
        locked_affix_positions: tuple[int, ...],
        result: EquipmentWashApplicationResult,
    ) -> tuple[str, ...]:
        lines = [
            f"{result.item.display_name}",
            "锁定："
            + ForgePanelPresenter._build_locked_affix_summary(
                item=item,
                locked_affix_positions=locked_affix_positions,
            ),
        ]
        if result.rerolled_affixes:
            lines.append("新词条：" + "｜".join(cls._format_affix_brief(affix) for affix in result.rerolled_affixes[:3]))
        else:
            lines.append("新词条：无变化")
        lines.extend(cls._build_resource_change_lines(resource_changes=result.resource_changes))
        return tuple(lines)

    @classmethod
    def _build_reforge_lines(
        cls,
        *,
        result: EquipmentReforgeApplicationResult,
    ) -> tuple[str, ...]:
        lines = [
            f"{result.item.display_name}",
            f"底材：{result.previous_template_id} → {result.item.template_name}",
        ]
        if result.previous_affixes:
            lines.append("旧词条：" + "｜".join(cls._format_affix_brief(affix) for affix in result.previous_affixes[:3]))
        lines.extend(cls._build_resource_change_lines(resource_changes=result.resource_changes))
        return tuple(lines)

    @classmethod
    def _build_nurture_lines(
        cls,
        *,
        result: ArtifactNurtureApplicationResult,
    ) -> tuple[str, ...]:
        lines = [
            f"{result.item.display_name}",
            f"祭炼 {result.previous_level} → {result.target_level}",
        ]
        lines.extend(cls._build_resource_change_lines(resource_changes=result.resource_changes))
        return tuple(lines)

    @classmethod
    def _build_dismantle_lines(
        cls,
        *,
        result: EquipmentDismantleApplicationResult,
    ) -> tuple[str, ...]:
        lines = [
            f"已分解：{result.item.display_name}",
            f"时间：{cls._format_datetime(result.settled_at)}",
        ]
        lines.extend(cls._build_resource_change_lines(resource_changes=result.resource_changes))
        return tuple(lines)

    @staticmethod
    def _build_unequip_lines(*, result: EquipmentUnequipApplicationResult) -> tuple[str, ...]:
        return (
            f"已卸下部位：{result.unequipped_slot_id}",
            f"当前物品：{result.item.display_name}",
            "物品已回到背包，可在背包中重新装配。",
        )

    @classmethod
    def _build_resource_change_lines(
        cls,
        *,
        resource_changes: tuple[EquipmentResourceLedgerEntry, ...],
    ) -> tuple[str, ...]:
        if not resource_changes:
            return ()
        formatted = []
        for entry in resource_changes[:4]:
            sign = "-" if entry.change_type == "consume" else "+"
            formatted.append(f"{_format_resource_name(entry.resource_id)} {sign}{entry.quantity}")
        return ("消耗：" + "｜".join(formatted),)

    @staticmethod
    def _format_affix_brief(affix) -> str:
        return ForgePanelPresenter._format_affix_line(affix)

    @staticmethod
    def _format_datetime(value) -> str:
        return f"{discord.utils.format_dt(value, style='f')}｜{discord.utils.format_dt(value, style='R')}"

    async def _send_panel(
        self,
        interaction: discord.Interaction,
        *,
        snapshot: ForgePanelSnapshot,
        owner_user_id: int,
        state: ForgePanelState,
    ) -> None:
        payload = self._build_payload(snapshot=snapshot, owner_user_id=owner_user_id, state=state)
        await self.responder.send_message(interaction, payload=payload, visibility=PanelVisibility.PRIVATE)

    async def _edit_panel(
        self,
        interaction: discord.Interaction,
        *,
        snapshot: ForgePanelSnapshot,
        owner_user_id: int,
        state: ForgePanelState,
    ) -> None:
        payload = self._build_payload(snapshot=snapshot, owner_user_id=owner_user_id, state=state)
        await self.responder.edit_message(interaction, payload=payload)

    def _build_payload(
        self,
        *,
        snapshot: ForgePanelSnapshot,
        owner_user_id: int,
        state: ForgePanelState,
    ) -> PanelMessagePayload:
        normalized_state = self._normalize_state(state=state, snapshot=snapshot)
        view = ForgePanelView(
            controller=self,
            owner_user_id=owner_user_id,
            snapshot=snapshot,
            state=normalized_state,
            timeout=self._panel_timeout,
        )
        embed = ForgePanelPresenter.build_embed(snapshot=snapshot, state=normalized_state)
        return PanelMessagePayload(embed=embed, view=view)


def _resolve_default_target_id(*, snapshot: ForgePanelSnapshot) -> str | None:
    if snapshot.selected_target is not None:
        return snapshot.selected_target.target_id
    if not snapshot.targets:
        return None
    return snapshot.targets[0].target_id


def _resolve_selected_target(*, snapshot: ForgePanelSnapshot, selected_target_id: str | None) -> ForgeTargetSnapshot | None:
    if selected_target_id is None:
        return snapshot.selected_target
    for target in snapshot.targets:
        if target.target_id == selected_target_id:
            return target
    return snapshot.selected_target


def _build_target_option_description(*, target: ForgeTargetSnapshot) -> str:
    if target.target_kind is ForgeTargetKind.EQUIPMENT:
        source_tag = "已装备" if target.equipped else "背包"
        return f"{source_tag}｜{target.summary_line}"
    if target.equipped_skill is None:
        return "功法目标未就绪"
    return f"{target.equipped_skill.skill_name}｜{target.equipped_skill.path_name}"


def _format_patch_name(patch_id: str) -> str:
    normalized_patch_id = patch_id.strip()
    if not normalized_patch_id:
        return "未命名流派修正"
    patch = get_static_config().skill_generation.get_patch(normalized_patch_id)
    if patch is not None:
        return str(patch.name)
    if _looks_like_internal_identifier(normalized_patch_id):
        return "未命名流派修正"
    return normalized_patch_id


def _looks_like_internal_identifier(value: str) -> bool:
    return all(character.islower() or character.isdigit() or character == "_" for character in value)


def _format_resource_name(resource_id: str) -> str:
    normalized_resource_id = resource_id.strip()
    return {
        "spirit_stone": "灵石",
        "enhancement_stone": "强化石",
        "enhancement_shard": "强化碎晶",
        "wash_dust": "洗炼尘",
        "spirit_sand": "灵砂",
        "spirit_pattern_stone": "灵纹石",
        "soul_binding_jade": "缚魂玉",
        "artifact_essence": "法宝精粹",
    }.get(normalized_resource_id, normalized_resource_id)


__all__ = [
    "ForgeActionNote",
    "ForgePanelController",
    "ForgePanelPresenter",
    "ForgePanelState",
    "ForgePanelView",
    "ForgePendingAction",
]
