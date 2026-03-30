"""Discord 装备 / 法宝 / 功法私有面板。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

import discord
from sqlalchemy.orm import Session, sessionmaker

from application.character.panel_query_service import CharacterPanelQueryService, CharacterPanelQueryServiceError
from application.character.skill_loadout_service import (
    SkillLoadoutService,
    SkillLoadoutServiceError,
    SkillPathSwitchApplicationResult,
    SkillSlotEquipApplicationResult,
)
from application.equipment.equipment_service import (
    ArtifactNurtureApplicationResult,
    EquipmentDismantleApplicationResult,
    EquipmentEnhancementApplicationResult,
    EquipmentEquipApplicationResult,
    EquipmentItemSnapshot,
    EquipmentReforgeApplicationResult,
    EquipmentResourceLedgerEntry,
    EquipmentService,
    EquipmentServiceError,
    EquipmentUnequipApplicationResult,
    EquipmentWashApplicationResult,
)
from application.equipment.panel_query_service import (
    EquipmentCardSnapshot,
    EquipmentPanelQueryService,
    EquipmentPanelQueryServiceError,
    EquipmentPanelSnapshot,
    EquipmentSlotPanelSnapshot,
    format_equipment_affix_display_line,
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
    "damage_bonus_permille": "穿透",
    "damage_reduction_permille": "减伤",
    "counter_rate_permille": "反击",
    "control_bonus_permille": "控势",
    "control_resist_permille": "定心",
    "healing_power_permille": "疗愈",
    "shield_power_permille": "护盾",
    "penetration_permille": "穿透",
}
_RESOURCE_NAME_BY_ID = {
    "spirit_stone": "灵石",
    "enhancement_stone": "强化石",
    "enhancement_shard": "强化碎晶",
    "wash_dust": "洗炼尘",
    "spirit_sand": "灵砂",
    "spirit_pattern_stone": "灵纹石",
    "soul_binding_jade": "缚魂玉",
    "artifact_essence": "法宝精粹",
}
_RESOURCE_POLICY_NAME_BY_ID = {
    "conserve": "保守",
    "steady": "稳态",
    "burst": "爆发",
}
_STAGE_NAME_BY_ID = {
    "early": "初期",
    "middle": "中期",
    "mid": "中期",
    "late": "后期",
    "peak": "圆满",
    "perfect": "圆满",
    "perfected": "圆满",
}


class EquipmentPanelServiceBundle(Protocol):
    """装备面板所需的最小服务集合。"""

    character_panel_query_service: CharacterPanelQueryService
    equipment_panel_query_service: EquipmentPanelQueryService
    equipment_service: EquipmentService
    skill_loadout_service: SkillLoadoutService


class EquipmentPanelDisplayMode(StrEnum):
    """装备面板展示模式。"""

    HUB = "hub"
    SLOT_DETAIL = "slot_detail"
    SKILL_DETAIL = "skill_detail"


class EquipmentPendingAction(StrEnum):
    """装备详情页待执行动作。"""

    WASH = "wash"
    REFORGE = "reforge"
    DISMANTLE = "dismantle"


@dataclass(frozen=True, slots=True)
class EquipmentActionNote:
    """装备面板动作反馈。"""

    title: str
    lines: tuple[str, ...]


class EquipmentPanelPresenter:
    """负责把装备面板快照投影为 Discord Embed。"""

    @classmethod
    def build_embed(
        cls,
        *,
        snapshot: EquipmentPanelSnapshot,
        display_mode: EquipmentPanelDisplayMode,
        selected_slot_id: str | None = None,
        selected_candidate_item_id: int | None = None,
        action_note: EquipmentActionNote | None = None,
    ) -> discord.Embed:
        if display_mode is EquipmentPanelDisplayMode.SLOT_DETAIL:
            slot_panel = cls._require_slot_panel(snapshot=snapshot, slot_id=selected_slot_id)
            return cls._build_slot_detail_embed(
                snapshot=snapshot,
                slot_panel=slot_panel,
                selected_candidate_item_id=selected_candidate_item_id,
                action_note=action_note,
            )
        if display_mode is EquipmentPanelDisplayMode.SKILL_DETAIL:
            return cls._build_skill_detail_embed(snapshot=snapshot, action_note=action_note)
        return cls._build_hub_embed(snapshot=snapshot, action_note=action_note)

    @classmethod
    def _build_hub_embed(
        cls,
        *,
        snapshot: EquipmentPanelSnapshot,
        action_note: EquipmentActionNote | None,
    ) -> discord.Embed:
        embed = discord.Embed(
            title=f"{snapshot.skill_snapshot.character_name}｜装备",
            description="仅操作者可见",
            color=discord.Color.dark_gold(),
        )
        embed.add_field(name="🛡 已装备部位列表", value=cls._build_equipped_block(snapshot=snapshot), inline=False)
        if action_note is not None and action_note.lines:
            embed.add_field(name=action_note.title, value="\n".join(action_note.lines), inline=False)
        embed.set_footer(text="主面板仅保留部位总览")
        return embed

    @classmethod
    def _build_slot_detail_embed(
        cls,
        *,
        snapshot: EquipmentPanelSnapshot,
        slot_panel: EquipmentSlotPanelSnapshot,
        selected_candidate_item_id: int | None,
        action_note: EquipmentActionNote | None,
    ) -> discord.Embed:
        selected_candidate = cls._resolve_selected_candidate(
            slot_panel=slot_panel,
            selected_candidate_item_id=selected_candidate_item_id,
        )
        embed = discord.Embed(
            title=f"{snapshot.skill_snapshot.character_name}｜{slot_panel.slot_name}",
            description="仅操作者可见",
            color=discord.Color.orange(),
        )
        embed.add_field(
            name="🛡 当前装备",
            value=cls._format_current_equipped_item(slot_panel=slot_panel),
            inline=False,
        )
        embed.add_field(
            name="🎒 候选列表",
            value=cls._format_candidate_list(
                slot_panel=slot_panel,
                selected_candidate_item_id=None if selected_candidate is None else selected_candidate.item_id,
            ),
            inline=False,
        )
        embed.add_field(
            name="✨ 选中候选",
            value=cls._format_candidate_detail(slot_panel=slot_panel, selected_candidate=selected_candidate),
            inline=False,
        )
        if action_note is not None and action_note.lines:
            embed.add_field(name=action_note.title, value="\n".join(action_note.lines), inline=False)
        embed.set_footer(text=f"{slot_panel.slot_name}详情")
        return embed

    @classmethod
    def _build_skill_detail_embed(
        cls,
        *,
        snapshot: EquipmentPanelSnapshot,
        action_note: EquipmentActionNote | None,
    ) -> discord.Embed:
        skill = snapshot.skill_snapshot
        embed = discord.Embed(
            title=f"{skill.character_name}｜功法详情",
            description="仅操作者可见",
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name="当前装配",
            value=(
                f"境界阶段：{_format_realm_stage(realm_id=skill.realm_id, stage_id=skill.stage_id)}\n"
                f"主修体系：{skill.main_axis_name}\n"
                f"主修功法：{skill.main_skill.skill_name}｜{skill.main_skill.rank_name}｜{skill.main_skill.quality_name}\n"
                f"所属流派：{skill.main_skill.path_name}"
            ),
            inline=False,
        )
        embed.add_field(
            name="战斗画像",
            value=(
                f"主修体系摘要：{skill.axis_focus_summary}\n"
                f"战斗定位：{skill.combat_identity}\n"
                f"战斗流派：{skill.behavior_template_name}\n"
                f"资源倾向：{_format_resource_policy_name(skill.resource_policy)}\n"
                f"偏好场景：{cls._format_preferred_scene(skill.preferred_scene)}"
            ),
            inline=False,
        )
        embed.add_field(name="主修详情", value=cls._format_skill_slot_detail(skill.main_skill), inline=False)
        embed.add_field(name="辅助装配", value=cls._build_auxiliary_skill_block(skill), inline=False)
        if action_note is not None and action_note.lines:
            embed.add_field(name=action_note.title, value="\n".join(action_note.lines), inline=False)
        return embed

    @classmethod
    def _build_equipped_block(cls, *, snapshot: EquipmentPanelSnapshot) -> str:
        lines: list[str] = []
        for slot_panel in snapshot.slot_panels:
            extra_candidates = cls._count_extra_candidates(slot_panel=slot_panel)
            if slot_panel.equipped_item is None:
                lines.append(f"{slot_panel.slot_name}｜未装备｜候选 {extra_candidates}")
                continue
            item = slot_panel.equipped_item
            growth = f"祭{item.artifact_nurture_level}" if item.is_artifact else f"+{item.enhancement_level}"
            lines.append(f"{slot_panel.slot_name}｜{item.display_name} {growth}｜候选 {extra_candidates}")
        if not lines:
            return "暂无装备"
        return "```\n" + "\n".join(lines) + "\n```"

    @classmethod
    def _build_artifact_block(cls, *, snapshot: EquipmentPanelSnapshot) -> str:
        artifact_panel = cls._require_slot_panel(snapshot=snapshot, slot_id="artifact")
        item = artifact_panel.equipped_item
        if item is None:
            if not artifact_panel.candidate_items:
                return "当前没有法宝。"
            preview = artifact_panel.candidate_items[0]
            return (
                f"当前未装备法宝\n"
                f"候选预览：{cls._format_item_head(preview)}\n"
                f"关键属性：{cls._format_primary_stat_lines(preview, limit=2)}"
            )
        detail_lines = [
            f"名称：{cls._format_item_head(item)}",
            f"培养等级：{item.artifact_nurture_level}",
            f"共鸣：{item.resonance_name or '无'}",
            f"关键属性：{cls._format_primary_stat_lines(item, limit=2)}",
        ]
        if item.affixes:
            detail_lines.append(
                "关键词条：" + "｜".join(cls._format_affix_line(affix) for affix in item.affixes[:2])
            )
        return "\n".join(detail_lines)

    @classmethod
    def _build_skill_brief_block(cls, *, snapshot: EquipmentPanelSnapshot) -> str:
        skill = snapshot.skill_snapshot
        auxiliary_summary = "｜".join(
            f"{item.slot_name} {item.skill_name}"
            for item in skill.auxiliary_skills
        )
        return (
            f"主修：{skill.main_skill.skill_name}｜{skill.main_skill.rank_name}｜{skill.main_skill.quality_name}\n"
            f"流派：{skill.main_skill.path_name}\n"
            f"辅助：{auxiliary_summary}\n"
            f"战斗流派：{skill.behavior_template_name}"
        )

    @staticmethod
    def _format_skill_slot_detail(skill_slot) -> str:
        lines = [
            f"功法：{skill_slot.skill_name}",
            f"流派：{skill_slot.path_name}",
            f"阶级：{skill_slot.rank_name}｜品质：{skill_slot.quality_name}",
        ]
        if skill_slot.resolved_patch_ids:
            lines.append("流派加成：" + "｜".join(_format_patch_name(patch_id) for patch_id in skill_slot.resolved_patch_ids[:3]))
        return "\n".join(lines)

    @classmethod
    def _build_auxiliary_skill_block(cls, skill_snapshot) -> str:
        lines: list[str] = []
        for skill_slot in skill_snapshot.auxiliary_skills:
            line = (
                f"{skill_slot.slot_name}：{skill_slot.skill_name}｜{skill_slot.path_name}｜"
                f"{skill_slot.rank_name}｜{skill_slot.quality_name}"
            )
            if skill_slot.resolved_patch_ids:
                line += "｜流派加成 " + "｜".join(_format_patch_name(patch_id) for patch_id in skill_slot.resolved_patch_ids[:2])
            lines.append(line)
        return "\n".join(lines)

    @classmethod
    def _build_candidate_summary(cls, *, snapshot: EquipmentPanelSnapshot) -> str:
        lines: list[str] = []
        for slot_panel in snapshot.slot_panels:
            if not slot_panel.candidate_items:
                lines.append(f"{slot_panel.slot_name}：暂无候选")
                continue
            preview = slot_panel.candidate_items[0]
            lines.append(f"{slot_panel.slot_name}：{cls._format_item_head(preview)}")
        active_count = len(snapshot.collection.active_items)
        dismantled_count = len(snapshot.collection.dismantled_items)
        lines.append(f"背包活跃装备：{active_count}")
        lines.append(f"已分解记录：{dismantled_count}")
        lines.append(f"灵石：{snapshot.spirit_stone}")
        return "\n".join(lines)

    @classmethod
    def _build_latest_drop_block(cls, *, snapshot: EquipmentPanelSnapshot) -> str:
        latest_drop = snapshot.latest_drop
        if latest_drop is None:
            return "暂无装备相关掉落或获取记录。"
        lines = [
            f"来源：{latest_drop.source_label}",
            f"时间：{discord.utils.format_dt(latest_drop.occurred_at, style='R')}",
        ]
        if latest_drop.item_lines:
            lines.append("物品：" + "｜".join(latest_drop.item_lines[:3]))
        if latest_drop.currency_lines:
            lines.append("资源：" + "｜".join(latest_drop.currency_lines[:3]))
        return "\n".join(lines)

    @classmethod
    def _format_current_equipped_item(cls, *, slot_panel: EquipmentSlotPanelSnapshot) -> str:
        if slot_panel.equipped_card is None:
            return "当前未装备。"
        return cls._format_card(slot_panel.equipped_card)

    @classmethod
    def _format_candidate_list(
        cls,
        *,
        slot_panel: EquipmentSlotPanelSnapshot,
        selected_candidate_item_id: int | None,
    ) -> str:
        if not slot_panel.candidate_items:
            return "当前没有可尝试装备的候选。"
        lines: list[str] = []
        for index, item in enumerate(slot_panel.candidate_items[:8], start=1):
            prefix = ">" if item.item_id == selected_candidate_item_id else " "
            badge_line = (
                slot_panel.candidate_cards[index - 1].badge_line
                if index - 1 < len(slot_panel.candidate_cards)
                else f"{item.rank_name}｜{item.quality_name}"
            )
            lines.append(f"{prefix}{index:02d}. {item.display_name}")
            lines.append(f"   {badge_line}")
        return "```\n" + "\n".join(lines) + "\n```"

    @classmethod
    def _format_candidate_detail(
        cls,
        *,
        slot_panel: EquipmentSlotPanelSnapshot,
        selected_candidate: EquipmentItemSnapshot | None,
    ) -> str:
        if selected_candidate is None:
            return "尚未选择候选装备。"
        card = cls._resolve_selected_candidate_card(
            slot_panel=slot_panel,
            selected_candidate_item_id=selected_candidate.item_id,
        )
        if card is not None:
            return cls._format_card(card)
        fallback_card = EquipmentCardSnapshot(
            name=selected_candidate.display_name,
            badge_line=f"{selected_candidate.slot_name}｜{selected_candidate.rank_name}｜{selected_candidate.quality_name}",
            growth_line=(
                f"强化 +{selected_candidate.enhancement_level}"
                if not selected_candidate.is_artifact
                else f"强化 +{selected_candidate.enhancement_level}｜祭炼 {selected_candidate.artifact_nurture_level}"
            ),
            stat_lines=(cls._format_primary_stat_lines(selected_candidate, limit=3),),
            keyword_lines=tuple(cls._format_affix_line(affix) for affix in selected_candidate.affixes[:3]),
        )
        return cls._format_card(fallback_card)

    @classmethod
    def _resolve_selected_candidate(
        cls,
        *,
        slot_panel: EquipmentSlotPanelSnapshot,
        selected_candidate_item_id: int | None,
    ) -> EquipmentItemSnapshot | None:
        if not slot_panel.candidate_items:
            return None
        if selected_candidate_item_id is None:
            return slot_panel.candidate_items[0]
        for item in slot_panel.candidate_items:
            if item.item_id == selected_candidate_item_id:
                return item
        return slot_panel.candidate_items[0]

    @classmethod
    def _resolve_selected_candidate_card(
        cls,
        *,
        slot_panel: EquipmentSlotPanelSnapshot,
        selected_candidate_item_id: int | None,
    ) -> EquipmentCardSnapshot | None:
        if not slot_panel.candidate_cards:
            return None
        selected_candidate = cls._resolve_selected_candidate(
            slot_panel=slot_panel,
            selected_candidate_item_id=selected_candidate_item_id,
        )
        if selected_candidate is None:
            return None
        for index, item in enumerate(slot_panel.candidate_items):
            if item.item_id == selected_candidate.item_id and index < len(slot_panel.candidate_cards):
                return slot_panel.candidate_cards[index]
        return slot_panel.candidate_cards[0]

    @staticmethod
    def _count_extra_candidates(*, slot_panel: EquipmentSlotPanelSnapshot) -> int:
        if slot_panel.equipped_item is None:
            return len(slot_panel.candidate_items)
        return sum(1 for item in slot_panel.candidate_items if item.item_id != slot_panel.equipped_item.item_id)

    @classmethod
    def _format_card(cls, card: EquipmentCardSnapshot) -> str:
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
        return "\n".join(lines)

    @staticmethod
    def _format_item_head(item: EquipmentItemSnapshot) -> str:
        quality = item.quality_name
        enhance = f"+{item.enhancement_level}"
        nurture = f"｜祭炼 {item.artifact_nurture_level}" if item.is_artifact else ""
        return f"[{quality}] {item.display_name} {enhance}{nurture}"

    @classmethod
    def _format_primary_stat_lines(cls, item: EquipmentItemSnapshot, *, limit: int) -> str:
        stats = item.resolved_stats if item.resolved_stats else item.base_attributes
        if not stats:
            return "无"
        parts: list[str] = []
        for stat in stats[:limit]:
            parts.append(f"{cls._format_stat_name(stat.stat_id)} {cls._format_stat_value(stat.stat_id, stat.value)}")
        return "｜".join(parts)

    @staticmethod
    def _format_preferred_scene(preferred_scene: str) -> str:
        normalized = preferred_scene.strip()
        if not normalized:
            return "未定场景"
        return normalized.replace("PVP", "问道争锋").replace("PVE", "渊境征伐")

    @classmethod
    def _format_affix_line(cls, affix) -> str:
        return format_equipment_affix_display_line(affix)

    @staticmethod
    def _format_stat_name(stat_id: str) -> str:
        return _STAT_NAME_BY_ID.get(stat_id, stat_id)

    @staticmethod
    def _format_stat_value(stat_id: str, value: int) -> str:
        if stat_id.endswith("_permille"):
            return f"{value / 10:.1f}%"
        return str(value)

    @staticmethod
    def _require_slot_panel(
        *,
        snapshot: EquipmentPanelSnapshot,
        slot_id: str | None,
    ) -> EquipmentSlotPanelSnapshot:
        if slot_id is None:
            raise EquipmentPanelQueryServiceError("未选择装备部位。")
        for slot_panel in snapshot.slot_panels:
            if slot_panel.slot_id == slot_id:
                return slot_panel
        raise EquipmentPanelQueryServiceError(f"未找到装备部位：{slot_id}")


def _build_skill_instance_select_options(*, snapshot: EquipmentPanelSnapshot) -> list[discord.SelectOption]:
    options: list[discord.SelectOption] = []
    for skill_item in snapshot.skill_snapshot.owned_skills:
        if skill_item.equipped_slot_id == skill_item.slot_id:
            continue
        options.append(
            discord.SelectOption(
                label=f"{skill_item.slot_name}｜{skill_item.skill_name}"[:100],
                value=str(skill_item.item_id),
                description=(
                    f"{skill_item.path_name}｜{skill_item.rank_name}｜{skill_item.quality_name}"
                )[:100],
            )
        )
    return options[:_MAX_SELECT_OPTIONS]


def _format_skill_path_label(*, path_id: str | None) -> str:
    normalized_path_id = None if path_id is None else path_id.strip()
    if not normalized_path_id:
        return "未配置"
    for path in get_static_config().skill_paths.paths:
        if path.path_id == normalized_path_id:
            return str(path.name)
    return "未知流派"


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


def _format_realm_stage(*, realm_id: str, stage_id: str) -> str:
    return f"{_format_realm_name(realm_id)}·{_format_stage_name(stage_id)}"


def _format_realm_name(realm_id: str) -> str:
    normalized_realm_id = realm_id.strip()
    if not normalized_realm_id:
        return "未知境界"
    for realm in get_static_config().realm_progression.realms:
        if realm.realm_id == normalized_realm_id:
            return str(realm.name)
    return "未知境界"


def _format_stage_name(stage_id: str) -> str:
    normalized_stage_id = stage_id.strip()
    if not normalized_stage_id:
        return "未知阶段"
    for stage in get_static_config().realm_progression.stages:
        if stage.stage_id == normalized_stage_id:
            return str(stage.name)
    return "未知阶段"


def _format_resource_policy_name(resource_policy: str) -> str:
    normalized_resource_policy = resource_policy.strip()
    if not normalized_resource_policy:
        return "未定资源倾向"
    return _RESOURCE_POLICY_NAME_BY_ID.get(normalized_resource_policy, "未知资源倾向")


def _looks_like_internal_identifier(value: str) -> bool:
    return all(character.islower() or character.isdigit() or character == "_" for character in value)


class EquipmentSlotSelect(discord.ui.Select):
    """装备部位选择器。"""

    def __init__(self, *, slot_panels: tuple[EquipmentSlotPanelSnapshot, ...], selected_slot_id: str | None) -> None:
        options = [
            discord.SelectOption(
                label=slot_panel.slot_name,
                value=slot_panel.slot_id,
                description=(slot_panel.equipped_item.display_name[:90] if slot_panel.equipped_item else "查看该部位详情"),
                default=slot_panel.slot_id == selected_slot_id,
            )
            for slot_panel in slot_panels[:_MAX_SELECT_OPTIONS]
        ]
        super().__init__(
            placeholder="选择装备部位查看详情",
            min_values=1,
            max_values=1,
            options=options,
            row=2,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, EquipmentPanelView):
            await interaction.response.defer()
            return
        await view.controller.open_slot_detail(
            interaction,
            character_id=view.character_id,
            owner_user_id=view.owner_user_id,
            slot_id=self.values[0],
            selected_candidate_item_id=None,
        )


class EquipmentCandidateSelect(discord.ui.Select):
    """候选装备选择器。"""

    def __init__(self, *, slot_panel: EquipmentSlotPanelSnapshot, selected_candidate_item_id: int | None) -> None:
        options = []
        for item in slot_panel.candidate_items[:_MAX_SELECT_OPTIONS]:
            stats_preview = EquipmentPanelPresenter._format_primary_stat_lines(item, limit=2)
            options.append(
                discord.SelectOption(
                    label=item.display_name[:100],
                    value=str(item.item_id),
                    description=f"{item.quality_name}｜{stats_preview}"[:100],
                    default=item.item_id == selected_candidate_item_id,
                )
            )
        super().__init__(
            placeholder="选择候选装备",
            min_values=1,
            max_values=1,
            options=options,
            row=3,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, EquipmentPanelView):
            await interaction.response.defer()
            return
        if view.selected_slot_id is None:
            await interaction.response.defer()
            return
        await view.controller.open_slot_detail(
            interaction,
            character_id=view.character_id,
            owner_user_id=view.owner_user_id,
            slot_id=view.selected_slot_id,
            selected_candidate_item_id=int(self.values[0]),
        )


class SkillInstanceEquipSelect(discord.ui.Select):
    """功法实例装配选择器。"""

    def __init__(self, *, options: list[discord.SelectOption]) -> None:
        super().__init__(
            placeholder="选择要装配的功法实例",
            min_values=1,
            max_values=1,
            options=options,
            row=3,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, EquipmentPanelView):
            await interaction.response.defer()
            return
        if view.display_mode is not EquipmentPanelDisplayMode.SKILL_DETAIL:
            await interaction.response.defer()
            return
        await view.controller.equip_skill_instance(
            interaction,
            character_id=view.character_id,
            owner_user_id=view.owner_user_id,
            skill_item_id=int(self.values[0]),
        )


class EquipmentWashAffixSelect(discord.ui.Select):
    """洗炼锁定词条选择器。"""

    def __init__(
        self,
        *,
        equipped_item: EquipmentItemSnapshot,
        selected_locked_positions: tuple[int, ...],
    ) -> None:
        options: list[discord.SelectOption] = []
        for index, affix in enumerate(equipped_item.affixes[:_MAX_SELECT_OPTIONS], start=1):
            position = affix.position or index
            options.append(
                discord.SelectOption(
                    label=f"{position}号词条",
                    value=str(position),
                    description=EquipmentPanelPresenter._format_affix_line(affix)[:100],
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
        if not isinstance(view, EquipmentPanelView):
            await interaction.response.defer()
            return
        slot_panel = view._selected_slot_panel()
        if slot_panel is None or slot_panel.equipped_item is None:
            await interaction.response.defer()
            return
        view.selected_wash_locked_positions = tuple(sorted(int(value) for value in self.values))
        view.action_note = view._build_wash_prepare_note(slot_panel=slot_panel)
        view._sync_component_state()
        await interaction.response.edit_message(embed=view.build_embed(), view=view)


class EquipmentPanelView(discord.ui.View):
    """装备私有面板视图。"""

    def __init__(
        self,
        *,
        controller: EquipmentPanelController,
        owner_user_id: int,
        character_id: int,
        snapshot: EquipmentPanelSnapshot,
        display_mode: EquipmentPanelDisplayMode,
        selected_slot_id: str | None,
        selected_candidate_item_id: int | None,
        action_note: EquipmentActionNote | None = None,
        timeout: float = _PANEL_TIMEOUT_SECONDS,
    ) -> None:
        super().__init__(timeout=timeout)
        self.controller = controller
        self.owner_user_id = owner_user_id
        self.character_id = character_id
        self.snapshot = snapshot
        self.display_mode = display_mode
        self.selected_slot_id = selected_slot_id
        self.selected_candidate_item_id = selected_candidate_item_id
        self.action_note = action_note
        self.pending_action: EquipmentPendingAction | None = None
        self.selected_wash_locked_positions: tuple[int, ...] = ()
        if display_mode is EquipmentPanelDisplayMode.SKILL_DETAIL:
            for item in (
                self.try_equip,
                self.enhance_equipment,
                self.wash_equipment,
                self.reforge_equipment,
                self.dismantle_equipment,
                self.nurture_artifact,
                self.unequip_equipment,
            ):
                if item in self.children:
                    self.remove_item(item)
        else:
            self.add_item(EquipmentSlotSelect(slot_panels=snapshot.slot_panels, selected_slot_id=selected_slot_id))
        self._sync_dynamic_components()
        self._sync_component_state()

    def build_embed(self) -> discord.Embed:
        return EquipmentPanelPresenter.build_embed(
            snapshot=self.snapshot,
            display_mode=self.display_mode,
            selected_slot_id=self.selected_slot_id,
            selected_candidate_item_id=self.selected_candidate_item_id,
            action_note=self.action_note,
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_user_id:
            return True
        await self.controller.responder.send_private_error(interaction, message="该私有面板仅允许发起者操作。")
        return False

    def _selected_slot_panel(self) -> EquipmentSlotPanelSnapshot | None:
        if self.selected_slot_id is None:
            return None
        for slot_panel in self.snapshot.slot_panels:
            if slot_panel.slot_id == self.selected_slot_id:
                return slot_panel
        return None

    def _selected_equipped_item(self) -> EquipmentItemSnapshot | None:
        slot_panel = self._selected_slot_panel()
        return None if slot_panel is None else slot_panel.equipped_item

    def _resolve_selected_candidate_id(self, *, slot_panel: EquipmentSlotPanelSnapshot) -> int | None:
        selected_candidate = EquipmentPanelPresenter._resolve_selected_candidate(
            slot_panel=slot_panel,
            selected_candidate_item_id=self.selected_candidate_item_id,
        )
        return None if selected_candidate is None else selected_candidate.item_id

    def _build_wash_prepare_note(self, *, slot_panel: EquipmentSlotPanelSnapshot) -> EquipmentActionNote:
        item = slot_panel.equipped_item
        if item is None:
            return EquipmentActionNote(title="洗炼准备", lines=("当前部位没有已装备物品。",))
        lines = [f"当前装备：{EquipmentPanelPresenter._format_item_head(item)}"]
        if not item.affixes:
            lines.append("当前装备没有可锁定词条。")
            lines.append("再次点击“执行洗炼”后，将直接洗炼。")
            return EquipmentActionNote(title="洗炼准备", lines=tuple(lines))
        lines.append(
            "可锁定词条："
            + "｜".join(
                f"{affix.position or index}. {EquipmentPanelPresenter._format_affix_line(affix)}"
                for index, affix in enumerate(item.affixes[:4], start=1)
            )
        )
        lines.append(
            "当前锁定："
            + self.controller._build_locked_affix_summary(
                item=item,
                locked_affix_positions=self.selected_wash_locked_positions,
            )
        )
        lines.append("请在下方选择保留词条，再点击“执行洗炼”。")
        return EquipmentActionNote(title="洗炼准备", lines=tuple(lines))

    @staticmethod
    def _build_confirmation_note(*, title: str, item: EquipmentItemSnapshot, confirm_label: str) -> EquipmentActionNote:
        return EquipmentActionNote(
            title=title,
            lines=(
                f"当前装备：{EquipmentPanelPresenter._format_item_head(item)}",
                f"再次点击“{confirm_label}”后执行。",
                "刷新或切换部位可取消当前确认。",
            ),
        )

    def _set_pending_action(
        self,
        *,
        pending_action: EquipmentPendingAction,
        action_note: EquipmentActionNote,
        selected_wash_locked_positions: tuple[int, ...] = (),
    ) -> None:
        self.pending_action = pending_action
        self.action_note = action_note
        self.selected_wash_locked_positions = selected_wash_locked_positions
        self._sync_dynamic_components()
        self._sync_component_state()

    def _sync_dynamic_components(self) -> None:
        for item in list(self.children):
            if isinstance(item, (EquipmentCandidateSelect, EquipmentWashAffixSelect, SkillInstanceEquipSelect)):
                self.remove_item(item)
        slot_panel = self._selected_slot_panel()
        if slot_panel is not None:
            if (
                self.pending_action is EquipmentPendingAction.WASH
                and slot_panel.equipped_item is not None
                and slot_panel.equipped_item.affixes
            ):
                self.add_item(
                    EquipmentWashAffixSelect(
                        equipped_item=slot_panel.equipped_item,
                        selected_locked_positions=self.selected_wash_locked_positions,
                    )
                )
            elif slot_panel.candidate_items:
                self.add_item(
                    EquipmentCandidateSelect(
                        slot_panel=slot_panel,
                        selected_candidate_item_id=self._resolve_selected_candidate_id(slot_panel=slot_panel),
                    )
                )
        if self.display_mode is EquipmentPanelDisplayMode.SKILL_DETAIL:
            skill_instance_options = _build_skill_instance_select_options(snapshot=self.snapshot)
            if skill_instance_options:
                self.add_item(SkillInstanceEquipSelect(options=skill_instance_options))
        should_show_nurture = (
            self.display_mode is EquipmentPanelDisplayMode.SLOT_DETAIL
            and slot_panel is not None
            and slot_panel.slot_id == "artifact"
            and slot_panel.equipped_item is not None
        )
        if should_show_nurture and self.nurture_artifact not in self.children:
            self.add_item(self.nurture_artifact)
        if not should_show_nurture and self.nurture_artifact in self.children:
            self.remove_item(self.nurture_artifact)

    def _sync_component_state(self) -> None:
        slot_panel = self._selected_slot_panel()
        equipped_item = None if slot_panel is None else slot_panel.equipped_item
        candidate_id = None if slot_panel is None else self._resolve_selected_candidate_id(slot_panel=slot_panel)
        is_slot_detail = self.display_mode is EquipmentPanelDisplayMode.SLOT_DETAIL and slot_panel is not None
        has_pending_action = self.pending_action is not None
        self.try_equip.disabled = not (is_slot_detail and candidate_id is not None and not has_pending_action)
        self.enhance_equipment.disabled = not (is_slot_detail and equipped_item is not None and not has_pending_action)
        self.wash_equipment.disabled = not (
            is_slot_detail
            and equipped_item is not None
            and (self.pending_action is None or self.pending_action is EquipmentPendingAction.WASH)
        )
        self.reforge_equipment.disabled = not (
            is_slot_detail
            and equipped_item is not None
            and (self.pending_action is None or self.pending_action is EquipmentPendingAction.REFORGE)
        )
        self.dismantle_equipment.disabled = not (
            is_slot_detail
            and equipped_item is not None
            and (self.pending_action is None or self.pending_action is EquipmentPendingAction.DISMANTLE)
        )
        self.unequip_equipment.disabled = not (is_slot_detail and equipped_item is not None and not has_pending_action)
        if self.nurture_artifact in self.children:
            self.nurture_artifact.disabled = not (
                is_slot_detail
                and slot_panel is not None
                and slot_panel.slot_id == "artifact"
                and equipped_item is not None
                and not has_pending_action
            )
        self.show_overview.disabled = self.display_mode is EquipmentPanelDisplayMode.HUB
        self.show_artifact_detail.disabled = (
            self.display_mode is EquipmentPanelDisplayMode.SLOT_DETAIL and self.selected_slot_id == "artifact"
        )
        self.show_skill_detail.disabled = self.display_mode is EquipmentPanelDisplayMode.SKILL_DETAIL
        self.wash_equipment.label = "执行洗炼" if self.pending_action is EquipmentPendingAction.WASH else "洗炼"
        self.wash_equipment.style = (
            discord.ButtonStyle.success
            if self.pending_action is EquipmentPendingAction.WASH
            else discord.ButtonStyle.secondary
        )
        self.reforge_equipment.label = "确认重铸" if self.pending_action is EquipmentPendingAction.REFORGE else "重铸"
        self.reforge_equipment.style = (
            discord.ButtonStyle.danger
            if self.pending_action is EquipmentPendingAction.REFORGE
            else discord.ButtonStyle.secondary
        )
        self.dismantle_equipment.label = "确认分解" if self.pending_action is EquipmentPendingAction.DISMANTLE else "分解"

    @discord.ui.button(label="总览", style=discord.ButtonStyle.primary, row=0)
    async def show_overview(  # type: ignore[override]
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        await self.controller.open_panel(
            interaction,
            character_id=self.character_id,
            owner_user_id=self.owner_user_id,
        )

    @discord.ui.button(label="法宝详情", style=discord.ButtonStyle.secondary, row=0)
    async def show_artifact_detail(  # type: ignore[override]
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        await self.controller.open_slot_detail(
            interaction,
            character_id=self.character_id,
            owner_user_id=self.owner_user_id,
            slot_id="artifact",
            selected_candidate_item_id=None,
        )

    @discord.ui.button(label="功法详情", style=discord.ButtonStyle.secondary, row=0)
    async def show_skill_detail(  # type: ignore[override]
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        await self.controller.open_skill_detail(
            interaction,
            character_id=self.character_id,
            owner_user_id=self.owner_user_id,
        )

    @discord.ui.button(label="刷新", style=discord.ButtonStyle.secondary, row=0)
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
            display_mode=self.display_mode,
            selected_slot_id=self.selected_slot_id,
            selected_candidate_item_id=self.selected_candidate_item_id,
        )

    @discord.ui.button(label="尝试装备", style=discord.ButtonStyle.success, row=1)
    async def try_equip(  # type: ignore[override]
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        slot_panel = self._selected_slot_panel()
        if slot_panel is None:
            await interaction.response.defer()
            return
        candidate_id = self._resolve_selected_candidate_id(slot_panel=slot_panel)
        if candidate_id is None:
            await interaction.response.defer()
            return
        await self.controller.equip_selected_item(
            interaction,
            character_id=self.character_id,
            owner_user_id=self.owner_user_id,
            slot_id=slot_panel.slot_id,
            equipment_item_id=candidate_id,
        )

    @discord.ui.button(label="强化", style=discord.ButtonStyle.primary, row=1)
    async def enhance_equipment(  # type: ignore[override]
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        slot_panel = self._selected_slot_panel()
        if slot_panel is None or slot_panel.equipped_item is None:
            await interaction.response.defer()
            return
        await self.controller.enhance_equipped_item(
            interaction,
            character_id=self.character_id,
            owner_user_id=self.owner_user_id,
            slot_id=slot_panel.slot_id,
            selected_candidate_item_id=self.selected_candidate_item_id,
        )

    @discord.ui.button(label="洗炼", style=discord.ButtonStyle.secondary, row=1)
    async def wash_equipment(  # type: ignore[override]
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        slot_panel = self._selected_slot_panel()
        if slot_panel is None or slot_panel.equipped_item is None:
            await interaction.response.defer()
            return
        if self.pending_action is not EquipmentPendingAction.WASH:
            self._set_pending_action(
                pending_action=EquipmentPendingAction.WASH,
                action_note=self._build_wash_prepare_note(slot_panel=slot_panel),
            )
            await interaction.response.edit_message(embed=self.build_embed(), view=self)
            return
        await self.controller.wash_equipped_item(
            interaction,
            character_id=self.character_id,
            owner_user_id=self.owner_user_id,
            slot_id=slot_panel.slot_id,
            selected_candidate_item_id=self.selected_candidate_item_id,
            locked_affix_positions=self.selected_wash_locked_positions,
        )

    @discord.ui.button(label="重铸", style=discord.ButtonStyle.secondary, row=1)
    async def reforge_equipment(  # type: ignore[override]
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        item = self._selected_equipped_item()
        slot_panel = self._selected_slot_panel()
        if slot_panel is None or item is None:
            await interaction.response.defer()
            return
        if self.pending_action is not EquipmentPendingAction.REFORGE:
            self._set_pending_action(
                pending_action=EquipmentPendingAction.REFORGE,
                action_note=self._build_confirmation_note(
                    title="重铸确认",
                    item=item,
                    confirm_label="确认重铸",
                ),
            )
            await interaction.response.edit_message(embed=self.build_embed(), view=self)
            return
        await self.controller.reforge_equipped_item(
            interaction,
            character_id=self.character_id,
            owner_user_id=self.owner_user_id,
            slot_id=slot_panel.slot_id,
            selected_candidate_item_id=self.selected_candidate_item_id,
        )

    @discord.ui.button(label="分解", style=discord.ButtonStyle.danger, row=1)
    async def dismantle_equipment(  # type: ignore[override]
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        item = self._selected_equipped_item()
        slot_panel = self._selected_slot_panel()
        if slot_panel is None or item is None:
            await interaction.response.defer()
            return
        if self.pending_action is not EquipmentPendingAction.DISMANTLE:
            self._set_pending_action(
                pending_action=EquipmentPendingAction.DISMANTLE,
                action_note=self._build_confirmation_note(
                    title="分解确认",
                    item=item,
                    confirm_label="确认分解",
                ),
            )
            await interaction.response.edit_message(embed=self.build_embed(), view=self)
            return
        await self.controller.dismantle_equipped_item(
            interaction,
            character_id=self.character_id,
            owner_user_id=self.owner_user_id,
            slot_id=slot_panel.slot_id,
            selected_candidate_item_id=self.selected_candidate_item_id,
        )

    @discord.ui.button(label="法宝培养", style=discord.ButtonStyle.primary, row=4)
    async def nurture_artifact(  # type: ignore[override]
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        slot_panel = self._selected_slot_panel()
        if slot_panel is None or slot_panel.equipped_item is None:
            await interaction.response.defer()
            return
        await self.controller.nurture_equipped_artifact(
            interaction,
            character_id=self.character_id,
            owner_user_id=self.owner_user_id,
            slot_id=slot_panel.slot_id,
            selected_candidate_item_id=self.selected_candidate_item_id,
        )

    @discord.ui.button(label="卸下装备", style=discord.ButtonStyle.secondary, row=4)
    async def unequip_equipment(  # type: ignore[override]
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        slot_panel = self._selected_slot_panel()
        if slot_panel is None or slot_panel.equipped_item is None:
            await interaction.response.defer()
            return
        await self.controller.unequip_equipped_item(
            interaction,
            character_id=self.character_id,
            owner_user_id=self.owner_user_id,
            slot_id=slot_panel.slot_id,
            selected_candidate_item_id=self.selected_candidate_item_id,
        )


class EquipmentPanelController:
    """组织装备私有面板交互。"""

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
        """按 Discord 用户标识打开装备面板。"""
        try:
            character_id = self._load_character_id_by_discord_user_id(discord_user_id=str(interaction.user.id))
            snapshot = self._load_snapshot(character_id=character_id)
        except (CharacterPanelQueryServiceError, EquipmentPanelQueryServiceError) as exc:
            await self.responder.send_private_error(interaction, message=str(exc))
            return
        await self._send_panel(
            interaction,
            snapshot=snapshot,
            owner_user_id=interaction.user.id,
            display_mode=EquipmentPanelDisplayMode.HUB,
            selected_slot_id=None,
            selected_candidate_item_id=None,
        )

    async def open_panel(
        self,
        interaction: discord.Interaction,
        *,
        character_id: int,
        owner_user_id: int | None = None,
    ) -> None:
        """按角色标识打开装备总览面板。"""
        try:
            snapshot = self._load_snapshot(character_id=character_id)
        except EquipmentPanelQueryServiceError as exc:
            await self.responder.send_private_error(interaction, message=str(exc))
            return
        send_mode = PanelVisibility.PRIVATE if owner_user_id is None else None
        payload = self._build_payload(
            snapshot=snapshot,
            owner_user_id=interaction.user.id if owner_user_id is None else owner_user_id,
            display_mode=EquipmentPanelDisplayMode.HUB,
            selected_slot_id=None,
            selected_candidate_item_id=None,
            action_note=None,
        )
        if send_mode is PanelVisibility.PRIVATE:
            await self.responder.send_message(interaction, payload=payload, visibility=PanelVisibility.PRIVATE)
            return
        await self.responder.edit_message(interaction, payload=payload)

    async def open_slot_detail(
        self,
        interaction: discord.Interaction,
        *,
        character_id: int,
        owner_user_id: int,
        slot_id: str,
        selected_candidate_item_id: int | None,
        action_note: EquipmentActionNote | None = None,
    ) -> None:
        """打开指定部位详情。"""
        try:
            snapshot = self._load_snapshot(character_id=character_id)
        except EquipmentPanelQueryServiceError as exc:
            await self.responder.send_private_error(interaction, message=str(exc))
            return
        selected_candidate_item_id = self._normalize_selected_candidate_id(
            snapshot=snapshot,
            slot_id=slot_id,
            selected_candidate_item_id=selected_candidate_item_id,
        )
        await self._edit_panel(
            interaction,
            snapshot=snapshot,
            owner_user_id=owner_user_id,
            display_mode=EquipmentPanelDisplayMode.SLOT_DETAIL,
            selected_slot_id=slot_id,
            selected_candidate_item_id=selected_candidate_item_id,
            action_note=action_note,
        )

    async def open_skill_detail(
        self,
        interaction: discord.Interaction,
        *,
        character_id: int,
        owner_user_id: int,
    ) -> None:
        """打开功法详情页。"""
        try:
            snapshot = self._load_snapshot(character_id=character_id)
        except EquipmentPanelQueryServiceError as exc:
            await self.responder.send_private_error(interaction, message=str(exc))
            return
        await self._edit_panel(
            interaction,
            snapshot=snapshot,
            owner_user_id=owner_user_id,
            display_mode=EquipmentPanelDisplayMode.SKILL_DETAIL,
            selected_slot_id=None,
            selected_candidate_item_id=None,
        )

    async def equip_skill_instance(
        self,
        interaction: discord.Interaction,
        *,
        character_id: int,
        owner_user_id: int,
        skill_item_id: int,
    ) -> None:
        """按功法实例刷新当前装配。"""
        try:
            result = self._equip_skill_instance(character_id=character_id, skill_item_id=skill_item_id)
            snapshot = self._load_snapshot(character_id=character_id)
        except (EquipmentPanelQueryServiceError, SkillLoadoutServiceError) as exc:
            await self.responder.send_private_error(interaction, message=str(exc))
            return
        await self._edit_panel(
            interaction,
            snapshot=snapshot,
            owner_user_id=owner_user_id,
            display_mode=EquipmentPanelDisplayMode.SKILL_DETAIL,
            selected_slot_id=None,
            selected_candidate_item_id=None,
            action_note=EquipmentActionNote(
                title="功法装配结果",
                lines=self._build_skill_instance_equip_lines(result=result, snapshot=snapshot),
            ),
        )

    async def switch_skill_main_path(
        self,
        interaction: discord.Interaction,
        *,
        character_id: int,
        owner_user_id: int,
        main_path_id: str,
    ) -> None:
        """兼容入口：切换当前角色的主修流派。"""
        try:
            result = self._switch_skill_main_path(character_id=character_id, main_path_id=main_path_id)
            snapshot = self._load_snapshot(character_id=character_id)
        except (EquipmentPanelQueryServiceError, SkillLoadoutServiceError) as exc:
            await self.responder.send_private_error(interaction, message=str(exc))
            return
        await self._edit_panel(
            interaction,
            snapshot=snapshot,
            owner_user_id=owner_user_id,
            display_mode=EquipmentPanelDisplayMode.SKILL_DETAIL,
            selected_slot_id=None,
            selected_candidate_item_id=None,
            action_note=EquipmentActionNote(
                title="功法装配结果",
                lines=self._build_skill_path_switch_lines(result=result, snapshot=snapshot),
            ),
        )

    async def refresh_panel(
        self,
        interaction: discord.Interaction,
        *,
        character_id: int,
        owner_user_id: int,
        display_mode: EquipmentPanelDisplayMode,
        selected_slot_id: str | None,
        selected_candidate_item_id: int | None,
    ) -> None:
        """刷新当前装备面板。"""
        try:
            snapshot = self._load_snapshot(character_id=character_id)
        except EquipmentPanelQueryServiceError as exc:
            await self.responder.send_private_error(interaction, message=str(exc))
            return
        if display_mode is EquipmentPanelDisplayMode.SLOT_DETAIL and selected_slot_id is not None:
            selected_candidate_item_id = self._normalize_selected_candidate_id(
                snapshot=snapshot,
                slot_id=selected_slot_id,
                selected_candidate_item_id=selected_candidate_item_id,
            )
        else:
            selected_slot_id = None if display_mode is EquipmentPanelDisplayMode.SKILL_DETAIL else selected_slot_id
            selected_candidate_item_id = None if display_mode is not EquipmentPanelDisplayMode.SLOT_DETAIL else selected_candidate_item_id
        await self._edit_panel(
            interaction,
            snapshot=snapshot,
            owner_user_id=owner_user_id,
            display_mode=display_mode,
            selected_slot_id=selected_slot_id,
            selected_candidate_item_id=selected_candidate_item_id,
        )

    async def equip_selected_item(
        self,
        interaction: discord.Interaction,
        *,
        character_id: int,
        owner_user_id: int,
        slot_id: str,
        equipment_item_id: int,
    ) -> None:
        """尝试装备当前选中的候选装备。"""
        try:
            result = self._equip_item(character_id=character_id, equipment_item_id=equipment_item_id)
            snapshot = self._load_snapshot(character_id=character_id)
        except (EquipmentPanelQueryServiceError, EquipmentServiceError) as exc:
            await self.responder.send_private_error(interaction, message=str(exc))
            return
        action_note = EquipmentActionNote(
            title="装备结果",
            lines=self._build_equip_lines(result=result),
        )
        selected_candidate_item_id = self._normalize_selected_candidate_id(
            snapshot=snapshot,
            slot_id=slot_id,
            selected_candidate_item_id=None,
        )
        await self._edit_panel(
            interaction,
            snapshot=snapshot,
            owner_user_id=owner_user_id,
            display_mode=EquipmentPanelDisplayMode.SLOT_DETAIL,
            selected_slot_id=slot_id,
            selected_candidate_item_id=selected_candidate_item_id,
            action_note=action_note,
        )

    async def enhance_equipped_item(
        self,
        interaction: discord.Interaction,
        *,
        character_id: int,
        owner_user_id: int,
        slot_id: str,
        selected_candidate_item_id: int | None,
    ) -> None:
        """对当前已装备物品执行强化。"""
        try:
            equipped_item = self._require_equipped_item(character_id=character_id, slot_id=slot_id)
            result = self._enhance_equipment(character_id=character_id, equipment_item_id=equipped_item.item_id)
            snapshot = self._load_snapshot(character_id=character_id)
        except (EquipmentPanelQueryServiceError, EquipmentServiceError) as exc:
            await self.responder.send_private_error(interaction, message=str(exc))
            return
        selected_candidate_item_id = self._normalize_selected_candidate_id(
            snapshot=snapshot,
            slot_id=slot_id,
            selected_candidate_item_id=selected_candidate_item_id,
        )
        await self._edit_panel(
            interaction,
            snapshot=snapshot,
            owner_user_id=owner_user_id,
            display_mode=EquipmentPanelDisplayMode.SLOT_DETAIL,
            selected_slot_id=slot_id,
            selected_candidate_item_id=selected_candidate_item_id,
            action_note=EquipmentActionNote(
                title="强化结果",
                lines=self._build_enhancement_lines(result=result),
            ),
        )

    async def wash_equipped_item(
        self,
        interaction: discord.Interaction,
        *,
        character_id: int,
        owner_user_id: int,
        slot_id: str,
        selected_candidate_item_id: int | None,
        locked_affix_positions: tuple[int, ...],
    ) -> None:
        """对当前已装备物品执行洗炼。"""
        try:
            equipped_item = self._require_equipped_item(character_id=character_id, slot_id=slot_id)
            locked_affix_indices = self._normalize_locked_affix_indices(
                equipped_item=equipped_item,
                locked_affix_positions=locked_affix_positions,
            )
            result = self._wash_equipment(
                character_id=character_id,
                equipment_item_id=equipped_item.item_id,
                locked_affix_indices=locked_affix_indices,
            )
            snapshot = self._load_snapshot(character_id=character_id)
        except (EquipmentPanelQueryServiceError, EquipmentServiceError) as exc:
            await self.responder.send_private_error(interaction, message=str(exc))
            return
        selected_candidate_item_id = self._normalize_selected_candidate_id(
            snapshot=snapshot,
            slot_id=slot_id,
            selected_candidate_item_id=selected_candidate_item_id,
        )
        await self._edit_panel(
            interaction,
            snapshot=snapshot,
            owner_user_id=owner_user_id,
            display_mode=EquipmentPanelDisplayMode.SLOT_DETAIL,
            selected_slot_id=slot_id,
            selected_candidate_item_id=selected_candidate_item_id,
            action_note=EquipmentActionNote(
                title="洗炼结果",
                lines=self._build_wash_lines(
                    item=equipped_item,
                    locked_affix_positions=locked_affix_positions,
                    result=result,
                ),
            ),
        )

    async def reforge_equipped_item(
        self,
        interaction: discord.Interaction,
        *,
        character_id: int,
        owner_user_id: int,
        slot_id: str,
        selected_candidate_item_id: int | None,
    ) -> None:
        """对当前已装备物品执行重铸。"""
        try:
            equipped_item = self._require_equipped_item(character_id=character_id, slot_id=slot_id)
            result = self._reforge_equipment(character_id=character_id, equipment_item_id=equipped_item.item_id)
            snapshot = self._load_snapshot(character_id=character_id)
        except (EquipmentPanelQueryServiceError, EquipmentServiceError) as exc:
            await self.responder.send_private_error(interaction, message=str(exc))
            return
        selected_candidate_item_id = self._normalize_selected_candidate_id(
            snapshot=snapshot,
            slot_id=slot_id,
            selected_candidate_item_id=selected_candidate_item_id,
        )
        await self._edit_panel(
            interaction,
            snapshot=snapshot,
            owner_user_id=owner_user_id,
            display_mode=EquipmentPanelDisplayMode.SLOT_DETAIL,
            selected_slot_id=slot_id,
            selected_candidate_item_id=selected_candidate_item_id,
            action_note=EquipmentActionNote(
                title="重铸结果",
                lines=self._build_reforge_lines(result=result),
            ),
        )

    async def nurture_equipped_artifact(
        self,
        interaction: discord.Interaction,
        *,
        character_id: int,
        owner_user_id: int,
        slot_id: str,
        selected_candidate_item_id: int | None,
    ) -> None:
        """对当前已装备法宝执行培养。"""
        try:
            equipped_item = self._require_equipped_item(character_id=character_id, slot_id=slot_id)
            if not equipped_item.is_artifact:
                raise EquipmentPanelQueryServiceError("当前部位没有已装备法宝。")
            result = self._nurture_artifact(character_id=character_id, equipment_item_id=equipped_item.item_id)
            snapshot = self._load_snapshot(character_id=character_id)
        except (EquipmentPanelQueryServiceError, EquipmentServiceError) as exc:
            await self.responder.send_private_error(interaction, message=str(exc))
            return
        selected_candidate_item_id = self._normalize_selected_candidate_id(
            snapshot=snapshot,
            slot_id=slot_id,
            selected_candidate_item_id=selected_candidate_item_id,
        )
        await self._edit_panel(
            interaction,
            snapshot=snapshot,
            owner_user_id=owner_user_id,
            display_mode=EquipmentPanelDisplayMode.SLOT_DETAIL,
            selected_slot_id=slot_id,
            selected_candidate_item_id=selected_candidate_item_id,
            action_note=EquipmentActionNote(
                title="法宝培养结果",
                lines=self._build_nurture_lines(result=result),
            ),
        )

    async def dismantle_equipped_item(
        self,
        interaction: discord.Interaction,
        *,
        character_id: int,
        owner_user_id: int,
        slot_id: str,
        selected_candidate_item_id: int | None,
    ) -> None:
        """对当前已装备物品执行分解。"""
        try:
            equipped_item = self._require_equipped_item(character_id=character_id, slot_id=slot_id)
            result = self._dismantle_equipment(character_id=character_id, equipment_item_id=equipped_item.item_id)
            snapshot = self._load_snapshot(character_id=character_id)
        except (EquipmentPanelQueryServiceError, EquipmentServiceError) as exc:
            await self.responder.send_private_error(interaction, message=str(exc))
            return
        selected_candidate_item_id = self._normalize_selected_candidate_id(
            snapshot=snapshot,
            slot_id=slot_id,
            selected_candidate_item_id=selected_candidate_item_id,
        )
        await self._edit_panel(
            interaction,
            snapshot=snapshot,
            owner_user_id=owner_user_id,
            display_mode=EquipmentPanelDisplayMode.SLOT_DETAIL,
            selected_slot_id=slot_id,
            selected_candidate_item_id=selected_candidate_item_id,
            action_note=EquipmentActionNote(
                title="分解结果",
                lines=self._build_dismantle_lines(result=result),
            ),
        )

    async def unequip_equipped_item(
        self,
        interaction: discord.Interaction,
        *,
        character_id: int,
        owner_user_id: int,
        slot_id: str,
        selected_candidate_item_id: int | None,
    ) -> None:
        """卸下当前部位的已装备物品。"""
        try:
            result = self._unequip_item(character_id=character_id, equipped_slot_id=slot_id)
            snapshot = self._load_snapshot(character_id=character_id)
        except (EquipmentPanelQueryServiceError, EquipmentServiceError) as exc:
            await self.responder.send_private_error(interaction, message=str(exc))
            return
        selected_candidate_item_id = self._normalize_selected_candidate_id(
            snapshot=snapshot,
            slot_id=slot_id,
            selected_candidate_item_id=selected_candidate_item_id,
        )
        await self._edit_panel(
            interaction,
            snapshot=snapshot,
            owner_user_id=owner_user_id,
            display_mode=EquipmentPanelDisplayMode.SLOT_DETAIL,
            selected_slot_id=slot_id,
            selected_candidate_item_id=selected_candidate_item_id,
            action_note=EquipmentActionNote(
                title="卸下结果",
                lines=self._build_unequip_lines(result=result),
            ),
        )

    def _load_character_id_by_discord_user_id(self, *, discord_user_id: str) -> int:
        with session_scope(self._session_factory) as session:
            services = self._service_bundle_factory(session)
            overview = services.character_panel_query_service.get_overview_by_discord_user_id(
                discord_user_id=discord_user_id,
            )
            return overview.character_id

    def _load_snapshot(self, *, character_id: int) -> EquipmentPanelSnapshot:
        with session_scope(self._session_factory) as session:
            services = self._service_bundle_factory(session)
            return services.equipment_panel_query_service.get_panel_snapshot(character_id=character_id)

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

    def _switch_skill_main_path(
        self,
        *,
        character_id: int,
        main_path_id: str,
    ) -> SkillPathSwitchApplicationResult:
        with session_scope(self._session_factory) as session:
            services = self._service_bundle_factory(session)
            return services.skill_loadout_service.switch_main_path(
                character_id=character_id,
                main_path_id=main_path_id,
            )

    def _equip_item(self, *, character_id: int, equipment_item_id: int) -> EquipmentEquipApplicationResult:
        with session_scope(self._session_factory) as session:
            services = self._service_bundle_factory(session)
            return services.equipment_service.equip_item(
                character_id=character_id,
                equipment_item_id=equipment_item_id,
            )

    def _require_equipped_item(self, *, character_id: int, slot_id: str) -> EquipmentItemSnapshot:
        snapshot = self._load_snapshot(character_id=character_id)
        slot_panel = EquipmentPanelPresenter._require_slot_panel(snapshot=snapshot, slot_id=slot_id)
        if slot_panel.equipped_item is None:
            raise EquipmentPanelQueryServiceError(f"{slot_panel.slot_name}当前没有已装备物品。")
        return slot_panel.equipped_item

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

    async def _send_panel(
        self,
        interaction: discord.Interaction,
        *,
        snapshot: EquipmentPanelSnapshot,
        owner_user_id: int,
        display_mode: EquipmentPanelDisplayMode,
        selected_slot_id: str | None,
        selected_candidate_item_id: int | None,
        action_note: EquipmentActionNote | None = None,
    ) -> None:
        payload = self._build_payload(
            snapshot=snapshot,
            owner_user_id=owner_user_id,
            display_mode=display_mode,
            selected_slot_id=selected_slot_id,
            selected_candidate_item_id=selected_candidate_item_id,
            action_note=action_note,
        )
        await self.responder.send_message(interaction, payload=payload, visibility=PanelVisibility.PRIVATE)

    async def _edit_panel(
        self,
        interaction: discord.Interaction,
        *,
        snapshot: EquipmentPanelSnapshot,
        owner_user_id: int,
        display_mode: EquipmentPanelDisplayMode,
        selected_slot_id: str | None,
        selected_candidate_item_id: int | None,
        action_note: EquipmentActionNote | None = None,
    ) -> None:
        payload = self._build_payload(
            snapshot=snapshot,
            owner_user_id=owner_user_id,
            display_mode=display_mode,
            selected_slot_id=selected_slot_id,
            selected_candidate_item_id=selected_candidate_item_id,
            action_note=action_note,
        )
        await self.responder.edit_message(interaction, payload=payload)

    def _build_payload(
        self,
        *,
        snapshot: EquipmentPanelSnapshot,
        owner_user_id: int,
        display_mode: EquipmentPanelDisplayMode,
        selected_slot_id: str | None,
        selected_candidate_item_id: int | None,
        action_note: EquipmentActionNote | None,
    ) -> PanelMessagePayload:
        if display_mode is EquipmentPanelDisplayMode.SLOT_DETAIL and selected_slot_id is not None:
            selected_candidate_item_id = self._normalize_selected_candidate_id(
                snapshot=snapshot,
                slot_id=selected_slot_id,
                selected_candidate_item_id=selected_candidate_item_id,
            )
        else:
            selected_candidate_item_id = None if display_mode is not EquipmentPanelDisplayMode.SLOT_DETAIL else selected_candidate_item_id
        view = EquipmentPanelView(
            controller=self,
            owner_user_id=owner_user_id,
            character_id=snapshot.character_id,
            snapshot=snapshot,
            display_mode=display_mode,
            selected_slot_id=selected_slot_id,
            selected_candidate_item_id=selected_candidate_item_id,
            action_note=action_note,
            timeout=self._panel_timeout,
        )
        embed = EquipmentPanelPresenter.build_embed(
            snapshot=snapshot,
            display_mode=display_mode,
            selected_slot_id=selected_slot_id,
            selected_candidate_item_id=selected_candidate_item_id,
            action_note=action_note,
        )
        return PanelMessagePayload(embed=embed, view=view)

    @staticmethod
    def _normalize_selected_candidate_id(
        *,
        snapshot: EquipmentPanelSnapshot,
        slot_id: str,
        selected_candidate_item_id: int | None,
    ) -> int | None:
        slot_panel = EquipmentPanelPresenter._require_slot_panel(snapshot=snapshot, slot_id=slot_id)
        selected_candidate = EquipmentPanelPresenter._resolve_selected_candidate(
            slot_panel=slot_panel,
            selected_candidate_item_id=selected_candidate_item_id,
        )
        return None if selected_candidate is None else selected_candidate.item_id

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
            raise EquipmentPanelQueryServiceError("洗炼锁定词条已失效，请重新选择。")
        return tuple(position - 1 for position in normalized_positions)

    @staticmethod
    def _build_skill_instance_equip_lines(
        *,
        result: SkillSlotEquipApplicationResult,
        snapshot: EquipmentPanelSnapshot,
    ) -> tuple[str, ...]:
        equipped_skill = None
        for skill_item in snapshot.skill_snapshot.owned_skills:
            if skill_item.item_id == result.equipped_skill_item_id:
                equipped_skill = skill_item
                break
        if equipped_skill is None:
            equipped_skill = snapshot.skill_snapshot.main_skill
        previous_item_line = (
            "此前该槽位已有已装配功法"
            if result.previous_skill_item_id is not None
            else "此前该槽位未记录实例"
        )
        return (
            f"装配槽位：{equipped_skill.slot_name}",
            f"当前功法：{equipped_skill.skill_name}｜{equipped_skill.rank_name}｜{equipped_skill.quality_name}",
            previous_item_line,
            f"所属流派：{equipped_skill.path_name}",
            f"战斗流派：{snapshot.skill_snapshot.behavior_template_name}",
        )

    @staticmethod
    def _build_skill_path_switch_lines(
        *,
        result: SkillPathSwitchApplicationResult,
        snapshot: EquipmentPanelSnapshot,
    ) -> tuple[str, ...]:
        skill = snapshot.skill_snapshot
        return (
            f"主修流派：{_format_skill_path_label(path_id=result.previous_main_path_id)} → {skill.main_skill.path_name}",
            f"主修功法：{skill.main_skill.skill_name}｜{skill.main_skill.rank_name}｜{skill.main_skill.quality_name}",
            f"主修体系：{skill.main_axis_name}",
            f"战斗流派：{skill.behavior_template_name}",
        )

    @staticmethod
    def _build_equip_lines(*, result: EquipmentEquipApplicationResult) -> tuple[str, ...]:
        lines = [
            f"已尝试装备到部位：{result.equipped_slot_id}",
            f"当前装备：{result.item.display_name}",
            f"品质：{result.item.quality_name}",
        ]
        if result.previous_item is not None:
            lines.append(f"替换下来的装备：{result.previous_item.display_name}")
        else:
            lines.append("此前该部位未装备或仍为同一件装备")
        return tuple(lines)

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
            "保留词条：" + cls._build_locked_affix_summary(item=item, locked_affix_positions=locked_affix_positions),
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
            "物品已回到当前部位候选列表。",
        )

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
            parts.append(f"{position}. {cls._format_affix_brief(affix)}")
        return "｜".join(parts)

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
                f"{cls._format_resource_name(entry.resource_id)} {sign}{entry.quantity}（{entry.before_quantity}→{entry.after_quantity}）"
            )
        return ("资源变化：" + "｜".join(formatted),)

    @staticmethod
    def _format_affix_brief(affix) -> str:
        return EquipmentPanelPresenter._format_affix_line(affix)

    @staticmethod
    def _format_resource_name(resource_id: str) -> str:
        return _RESOURCE_NAME_BY_ID.get(resource_id, resource_id)

    @staticmethod
    def _format_datetime(value) -> str:
        return f"{discord.utils.format_dt(value, style='f')}｜{discord.utils.format_dt(value, style='R')}"


__all__ = [
    "EquipmentPanelController",
    "EquipmentPanelDisplayMode",
    "EquipmentPanelPresenter",
    "EquipmentPanelView",
]
