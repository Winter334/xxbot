"""Discord 背包私有面板。"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Protocol

import discord
from sqlalchemy.orm import Session, sessionmaker

from application.character.panel_query_service import CharacterPanelQueryService, CharacterPanelQueryServiceError
from application.character.profile_panel_query_service import SkillPanelSkillSlotSnapshot
from application.character.skill_loadout_service import (
    SkillLoadoutService,
    SkillLoadoutServiceError,
    SkillSlotEquipApplicationResult,
)
from application.equipment.backpack_query_service import (
    BackpackCardSnapshot,
    BackpackEntryKey,
    BackpackEntryKind,
    BackpackFilterId,
    BackpackPanelQueryService,
    BackpackPanelQueryServiceError,
    BackpackPanelSnapshot,
)
from application.equipment.equipment_service import (
    EquipmentEquipApplicationResult,
    EquipmentItemSnapshot,
    EquipmentService,
    EquipmentServiceError,
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
_FILTER_LABEL_BY_ID = {
    BackpackFilterId.ALL: "全部",
    BackpackFilterId.WEAPON: "武器",
    BackpackFilterId.ARMOR: "护甲",
    BackpackFilterId.ACCESSORY: "饰品",
    BackpackFilterId.ARTIFACT: "法宝",
    BackpackFilterId.SKILL: "功法",
}


class BackpackPanelServiceBundle(Protocol):
    """背包面板所需的最小服务集合。"""

    character_panel_query_service: CharacterPanelQueryService
    backpack_panel_query_service: BackpackPanelQueryService
    equipment_service: EquipmentService
    skill_loadout_service: SkillLoadoutService


@dataclass(frozen=True, slots=True)
class BackpackActionNote:
    """背包动作反馈。"""

    title: str
    lines: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BackpackPanelState:
    """背包面板显式状态。"""

    filter_id: BackpackFilterId = BackpackFilterId.ALL
    page: int = 1
    selected_entry_key: BackpackEntryKey | None = None
    action_note: BackpackActionNote | None = None


class BackpackPanelPresenter:
    """负责把背包快照投影为 Discord Embed。"""

    @classmethod
    def build_embed(
        cls,
        *,
        snapshot: BackpackPanelSnapshot,
        state: BackpackPanelState,
    ) -> discord.Embed:
        embed = discord.Embed(
            title=f"{snapshot.character_name}｜背包",
            description="仅操作者可见",
            color=discord.Color.dark_gold(),
        )
        embed.add_field(name="🎒 当前页", value=cls._build_page_entries_block(snapshot=snapshot), inline=False)
        embed.add_field(name="✨ 选中卡", value=cls._build_selected_detail_block(snapshot=snapshot), inline=False)
        embed.add_field(name="🛡 当前已装同槽卡", value=cls._build_equipped_detail_block(snapshot=snapshot), inline=False)
        if state.action_note is not None and state.action_note.lines:
            embed.add_field(name=state.action_note.title, value="\n".join(state.action_note.lines), inline=False)
        embed.set_footer(text="背包：浏览当前页与装配所选实例")
        return embed

    @classmethod
    def _build_overview_block(cls, *, snapshot: BackpackPanelSnapshot) -> str:
        selected_label = "无"
        if snapshot.selected_detail is not None:
            selected_label = cls._format_selected_entry_label(snapshot=snapshot)
        return "\n".join(
            (
                f"当前筛选：{_FILTER_LABEL_BY_ID[snapshot.filter_id]}",
                f"页码：第 {snapshot.page}/{snapshot.total_pages} 页",
                f"实例数：{snapshot.total_items}",
                f"当前选中：{selected_label}",
            )
        )

    @classmethod
    def _build_page_entries_block(cls, *, snapshot: BackpackPanelSnapshot) -> str:
        if not snapshot.page_entries:
            if snapshot.total_items <= 0:
                return "当前背包为空。"
            return "当前筛选下本页无实例。"
        lines = [f"第 {snapshot.page}/{snapshot.total_pages} 页｜共 {snapshot.total_items} 件"]
        for index, entry in enumerate(snapshot.page_entries, start=1 + (snapshot.page - 1) * snapshot.page_size):
            prefix = ">" if snapshot.selected_detail is not None and entry.entry_key == snapshot.selected_detail.entry_key else " "
            equipped_tag = " 已装" if entry.equipped else ""
            kind_tag = "功法" if entry.entry_kind is BackpackEntryKind.SKILL else ("法宝" if entry.is_artifact else entry.slot_name)
            lines.append(f"{prefix}{index:02d}. {kind_tag}{equipped_tag} {entry.display_name}")
            lines.append(f"   {entry.summary_line}")
        return f"```\n{cls._truncate_lines(tuple(lines), limit=900)}\n```"

    @classmethod
    def _build_selected_detail_block(cls, *, snapshot: BackpackPanelSnapshot) -> str:
        detail = snapshot.selected_detail
        if detail is None or detail.selected_card is None:
            return "尚未选中实例。"
        return cls._format_card(detail.selected_card)

    @classmethod
    def _build_equipped_detail_block(cls, *, snapshot: BackpackPanelSnapshot) -> str:
        detail = snapshot.selected_detail
        if detail is None:
            return "尚未选中实例。"
        if detail.equipped_card is None:
            return "当前槽位没有已装实例。"
        return cls._format_card(detail.equipped_card)

    @classmethod
    def _build_compare_block(cls, *, snapshot: BackpackPanelSnapshot) -> str:
        detail = snapshot.selected_detail
        if detail is None:
            return "尚未选中实例。"
        if detail.entry_kind is BackpackEntryKind.EQUIPMENT:
            return cls._build_equipment_compare_block(snapshot=snapshot)
        return cls._build_skill_compare_block(snapshot=snapshot)

    @classmethod
    def _build_equipment_compare_block(cls, *, snapshot: BackpackPanelSnapshot) -> str:
        detail = snapshot.selected_detail
        assert detail is not None
        selected_item = detail.equipment_item
        equipped_item = detail.same_type_equipped_equipment_item
        if selected_item is None:
            return "选中实例已失效。"
        if equipped_item is None:
            return f"对比槽位：{selected_item.slot_name}\n当前该槽位暂无已装备实例。"
        if detail.is_same_as_equipped:
            return f"对比槽位：{selected_item.slot_name}\n当前已装备同类实例即该实例。"
        delta_parts = cls._build_equipment_delta_parts(selected_item=selected_item, equipped_item=equipped_item)
        lines = [
            f"对比槽位：{selected_item.slot_name}",
            f"当前装配：{cls._format_equipment_head(equipped_item)}",
            f"目标装配：{cls._format_equipment_head(selected_item)}",
            "主要属性差异：" + ("｜".join(delta_parts) if delta_parts else "无明显差异"),
        ]
        return "\n".join(lines)

    @classmethod
    def _build_skill_compare_block(cls, *, snapshot: BackpackPanelSnapshot) -> str:
        detail = snapshot.selected_detail
        assert detail is not None
        selected_item = detail.skill_item
        equipped_item = detail.same_type_equipped_skill_item
        if selected_item is None:
            return "选中实例已失效。"
        if equipped_item is None:
            return f"对比槽位：{selected_item.slot_name}\n当前该槽位暂无已装配功法。"
        if detail.is_same_as_equipped:
            return f"对比槽位：{selected_item.slot_name}\n当前已装配同类实例即该实例。"
        patch_delta = len(selected_item.resolved_patch_ids) - len(equipped_item.resolved_patch_ids)
        lines = [
            f"对比槽位：{selected_item.slot_name}",
            f"当前装配：{equipped_item.skill_name}｜{equipped_item.path_name}",
            f"目标装配：{selected_item.skill_name}｜{selected_item.path_name}",
            f"阶数 / 品质：{equipped_item.rank_name}｜{equipped_item.quality_name} -> {selected_item.rank_name}｜{selected_item.quality_name}",
            f"流派加成数量变化：{patch_delta:+d}",
        ]
        return "\n".join(lines)

    @classmethod
    def _format_selected_entry_label(cls, *, snapshot: BackpackPanelSnapshot) -> str:
        detail = snapshot.selected_detail
        if detail is None:
            return "无"
        if detail.entry_kind is BackpackEntryKind.EQUIPMENT and detail.equipment_item is not None:
            return detail.equipment_item.display_name
        if detail.entry_kind is BackpackEntryKind.SKILL and detail.skill_item is not None:
            return detail.skill_item.skill_name
        return "无"

    @classmethod
    def _format_card(cls, card: BackpackCardSnapshot) -> str:
        lines = [card.name, f"```\n{card.badge_line}"]
        if card.growth_line:
            lines.append(card.growth_line)
        lines.extend(card.stat_lines[:4])
        lines.append("```")
        if card.keyword_lines:
            lines.append("词条：" + "｜".join(card.keyword_lines[:3]))
        else:
            lines.append("词条：无")
        return cls._truncate_lines(tuple(lines), limit=1000)

    @classmethod
    def _format_equipment_detail(cls, item: EquipmentItemSnapshot) -> str:
        lines = [
            f"名称：{cls._format_equipment_head(item)}",
            f"部位：{item.slot_name}",
            f"{'法宝器胚' if item.is_artifact else '底材'}：{item.template_name}",
            f"阶数 / 品质：{item.rank_name}｜{item.quality_name}",
            f"强化：+{item.enhancement_level}",
            f"主要属性：{cls._format_primary_stat_lines(item, limit=4)}",
        ]
        if item.is_artifact:
            lines.append(f"祭炼：{item.artifact_nurture_level}")
            lines.append(f"共鸣：{item.resonance_name or '无'}")
        affix_lines = cls._format_affix_detail_lines(item.affixes)
        if affix_lines:
            lines.append("词条明细：")
            lines.extend(affix_lines)
        else:
            lines.append("词条：无")
        return cls._truncate_lines(tuple(lines), limit=1000)

    @classmethod
    def _format_skill_detail(cls, skill_item: SkillPanelSkillSlotSnapshot) -> str:
        lines = [
            f"功法：{skill_item.skill_name}",
            f"槽位：{skill_item.slot_name}",
            f"流派：{skill_item.path_name}",
            f"阶数 / 品质：{skill_item.rank_name}｜{skill_item.quality_name}",
        ]
        if skill_item.resolved_patch_ids:
            lines.append("流派加成：" + "｜".join(_format_patch_name(patch_id) for patch_id in skill_item.resolved_patch_ids[:5]))
        else:
            lines.append("流派加成：无")
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

    @classmethod
    def _build_equipment_delta_parts(
        cls,
        *,
        selected_item: EquipmentItemSnapshot,
        equipped_item: EquipmentItemSnapshot,
    ) -> tuple[str, ...]:
        selected_map = {stat.stat_id: stat.value for stat in selected_item.resolved_stats}
        equipped_map = {stat.stat_id: stat.value for stat in equipped_item.resolved_stats}
        all_stat_ids = sorted(set(selected_map) | set(equipped_map))
        delta_pairs: list[tuple[int, str, str]] = []
        for stat_id in all_stat_ids:
            delta_value = selected_map.get(stat_id, 0) - equipped_map.get(stat_id, 0)
            if delta_value == 0:
                continue
            sign = "+" if delta_value > 0 else ""
            delta_pairs.append(
                (
                    abs(delta_value),
                    cls._format_stat_name(stat_id),
                    f"{cls._format_stat_name(stat_id)} {sign}{cls._format_stat_value(stat_id, delta_value)}",
                )
            )
        delta_pairs.sort(key=lambda item: (-item[0], item[1]))
        return tuple(item[2] for item in delta_pairs[:5])

    @staticmethod
    def _format_stat_name(stat_id: str) -> str:
        return _STAT_NAME_BY_ID.get(stat_id, stat_id)

    @staticmethod
    def _format_stat_value(stat_id: str, value: int) -> str:
        if stat_id.endswith("_permille"):
            return f"{value / 10:.1f}%"
        return str(value)

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


class BackpackFilterSelect(discord.ui.Select):
    """背包筛选选择器。"""

    def __init__(self, *, selected_filter_id: BackpackFilterId) -> None:
        options = [
            discord.SelectOption(
                label=_FILTER_LABEL_BY_ID[filter_id],
                value=filter_id.value,
                default=filter_id is selected_filter_id,
            )
            for filter_id in (
                BackpackFilterId.ALL,
                BackpackFilterId.WEAPON,
                BackpackFilterId.ARMOR,
                BackpackFilterId.ACCESSORY,
                BackpackFilterId.ARTIFACT,
                BackpackFilterId.SKILL,
            )
        ]
        super().__init__(
            placeholder="选择背包筛选",
            min_values=1,
            max_values=1,
            options=options,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, BackpackPanelView):
            await interaction.response.defer()
            return
        await view.controller.change_filter(
            interaction,
            character_id=view.character_id,
            owner_user_id=view.owner_user_id,
            current_state=view.state,
            filter_id=BackpackFilterId(self.values[0]),
        )


class BackpackItemSelect(discord.ui.Select):
    """背包当前页实例选择器。"""

    def __init__(self, *, snapshot: BackpackPanelSnapshot, state: BackpackPanelState) -> None:
        options = [
            discord.SelectOption(
                label=entry.display_name[:100],
                value=entry.entry_key.serialize(),
                description=entry.summary_line[:100],
                default=entry.entry_key == state.selected_entry_key,
            )
            for entry in snapshot.page_entries[:_MAX_SELECT_OPTIONS]
        ]
        super().__init__(
            placeholder="选择当前页实例查看详情",
            min_values=1,
            max_values=1,
            options=options,
            row=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, BackpackPanelView):
            await interaction.response.defer()
            return
        await view.controller.select_entry(
            interaction,
            character_id=view.character_id,
            owner_user_id=view.owner_user_id,
            current_state=view.state,
            entry_key=BackpackEntryKey.parse(self.values[0]),
        )


class BackpackPanelView(discord.ui.View):
    """背包私有面板视图。"""

    def __init__(
        self,
        *,
        controller: BackpackPanelController,
        owner_user_id: int,
        snapshot: BackpackPanelSnapshot,
        state: BackpackPanelState,
        timeout: float = _PANEL_TIMEOUT_SECONDS,
    ) -> None:
        super().__init__(timeout=timeout)
        self.controller = controller
        self.owner_user_id = owner_user_id
        self.character_id = snapshot.character_id
        self.snapshot = snapshot
        self.state = state
        self.add_item(BackpackFilterSelect(selected_filter_id=state.filter_id))
        if snapshot.page_entries:
            self.add_item(BackpackItemSelect(snapshot=snapshot, state=state))
        self._sync_component_state()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_user_id:
            return True
        await self.controller.responder.send_private_error(interaction, message="该私有面板仅允许发起者操作。")
        return False

    def build_embed(self) -> discord.Embed:
        return BackpackPanelPresenter.build_embed(snapshot=self.snapshot, state=self.state)

    def _sync_component_state(self) -> None:
        self.previous_page.disabled = self.snapshot.page <= 1
        self.next_page.disabled = self.snapshot.page >= self.snapshot.total_pages or self.snapshot.total_items <= 0
        selected_detail = self.snapshot.selected_detail
        self.equip_selected.label = "装配"
        self.equip_selected.disabled = True
        if selected_detail is not None:
            self.equip_selected.label = selected_detail.equip_action_label
            self.equip_selected.disabled = not selected_detail.equip_action_enabled

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

    @discord.ui.button(label="装配", style=discord.ButtonStyle.success, row=2)
    async def equip_selected(  # type: ignore[override]
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        await self.controller.equip_selected(
            interaction,
            character_id=self.character_id,
            owner_user_id=self.owner_user_id,
            current_state=self.state,
        )


class BackpackPanelController:
    """组织背包私有面板交互。"""

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
        """按 Discord 用户标识打开背包面板。"""
        initial_state = BackpackPanelState()
        try:
            character_id = self._load_character_id_by_discord_user_id(discord_user_id=str(interaction.user.id))
            snapshot = self._load_snapshot(character_id=character_id, state=initial_state)
        except (CharacterPanelQueryServiceError, BackpackPanelQueryServiceError) as exc:
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
        """按角色标识打开背包面板。"""
        initial_state = BackpackPanelState()
        try:
            snapshot = self._load_snapshot(character_id=character_id, state=initial_state)
        except BackpackPanelQueryServiceError as exc:
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
        current_state: BackpackPanelState,
        filter_id: BackpackFilterId,
    ) -> None:
        next_state = replace(
            current_state,
            filter_id=filter_id,
            page=1,
            selected_entry_key=None,
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
        current_state: BackpackPanelState,
        page_delta: int,
    ) -> None:
        next_state = replace(
            current_state,
            page=max(1, current_state.page + page_delta),
            action_note=None,
        )
        await self._refresh_and_edit(
            interaction,
            character_id=character_id,
            owner_user_id=owner_user_id,
            state=next_state,
        )

    async def select_entry(
        self,
        interaction: discord.Interaction,
        *,
        character_id: int,
        owner_user_id: int,
        current_state: BackpackPanelState,
        entry_key: BackpackEntryKey,
    ) -> None:
        next_state = replace(current_state, selected_entry_key=entry_key, action_note=None)
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
        current_state: BackpackPanelState,
    ) -> None:
        await self._refresh_and_edit(
            interaction,
            character_id=character_id,
            owner_user_id=owner_user_id,
            state=current_state,
        )

    async def equip_selected(
        self,
        interaction: discord.Interaction,
        *,
        character_id: int,
        owner_user_id: int,
        current_state: BackpackPanelState,
    ) -> None:
        try:
            snapshot = self._load_snapshot(character_id=character_id, state=current_state)
        except BackpackPanelQueryServiceError as exc:
            await self.responder.send_private_error(interaction, message=str(exc))
            return
        normalized_state = self._normalize_state(state=current_state, snapshot=snapshot)
        selected_detail = snapshot.selected_detail
        if selected_detail is None:
            next_state = replace(
                normalized_state,
                action_note=BackpackActionNote(title="装配结果", lines=("请先从当前页下拉框中选择一个实例。",)),
            )
            await self._edit_panel(
                interaction,
                snapshot=snapshot,
                owner_user_id=owner_user_id,
                state=next_state,
            )
            return
        if not selected_detail.equip_action_enabled:
            next_state = replace(
                normalized_state,
                action_note=BackpackActionNote(title="装配结果", lines=("当前选中实例已处于装配状态。",)),
            )
            await self._edit_panel(
                interaction,
                snapshot=snapshot,
                owner_user_id=owner_user_id,
                state=next_state,
            )
            return

        try:
            if selected_detail.entry_kind is BackpackEntryKind.EQUIPMENT and selected_detail.equipment_item is not None:
                result = self._equip_item(
                    character_id=character_id,
                    equipment_item_id=selected_detail.equipment_item.item_id,
                )
                action_note = BackpackActionNote(title="装配结果", lines=self._build_equipment_equip_lines(result=result))
            elif selected_detail.entry_kind is BackpackEntryKind.SKILL and selected_detail.skill_item is not None:
                result = self._equip_skill_instance(
                    character_id=character_id,
                    skill_item_id=selected_detail.skill_item.item_id,
                )
                action_note = BackpackActionNote(title="装配结果", lines=self._build_skill_equip_lines(result=result))
            else:
                action_note = BackpackActionNote(title="装配结果", lines=("选中实例已失效，请重新选择。",))
        except (EquipmentServiceError, SkillLoadoutServiceError, BackpackPanelQueryServiceError) as exc:
            await self.responder.send_private_error(interaction, message=str(exc))
            return

        next_state = replace(normalized_state, action_note=action_note)
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
        state: BackpackPanelState,
    ) -> None:
        try:
            snapshot = self._load_snapshot(character_id=character_id, state=state)
        except BackpackPanelQueryServiceError as exc:
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
    def _normalize_state(*, state: BackpackPanelState, snapshot: BackpackPanelSnapshot) -> BackpackPanelState:
        next_selected_entry_key = state.selected_entry_key
        if next_selected_entry_key is not None and snapshot.selected_detail is None:
            next_selected_entry_key = None
        return replace(
            state,
            filter_id=snapshot.filter_id,
            page=snapshot.page,
            selected_entry_key=next_selected_entry_key,
        )

    def _load_character_id_by_discord_user_id(self, *, discord_user_id: str) -> int:
        with session_scope(self._session_factory) as session:
            services = self._service_bundle_factory(session)
            overview = services.character_panel_query_service.get_overview_by_discord_user_id(
                discord_user_id=discord_user_id,
            )
            return overview.character_id

    def _load_snapshot(self, *, character_id: int, state: BackpackPanelState) -> BackpackPanelSnapshot:
        with session_scope(self._session_factory) as session:
            services = self._service_bundle_factory(session)
            return services.backpack_panel_query_service.get_panel_snapshot(
                character_id=character_id,
                filter_id=state.filter_id,
                page=state.page,
                selected_entry_key=state.selected_entry_key,
            )

    def _equip_item(self, *, character_id: int, equipment_item_id: int) -> EquipmentEquipApplicationResult:
        with session_scope(self._session_factory) as session:
            services = self._service_bundle_factory(session)
            return services.equipment_service.equip_item(
                character_id=character_id,
                equipment_item_id=equipment_item_id,
            )

    def _equip_skill_instance(
        self,
        *,
        character_id: int,
        skill_item_id: int,
    ) -> SkillSlotEquipApplicationResult:
        with session_scope(self._session_factory) as session:
            services = self._service_bundle_factory(session)
            return services.skill_loadout_service.equip_skill_instance(
                character_id=character_id,
                skill_item_id=skill_item_id,
            )

    @staticmethod
    def _build_equipment_equip_lines(result: EquipmentEquipApplicationResult) -> tuple[str, ...]:
        lines = [
            f"已装配：{BackpackPanelPresenter._format_equipment_head(result.item)}",
            f"目标部位：{result.item.slot_name}",
        ]
        if result.previous_item is not None:
            lines.append(f"替换下旧装备：{BackpackPanelPresenter._format_equipment_head(result.previous_item)}")
        else:
            lines.append("该部位原先为空。")
        return tuple(lines)

    @staticmethod
    def _build_skill_equip_lines(result: SkillSlotEquipApplicationResult) -> tuple[str, ...]:
        slot_name = _format_skill_slot_name(result.slot_id)
        lines = [
            f"已装配槽位：{slot_name}",
            f"当前功法实例键：skill:{result.equipped_skill_item_id}",
        ]
        if result.previous_skill_item_id is not None and result.previous_skill_item_id != result.equipped_skill_item_id:
            lines.append(f"替换下旧功法实例：skill:{result.previous_skill_item_id}")
        elif result.previous_skill_item_id == result.equipped_skill_item_id:
            lines.append("当前选中实例本就是该槽位装配实例。")
        else:
            lines.append("该槽位此前为空。")
        return tuple(lines)

    async def _send_panel(
        self,
        interaction: discord.Interaction,
        *,
        snapshot: BackpackPanelSnapshot,
        owner_user_id: int,
        state: BackpackPanelState,
    ) -> None:
        payload = self._build_payload(snapshot=snapshot, owner_user_id=owner_user_id, state=state)
        await self.responder.send_message(interaction, payload=payload, visibility=PanelVisibility.PRIVATE)

    async def _edit_panel(
        self,
        interaction: discord.Interaction,
        *,
        snapshot: BackpackPanelSnapshot,
        owner_user_id: int,
        state: BackpackPanelState,
    ) -> None:
        payload = self._build_payload(snapshot=snapshot, owner_user_id=owner_user_id, state=state)
        await self.responder.edit_message(interaction, payload=payload)

    def _build_payload(
        self,
        *,
        snapshot: BackpackPanelSnapshot,
        owner_user_id: int,
        state: BackpackPanelState,
    ) -> PanelMessagePayload:
        view = BackpackPanelView(
            controller=self,
            owner_user_id=owner_user_id,
            snapshot=snapshot,
            state=state,
            timeout=self._panel_timeout,
        )
        embed = BackpackPanelPresenter.build_embed(snapshot=snapshot, state=state)
        return PanelMessagePayload(embed=embed, view=view)


def _format_skill_slot_name(slot_id: str) -> str:
    return {
        "main": "主修",
        "guard": "护体",
        "movement": "身法",
        "spirit": "神识",
    }.get(slot_id, slot_id)


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


__all__ = [
    "BackpackActionNote",
    "BackpackPanelController",
    "BackpackPanelPresenter",
    "BackpackPanelState",
    "BackpackPanelView",
]
