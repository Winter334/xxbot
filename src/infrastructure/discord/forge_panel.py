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
    ForgeOperationId,
    ForgePanelQueryService,
    ForgePanelQueryServiceError,
    ForgePanelSnapshot,
    ForgeTargetKind,
    ForgeTargetSnapshot,
)
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

    selected_slot_id: str | None = None
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
        selected_target = _resolve_selected_target(snapshot=snapshot, selected_slot_id=state.selected_slot_id)
        embed = discord.Embed(
            title=f"{snapshot.character_name}｜锻造",
            description="仅操作者可见",
            color=discord.Color.dark_gold(),
        )
        embed.add_field(name="培养资源", value=cls._build_resource_block(snapshot=snapshot), inline=False)
        embed.add_field(name="当前目标", value=cls._build_target_overview_block(target=selected_target), inline=False)
        embed.add_field(name="目标详情", value=cls._build_target_detail_block(target=selected_target), inline=False)
        embed.add_field(name="可执行操作", value=cls._build_operation_block(target=selected_target, state=state), inline=False)
        if state.action_note is not None and state.action_note.lines:
            embed.add_field(name=state.action_note.title, value="\n".join(state.action_note.lines), inline=False)
        embed.set_footer(text="锻造只负责资源栏、已装备目标选择与成长操作；背包负责浏览与装配")
        return embed

    @staticmethod
    def _build_resource_block(*, snapshot: ForgePanelSnapshot) -> str:
        if not snapshot.resources.entries:
            return "当前没有可显示的锻造资源。"
        lines: list[str] = []
        row_parts: list[str] = []
        for index, entry in enumerate(snapshot.resources.entries, start=1):
            row_parts.append(f"{entry.resource_name}：{entry.quantity}")
            if index % 4 == 0:
                lines.append("｜".join(row_parts))
                row_parts = []
        if row_parts:
            lines.append("｜".join(row_parts))
        return "\n".join(lines)

    @classmethod
    def _build_target_overview_block(cls, *, target: ForgeTargetSnapshot | None) -> str:
        if target is None:
            return "当前没有可选锻造目标。"
        target_kind_text = "装备 / 法宝" if target.target_kind is ForgeTargetKind.EQUIPMENT else "功法槽位"
        if target.target_kind is ForgeTargetKind.EQUIPMENT:
            current_target = target.equipped_item.display_name if target.equipped_item is not None else "暂无已装备目标"
        else:
            current_target = "无"
            if target.equipped_skill is not None:
                current_target = f"{target.equipped_skill.skill_name}｜{target.equipped_skill.path_name}"
        return "\n".join(
            (
                f"目标类别：{target_kind_text}",
                f"当前槽位：{target.slot_name}",
                f"定位说明：{target.core_role}",
                f"当前目标：{current_target}",
            )
        )

    @classmethod
    def _build_target_detail_block(cls, *, target: ForgeTargetSnapshot | None) -> str:
        if target is None:
            return "当前没有可选锻造目标。"
        if target.target_kind is ForgeTargetKind.SKILL:
            return cls._build_skill_detail_block(target=target)
        return cls._build_equipment_detail_block(target=target)

    @classmethod
    def _build_equipment_detail_block(cls, *, target: ForgeTargetSnapshot) -> str:
        if target.equipped_item is None:
            return "\n".join(
                (
                    f"槽位：{target.slot_name}",
                    f"定位：{target.core_role}",
                    target.action_status_text,
                )
            )
        item = target.equipped_item
        lines = [
            f"当前装备：{cls._format_equipment_head(item)}",
            f"部位：{item.slot_name}",
            f"底材：{item.template_name}",
            f"阶数 / 品质：{item.rank_name}｜{item.quality_name}",
            f"强化：+{item.enhancement_level}",
            f"主要属性：{cls._format_primary_stat_lines(item, limit=3)}",
        ]
        if item.is_artifact:
            lines.append(f"祭炼：{item.artifact_nurture_level}")
            lines.append(f"共鸣：{item.resonance_name or '无'}")
        if item.affixes:
            lines.append("关键词条：" + "｜".join(cls._format_affix_line(affix) for affix in item.affixes[:3]))
        return "\n".join(lines)

    @classmethod
    def _build_skill_detail_block(cls, *, target: ForgeTargetSnapshot) -> str:
        skill = target.equipped_skill
        if skill is None:
            return "\n".join((f"槽位：{target.slot_name}", target.action_status_text))
        lines = [
            f"当前功法：{skill.skill_name}",
            f"槽位：{skill.slot_name}",
            f"流派：{skill.path_name}",
            f"阶数 / 品质：{skill.rank_name}｜{skill.quality_name}",
        ]
        if skill.resolved_patch_ids:
            lines.append("流派加成：" + "｜".join(skill.resolved_patch_ids[:3]))
        lines.append(target.action_status_text)
        return "\n".join(lines)

    @classmethod
    def _build_operation_block(cls, *, target: ForgeTargetSnapshot | None, state: ForgePanelState) -> str:
        if target is None:
            return "当前没有可执行的锻造操作。"
        if target.target_kind is ForgeTargetKind.SKILL:
            return target.action_status_text
        supported_operations = "｜".join(_OPERATION_NAME_BY_ID[operation_id] for operation_id in target.supported_operations)
        if target.equipped_item is None:
            return "\n".join((f"可用操作：{supported_operations}", target.action_status_text))
        lines = [
            f"当前模式：{cls._format_pending_action(state.pending_action)}",
            f"可用操作：{supported_operations}",
        ]
        if state.pending_action is ForgePendingAction.WASH:
            lines.append(
                "锁定词条："
                + cls._build_locked_affix_summary(
                    item=target.equipped_item,
                    locked_affix_positions=state.selected_locked_affix_positions,
                )
            )
            lines.append("已进入洗炼准备态；可在下拉框保留词条后再次点击“执行洗炼”。")
        elif state.pending_action is ForgePendingAction.REFORGE:
            lines.append("已进入重铸确认态；再次点击“确认重铸”后执行。")
        elif state.pending_action is ForgePendingAction.DISMANTLE:
            lines.append("已进入分解确认态；再次点击“确认分解”后执行。")
        else:
            lines.append(target.action_status_text)
        return "\n".join(lines)

    @staticmethod
    def _format_pending_action(pending_action: ForgePendingAction) -> str:
        return {
            ForgePendingAction.NONE: "查看中",
            ForgePendingAction.WASH: "洗炼准备",
            ForgePendingAction.REFORGE: "重铸确认",
            ForgePendingAction.DISMANTLE: "分解确认",
        }[pending_action]

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
        return f"{affix.affix_name}({affix.tier_name}) {cls._format_stat_value(affix.stat_id, affix.value)}"

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


class ForgeTargetSelect(discord.ui.Select):
    """锻造目标选择器。"""

    def __init__(self, *, snapshot: ForgePanelSnapshot, state: ForgePanelState) -> None:
        options = [
            discord.SelectOption(
                label=target.slot_name[:100],
                value=target.slot_id,
                description=_build_target_option_description(target=target)[:100],
                default=target.slot_id == state.selected_slot_id,
            )
            for target in snapshot.targets[:_MAX_SELECT_OPTIONS]
        ]
        super().__init__(
            placeholder="选择锻造目标",
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
        await view.controller.select_target(
            interaction,
            character_id=view.character_id,
            owner_user_id=view.owner_user_id,
            current_state=view.state,
            slot_id=self.values[0],
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
            row=3,
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
        self.add_item(ForgeTargetSelect(snapshot=snapshot, state=state))
        selected_target = self._selected_target()
        if (
            selected_target is not None
            and selected_target.target_kind is ForgeTargetKind.EQUIPMENT
            and selected_target.equipped_item is not None
            and state.pending_action is ForgePendingAction.WASH
            and selected_target.equipped_item.affixes
        ):
            self.add_item(
                ForgeWashAffixSelect(
                    item=selected_target.equipped_item,
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
        return _resolve_selected_target(snapshot=self.snapshot, selected_slot_id=self.state.selected_slot_id)

    def _sync_component_state(self) -> None:
        selected_target = self._selected_target()
        has_equipped_target = (
            selected_target is not None
            and selected_target.target_kind is ForgeTargetKind.EQUIPMENT
            and selected_target.equipped_item is not None
        )
        can_enhance = has_equipped_target and ForgeOperationId.ENHANCE in selected_target.supported_operations
        can_wash = has_equipped_target and ForgeOperationId.WASH in selected_target.supported_operations
        can_reforge = has_equipped_target and ForgeOperationId.REFORGE in selected_target.supported_operations
        can_dismantle = has_equipped_target and ForgeOperationId.DISMANTLE in selected_target.supported_operations
        can_nurture = has_equipped_target and ForgeOperationId.NURTURE in selected_target.supported_operations
        can_unequip = has_equipped_target and ForgeOperationId.UNEQUIP in selected_target.supported_operations
        has_pending_action = self.state.pending_action is not ForgePendingAction.NONE

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
        self.nurture_target.disabled = not (can_nurture and not has_pending_action)
        self.unequip_target.disabled = not (can_unequip and not has_pending_action)

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

    @discord.ui.button(label="刷新", style=discord.ButtonStyle.primary, row=1)
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

    @discord.ui.button(label="强化", style=discord.ButtonStyle.primary, row=1)
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

    @discord.ui.button(label="洗炼", style=discord.ButtonStyle.secondary, row=1)
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

    @discord.ui.button(label="重铸", style=discord.ButtonStyle.secondary, row=1)
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

    @discord.ui.button(label="分解", style=discord.ButtonStyle.danger, row=1)
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

    @discord.ui.button(label="法宝培养", style=discord.ButtonStyle.primary, row=2)
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

    @discord.ui.button(label="卸下装备", style=discord.ButtonStyle.secondary, row=2)
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
            snapshot = self._load_snapshot(character_id=character_id)
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
            snapshot = self._load_snapshot(character_id=character_id)
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

    async def select_target(
        self,
        interaction: discord.Interaction,
        *,
        character_id: int,
        owner_user_id: int,
        current_state: ForgePanelState,
        slot_id: str,
    ) -> None:
        next_state = replace(
            current_state,
            selected_slot_id=slot_id,
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
            snapshot = self._load_snapshot(character_id=character_id)
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
            snapshot = self._load_snapshot(character_id=character_id)
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
            snapshot = self._load_snapshot(character_id=character_id)
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
            snapshot = self._load_snapshot(character_id=character_id)
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
            snapshot = self._load_snapshot(character_id=character_id)
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
            snapshot = self._load_snapshot(character_id=character_id)
            normalized_state = self._normalize_state(state=current_state, snapshot=snapshot)
            target = self._require_equipment_target(snapshot=snapshot, state=normalized_state)
            item = self._require_equipped_item(target=target)
            if not item.is_artifact or target.slot_id != "artifact":
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
            snapshot = self._load_snapshot(character_id=character_id)
            normalized_state = self._normalize_state(state=current_state, snapshot=snapshot)
            target = self._require_equipment_target(snapshot=snapshot, state=normalized_state)
            self._require_equipped_item(target=target)
            result = self._unequip_item(character_id=character_id, equipped_slot_id=target.slot_id)
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
            snapshot = self._load_snapshot(character_id=character_id)
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
        valid_slot_ids = {target.slot_id for target in snapshot.targets}
        selected_slot_id = state.selected_slot_id if state.selected_slot_id in valid_slot_ids else _resolve_default_slot_id(snapshot=snapshot)
        selected_target = _resolve_selected_target(snapshot=snapshot, selected_slot_id=selected_slot_id)
        pending_action = state.pending_action
        selected_locked_affix_positions = state.selected_locked_affix_positions
        action_note = state.action_note
        if (
            selected_target is None
            or selected_target.target_kind is ForgeTargetKind.SKILL
            or selected_target.equipped_item is None
        ):
            pending_action = ForgePendingAction.NONE
            selected_locked_affix_positions = ()
        if state.selected_slot_id != selected_slot_id and pending_action is ForgePendingAction.NONE:
            action_note = state.action_note
        if state.selected_slot_id != selected_slot_id and state.pending_action is not ForgePendingAction.NONE:
            action_note = None
        return replace(
            state,
            selected_slot_id=selected_slot_id,
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

    def _load_snapshot(self, *, character_id: int) -> ForgePanelSnapshot:
        with session_scope(self._session_factory) as session:
            services = self._service_bundle_factory(session)
            return services.forge_panel_query_service.get_panel_snapshot(character_id=character_id)

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
        target = _resolve_selected_target(snapshot=snapshot, selected_slot_id=state.selected_slot_id)
        if target is None:
            raise ForgePanelQueryServiceError("当前没有可操作的锻造目标。")
        if target.target_kind is ForgeTargetKind.SKILL:
            raise ForgePanelQueryServiceError(target.action_status_text)
        return target

    @staticmethod
    def _require_equipped_item(*, target: ForgeTargetSnapshot) -> EquipmentItemSnapshot:
        if target.equipped_item is None:
            raise ForgePanelQueryServiceError(target.action_status_text)
        return target.equipped_item

    @staticmethod
    def _build_wash_prepare_note(
        *,
        item: EquipmentItemSnapshot,
        locked_affix_positions: tuple[int, ...],
    ) -> ForgeActionNote:
        lines = [f"当前装备：{ForgePanelPresenter._format_equipment_head(item)}"]
        if not item.affixes:
            lines.append("当前装备没有可锁定词条。")
            lines.append("再次点击“执行洗炼”后，将直接洗炼。")
            return ForgeActionNote(title="洗炼准备", lines=tuple(lines))
        lines.append(
            "可锁定词条："
            + "｜".join(
                f"{affix.position or index}. {ForgePanelPresenter._format_affix_line(affix)}"
                for index, affix in enumerate(item.affixes[:4], start=1)
            )
        )
        lines.append(
            "当前锁定："
            + ForgePanelPresenter._build_locked_affix_summary(
                item=item,
                locked_affix_positions=locked_affix_positions,
            )
        )
        lines.append("请在下方选择保留词条，再点击“执行洗炼”。")
        return ForgeActionNote(title="洗炼准备", lines=tuple(lines))

    @staticmethod
    def _build_confirmation_note(*, title: str, item: EquipmentItemSnapshot, confirm_label: str) -> ForgeActionNote:
        return ForgeActionNote(
            title=title,
            lines=(
                f"当前目标：{ForgePanelPresenter._format_equipment_head(item)}",
                f"再次点击“{confirm_label}”后执行。",
                "刷新或切换目标可取消当前确认。",
            ),
        )

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
            f"当前装备：{result.item.display_name}",
            f"强化等级：+{result.previous_level} → +{result.target_level}",
            f"结果：{'成功' if result.success else '失败'}｜成功率：{float(result.success_rate) * 100:.1f}%",
        ]
        if result.added_affixes:
            lines.append("新增词条：" + "｜".join(cls._format_affix_brief(affix) for affix in result.added_affixes[:3]))
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
            f"当前装备：{result.item.display_name}",
            "保留词条："
            + ForgePanelPresenter._build_locked_affix_summary(
                item=item,
                locked_affix_positions=locked_affix_positions,
            ),
        ]
        if result.rerolled_affixes:
            lines.append("重洗词条：" + "｜".join(cls._format_affix_brief(affix) for affix in result.rerolled_affixes[:3]))
        else:
            lines.append("重洗词条：本次没有新的词条变化。")
        lines.extend(cls._build_resource_change_lines(resource_changes=result.resource_changes))
        return tuple(lines)

    @classmethod
    def _build_reforge_lines(
        cls,
        *,
        result: EquipmentReforgeApplicationResult,
    ) -> tuple[str, ...]:
        lines = [
            f"当前装备：{result.item.display_name}",
            f"重铸前底材：{result.previous_template_id}",
            f"重铸后底材：{result.item.template_name}",
        ]
        if result.previous_affixes:
            lines.append("重铸前词条：" + "｜".join(cls._format_affix_brief(affix) for affix in result.previous_affixes[:3]))
        lines.extend(cls._build_resource_change_lines(resource_changes=result.resource_changes))
        return tuple(lines)

    @classmethod
    def _build_nurture_lines(
        cls,
        *,
        result: ArtifactNurtureApplicationResult,
    ) -> tuple[str, ...]:
        lines = [
            f"当前法宝：{result.item.display_name}",
            f"培养等级：{result.previous_level} → {result.target_level}",
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
            f"结算时间：{cls._format_datetime(result.settled_at)}",
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
            formatted.append(
                f"{_format_resource_name(entry.resource_id)} {sign}{entry.quantity}（{entry.before_quantity}→{entry.after_quantity}）"
            )
        return ("资源变化：" + "｜".join(formatted),)

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


def _resolve_default_slot_id(*, snapshot: ForgePanelSnapshot) -> str | None:
    for target in snapshot.targets:
        if target.target_kind is ForgeTargetKind.EQUIPMENT and target.equipped_item is not None:
            return target.slot_id
    if not snapshot.targets:
        return None
    return snapshot.targets[0].slot_id


def _resolve_selected_target(*, snapshot: ForgePanelSnapshot, selected_slot_id: str | None) -> ForgeTargetSnapshot | None:
    if selected_slot_id is None:
        return None
    for target in snapshot.targets:
        if target.slot_id == selected_slot_id:
            return target
    return None


def _build_target_option_description(*, target: ForgeTargetSnapshot) -> str:
    if target.target_kind is ForgeTargetKind.EQUIPMENT:
        if target.equipped_item is None:
            return "暂无已装备目标"
        return f"{target.equipped_item.display_name}｜强化 +{target.equipped_item.enhancement_level}"
    if target.equipped_skill is None:
        return "功法目标未就绪"
    return f"{target.equipped_skill.skill_name}｜未开放培养"


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
