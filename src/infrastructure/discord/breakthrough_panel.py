"""Discord 突破秘境私有入口与结算面板。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

import discord
from sqlalchemy.orm import Session, sessionmaker

from application.breakthrough import BreakthroughTrialChallengeResult, BreakthroughTrialService, BreakthroughTrialServiceError
from application.breakthrough.panel_service import (
    BreakthroughPanelService,
    BreakthroughPanelServiceError,
    BreakthroughPanelSnapshot,
    BreakthroughRecentSettlementSnapshot,
)
from application.character import (
    BreakthroughExecutionResult,
    CharacterProgressionService,
    CharacterProgressionServiceError,
)
from application.character.panel_query_service import CharacterPanelQueryService, CharacterPanelQueryServiceError
from infrastructure.db.session import session_scope
from infrastructure.discord.character_panel import (
    DiscordInteractionVisibilityResponder,
    PanelMessagePayload,
    PanelVisibility,
)

_PANEL_TIMEOUT_SECONDS = 20 * 60
_PUBLIC_SPIRIT_STONE_THRESHOLD = 2000
_PUBLIC_HIGH_VALUE_ITEM_IDS = frozenset({"artifact_essence", "soul_binding_jade"})
_SETTLEMENT_NAME_BY_VALUE = {
    "defeat": "试炼失败",
    "first_clear": "首次通关",
    "repeat_clear": "重复通关",
}
_PROGRESS_STATUS_NAME_BY_VALUE = {
    None: "无记录",
    "failed": "未通关",
    "cleared": "已通关",
}
_REWARD_DIRECTION_NAME_BY_VALUE = {
    None: "无",
    "spirit_stone": "灵石补口",
    "enhancement_material": "强化材料补口",
    "reforge_material": "洗炼材料补口",
    "comprehension_material": "参悟辅材补口",
    "artifact_material": "法宝培养补口",
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
    "qi_condensation_grass": "凝气草",
    "foundation_pill": "筑基丹",
    "core_crystal": "金丹晶核",
    "nascent_soul_lotus": "元婴莲",
    "deity_heart_incense": "化神心香",
    "void_breaking_stone": "破虚石",
    "body_refining_marble": "合体玄玉",
    "great_vehicle_golden_leaf": "大乘金叶",
    "tribulation_guiding_talisman": "引劫符",
}


class BreakthroughDisplayMode(StrEnum):
    """突破秘境私有面板展示模式。"""

    HUB = "hub"
    SETTLEMENT = "settlement"


class BreakthroughPanelServiceBundle(Protocol):
    """突破秘境面板所需的最小服务集合。"""

    character_panel_query_service: CharacterPanelQueryService
    breakthrough_panel_service: BreakthroughPanelService
    breakthrough_trial_service: BreakthroughTrialService
    character_progression_service: CharacterProgressionService


@dataclass(frozen=True, slots=True)
class BreakthroughActionNote:
    """突破秘境面板动作反馈。"""

    title: str
    lines: tuple[str, ...]


class BreakthroughPanelPresenter:
    """负责把突破秘境聚合快照投影为 Discord Embed。"""

    @classmethod
    def build_hub_embed(
        cls,
        *,
        snapshot: BreakthroughPanelSnapshot,
        selected_mapping_id: str | None,
        action_note: BreakthroughActionNote | None = None,
    ) -> discord.Embed:
        embed = discord.Embed(
            title=f"{snapshot.overview.character_name}｜突破秘境",
            description="仅操作者可见",
            color=discord.Color.dark_blue(),
        )
        embed.add_field(name="当前突破状态", value=cls._build_status_block(snapshot=snapshot), inline=False)
        embed.add_field(name="突破资格与前置", value=cls._build_precheck_block(snapshot=snapshot), inline=False)
        embed.add_field(
            name="当前试炼",
            value=cls._build_selected_trial_block(snapshot=snapshot, selected_mapping_id=selected_mapping_id),
            inline=False,
        )
        embed.add_field(name="分组概览", value=cls._build_group_overview_block(snapshot=snapshot), inline=False)
        embed.add_field(name="最近一次结算摘要", value=cls._build_recent_settlement_summary(snapshot=snapshot), inline=False)
        if action_note is not None and action_note.lines:
            embed.add_field(name=action_note.title, value="\n".join(action_note.lines), inline=False)
        footer_text = _build_trial_footer(snapshot=snapshot, selected_mapping_id=selected_mapping_id)
        embed.set_footer(text=footer_text)
        return embed

    @classmethod
    def build_settlement_embed(
        cls,
        *,
        snapshot: BreakthroughPanelSnapshot,
        selected_mapping_id: str | None,
        action_note: BreakthroughActionNote | None = None,
    ) -> discord.Embed:
        recent_settlement = snapshot.recent_settlement
        if recent_settlement is None:
            return cls.build_hub_embed(
                snapshot=snapshot,
                selected_mapping_id=selected_mapping_id,
                action_note=action_note,
            )
        embed = discord.Embed(
            title=f"{snapshot.overview.character_name}｜突破试炼结算",
            description="仅操作者可见",
            color=discord.Color.dark_purple(),
        )
        embed.add_field(
            name="结算概览",
            value=cls._build_settlement_overview_block(recent_settlement=recent_settlement),
            inline=False,
        )
        embed.add_field(
            name="资格与前置检查",
            value=cls._build_settlement_requirement_block(snapshot=snapshot, recent_settlement=recent_settlement),
            inline=False,
        )
        embed.add_field(
            name="奖励与资源变化",
            value=cls._build_settlement_reward_block(recent_settlement=recent_settlement),
            inline=False,
        )
        embed.add_field(
            name="当前状态变化",
            value=cls._build_settlement_status_block(snapshot=snapshot, recent_settlement=recent_settlement),
            inline=False,
        )
        embed.add_field(
            name="关键战报摘要",
            value=cls._build_battle_report_block(recent_settlement=recent_settlement),
            inline=False,
        )
        if action_note is not None and action_note.lines:
            embed.add_field(name=action_note.title, value="\n".join(action_note.lines), inline=False)
        footer_text = f"最近结算时间：{_format_datetime(recent_settlement.occurred_at)}｜{_build_trial_footer(snapshot=snapshot, selected_mapping_id=selected_mapping_id)}"
        embed.set_footer(text=footer_text)
        return embed

    @classmethod
    def _build_status_block(cls, *, snapshot: BreakthroughPanelSnapshot) -> str:
        hub = snapshot.hub
        lines = [
            f"境界：{snapshot.overview.realm_name}·{snapshot.overview.stage_name}",
            f"生命：{cls._format_ratio(hub.current_hp_ratio)}",
            f"灵力：{cls._format_ratio(hub.current_mp_ratio)}",
            f"突破资格：{'已获得' if hub.qualification_obtained else '未获得'}",
        ]
        current_trial = hub.current_trial
        if current_trial is None:
            lines.append("当前关卡：无可推进的新关卡")
        else:
            lines.extend(
                (
                    f"当前关卡：{current_trial.trial_name}",
                    f"关卡状态：{'可挑战' if current_trial.can_challenge else '暂不可挑战'}",
                )
            )
        lines.append(f"已通关关卡数：{len(hub.cleared_mapping_ids)}")
        return "\n".join(lines)

    @classmethod
    def _build_precheck_block(cls, *, snapshot: BreakthroughPanelSnapshot) -> str:
        precheck = snapshot.precheck
        lines = [
            f"目标境界：{precheck.target_realm_name or '当前已到开放上限'}",
            f"当前判定：{'已满足全部前置' if precheck.passed else '仍有未满足前置'}",
            (
                "修为："
                f"{precheck.current_cultivation_value}/"
                f"{precheck.required_cultivation_value if precheck.required_cultivation_value is not None else '-'}"
            ),
            (
                "感悟："
                f"{precheck.current_comprehension_value}/"
                f"{precheck.required_comprehension_value if precheck.required_comprehension_value is not None else '-'}"
            ),
            f"资格状态：{'已具备' if precheck.qualification_obtained else '尚未具备'}",
        ]
        gap_lines = cls._build_gap_lines(snapshot=snapshot)
        if gap_lines:
            lines.append("缺口：" + "；".join(gap_lines))
        return "\n".join(lines)

    @classmethod
    def _build_selected_trial_block(cls, *, snapshot: BreakthroughPanelSnapshot, selected_mapping_id: str | None) -> str:
        trial_snapshot = _find_trial_snapshot(snapshot=snapshot, mapping_id=selected_mapping_id)
        if trial_snapshot is None:
            return "当前没有可挑战或可复读的突破试炼。"
        group_name = _resolve_group_name(snapshot=snapshot, group_id=trial_snapshot.group_id)
        lines = [
            f"名称：{trial_snapshot.trial_name}",
            f"分组：{group_name}",
            f"境界：{snapshot.overview.realm_name} → {snapshot.precheck.target_realm_name or trial_snapshot.to_realm_id}",
            f"试炼景象：{trial_snapshot.environment_rule}",
            f"重复奖励方向：{_REWARD_DIRECTION_NAME_BY_VALUE.get(trial_snapshot.repeat_reward_direction, trial_snapshot.repeat_reward_direction)}",
            f"首通资格：{'是' if trial_snapshot.first_clear_grants_qualification else '否'}",
            f"当前可挑战：{'是' if trial_snapshot.can_challenge else '否'}",
            f"历史状态：{_build_trial_history_status(trial_snapshot=trial_snapshot)}",
            f"尝试次数：{trial_snapshot.attempt_count}｜通关次数：{trial_snapshot.cleared_count}",
        ]
        if trial_snapshot.last_cleared_at is not None:
            lines.append(f"最近通关：{trial_snapshot.last_cleared_at}")
        return "\n".join(lines)

    @classmethod
    def _build_group_overview_block(cls, *, snapshot: BreakthroughPanelSnapshot) -> str:
        lines: list[str] = []
        for group in snapshot.hub.groups:
            challengeable_count = sum(1 for trial in group.trials if trial.can_challenge)
            cleared_count = sum(1 for trial in group.trials if trial.is_cleared)
            current_trial = next((trial for trial in group.trials if trial.is_current_trial), None)
            current_trial_name = current_trial.trial_name if current_trial is not None else "无"
            lines.append(
                (
                    f"{group.group_name}：可挑战 {challengeable_count}｜已通关 {cleared_count}/{len(group.trials)}\n"
                    f"主题：{group.theme_summary}\n"
                    f"奖励方向：{group.reward_focus_summary}\n"
                    f"当前关卡：{current_trial_name}"
                )
            )
        if not lines:
            return "当前没有可展示的突破分组。"
        return "\n\n".join(lines)

    @classmethod
    def _build_recent_settlement_summary(cls, *, snapshot: BreakthroughPanelSnapshot) -> str:
        recent_settlement = snapshot.recent_settlement
        if recent_settlement is None:
            return "暂无最近一次突破试炼结算。"
        settlement = recent_settlement.settlement
        lines = [
            f"关卡：{recent_settlement.trial_name}",
            f"结果：{'胜利' if settlement.victory else '失败'}｜{_SETTLEMENT_NAME_BY_VALUE.get(settlement.settlement_type, settlement.settlement_type)}",
            f"资格变化：{'本次获得突破资格' if settlement.qualification_granted else '本次未获得新资格'}",
            f"资源摘要：{cls._build_compact_reward_summary(settlement=settlement)}",
            f"结算时间：{_format_datetime(recent_settlement.occurred_at)}",
        ]
        return "\n".join(lines)

    @classmethod
    def _build_settlement_overview_block(cls, *, recent_settlement: BreakthroughRecentSettlementSnapshot) -> str:
        settlement = recent_settlement.settlement
        lines = [
            f"关卡：{recent_settlement.trial_name}",
            f"分组：{recent_settlement.group_name}",
            f"结果：{'胜利' if settlement.victory else '失败'}",
            f"结算类型：{_SETTLEMENT_NAME_BY_VALUE.get(settlement.settlement_type, settlement.settlement_type)}",
            f"突破资格变化：{'已获得' if settlement.qualification_granted else '无新增'}",
        ]
        return "\n".join(lines)

    @classmethod
    def _build_settlement_requirement_block(
        cls,
        *,
        snapshot: BreakthroughPanelSnapshot,
        recent_settlement: BreakthroughRecentSettlementSnapshot,
    ) -> str:
        precheck = snapshot.precheck
        settlement = recent_settlement.settlement
        lines = [
            f"当前资格：{'已具备' if snapshot.hub.qualification_obtained else '尚未具备'}",
            f"本次资格变化：{'获得突破资格' if settlement.qualification_granted else '无新增资格'}",
            f"前置判定：{'已满足全部前置' if precheck.passed else '仍有缺口'}",
        ]
        gap_lines = cls._build_gap_lines(snapshot=snapshot)
        if gap_lines:
            lines.append("剩余缺口：" + "；".join(gap_lines))
        else:
            lines.append("剩余缺口：无")
        return "\n".join(lines)

    @classmethod
    def _build_settlement_reward_block(cls, *, recent_settlement: BreakthroughRecentSettlementSnapshot) -> str:
        settlement = recent_settlement.settlement
        lines: list[str] = []
        reward_lines = cls._build_reward_package_lines(settlement=settlement)
        if reward_lines:
            lines.append("奖励包：")
            lines.extend(f"- {line}" for line in reward_lines)
        else:
            lines.append("奖励包：无")
        currency_lines = cls._format_currency_changes(settlement.currency_changes)
        lines.append("货币变化：" + ("｜".join(currency_lines) if currency_lines else "无"))
        item_lines = cls._format_item_changes(settlement.item_changes)
        lines.append("物品变化：" + ("｜".join(item_lines) if item_lines else "无"))
        soft_limit_lines = cls._build_soft_limit_lines(settlement=settlement)
        if soft_limit_lines:
            lines.append("软限制：")
            lines.extend(f"- {line}" for line in soft_limit_lines)
        return "\n".join(lines)

    @classmethod
    def _build_settlement_status_block(
        cls,
        *,
        snapshot: BreakthroughPanelSnapshot,
        recent_settlement: BreakthroughRecentSettlementSnapshot,
    ) -> str:
        settlement = recent_settlement.settlement
        latest_trial = _find_trial_snapshot(snapshot=snapshot, mapping_id=recent_settlement.mapping_id)
        lines = [
            f"生命：{cls._format_ratio(snapshot.hub.current_hp_ratio)}",
            f"灵力：{cls._format_ratio(snapshot.hub.current_mp_ratio)}",
            f"进度状态：{_PROGRESS_STATUS_NAME_BY_VALUE.get(settlement.progress_status, settlement.progress_status or '无记录')}",
            f"累计尝试：{settlement.attempt_count}｜累计通关：{settlement.cleared_count}",
            f"当前资格持有：{'是' if snapshot.hub.qualification_obtained else '否'}",
        ]
        if latest_trial is not None:
            lines.append(f"当前关卡可挑战：{'是' if latest_trial.can_challenge else '否'}")
            lines.append(
                "当前重复奖励方向："
                f"{_REWARD_DIRECTION_NAME_BY_VALUE.get(latest_trial.repeat_reward_direction, latest_trial.repeat_reward_direction)}"
            )
        return "\n".join(lines)

    @classmethod
    def _build_battle_report_block(cls, *, recent_settlement: BreakthroughRecentSettlementSnapshot) -> str:
        battle_report_digest = recent_settlement.battle_report_digest
        if battle_report_digest is None:
            return "本次结算没有可展示的持久化战报摘要。"
        return "\n".join(
            (
                f"聚焦角色：{battle_report_digest.focus_unit_name}",
                f"战斗结果：{battle_report_digest.result}｜回合数：{battle_report_digest.completed_rounds}",
                (
                    "终局血蓝："
                    f"生命 {cls._format_ratio(battle_report_digest.final_hp_ratio)}｜"
                    f"灵力 {cls._format_ratio(battle_report_digest.final_mp_ratio)}"
                ),
                (
                    "输出承伤："
                    f"造成 {battle_report_digest.ally_damage_dealt}｜"
                    f"承受 {battle_report_digest.ally_damage_taken}｜"
                    f"治疗 {battle_report_digest.ally_healing_done}"
                ),
                (
                    "关键触发："
                    f"命中 {battle_report_digest.successful_hits}｜"
                    f"暴击 {battle_report_digest.critical_hits}｜"
                    f"被控跳过 {battle_report_digest.control_skips}"
                ),
            )
        )

    @classmethod
    def _build_gap_lines(cls, *, snapshot: BreakthroughPanelSnapshot) -> list[str]:
        gap_lines: list[str] = []
        for gap in snapshot.precheck.gaps:
            if gap.gap_type == "open_limit":
                gap_lines.append("当前已到开放上限")
            elif gap.gap_type == "cultivation_insufficient":
                gap_lines.append(f"修为还差 {gap.missing_value}")
            elif gap.gap_type == "comprehension_insufficient":
                gap_lines.append(f"感悟还差 {gap.missing_value}")
            elif gap.gap_type == "qualification_missing":
                gap_lines.append("缺少突破资格")
            elif gap.gap_type == "material_insufficient":
                item_name = _RESOURCE_NAME_BY_ID.get(gap.item_id or "", gap.item_id or "材料")
                gap_lines.append(f"{item_name} 还差 {gap.missing_value}")
        return gap_lines

    @classmethod
    def _build_reward_package_lines(cls, *, settlement) -> list[str]:
        reward_payload = settlement.reward_payload
        items = reward_payload.get("items")
        if not isinstance(items, Sequence) or isinstance(items, (str, bytes, bytearray)):
            return []
        lines: list[str] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            reward_kind = str(item.get("reward_kind") or "")
            if reward_kind == "qualification":
                lines.append("突破资格 ×1")
                continue
            resource_id = str(item.get("resource_id") or "")
            quantity = _read_int(item.get("quantity"))
            lines.append(f"{_RESOURCE_NAME_BY_ID.get(resource_id, resource_id or '未知资源')} ×{quantity}")
        return lines

    @classmethod
    def _build_soft_limit_lines(cls, *, settlement) -> list[str]:
        snapshot = settlement.soft_limit_snapshot
        if not isinstance(snapshot, dict):
            return []
        reward_direction = str(snapshot.get("reward_direction") or "")
        return [
            f"方向：{_REWARD_DIRECTION_NAME_BY_VALUE.get(reward_direction, reward_direction or '无')}",
            f"周期：{snapshot.get('cycle_type') or '-'}｜锚点：{snapshot.get('cycle_anchor') or '-'}",
            (
                "次数："
                f"{_read_int(snapshot.get('consumed_count_before'))} → {_read_int(snapshot.get('consumed_count_after'))}"
                f" / 高收益上限 {_read_int(snapshot.get('high_yield_limit'))}"
            ),
            f"倍率：{snapshot.get('applied_ratio') or '-'}｜进入衰减：{'是' if bool(snapshot.get('entered_reduced_yield')) else '否'}",
        ]

    @classmethod
    def _build_compact_reward_summary(cls, *, settlement) -> str:
        parts: list[str] = []
        if settlement.qualification_granted:
            parts.append("突破资格")
        parts.extend(cls._format_currency_changes(settlement.currency_changes))
        parts.extend(cls._format_item_changes(settlement.item_changes))
        if not parts:
            return "无"
        return "｜".join(parts)

    @staticmethod
    def _format_currency_changes(currency_changes: dict[str, int]) -> list[str]:
        lines: list[str] = []
        for resource_id, quantity in currency_changes.items():
            if quantity <= 0:
                continue
            lines.append(f"{_RESOURCE_NAME_BY_ID.get(resource_id, resource_id)} +{quantity}")
        return lines

    @staticmethod
    def _format_item_changes(item_changes: Sequence[dict[str, object]]) -> list[str]:
        lines: list[str] = []
        for item in item_changes:
            item_id = str(item.get("item_id") or "")
            quantity = _read_int(item.get("quantity"))
            if quantity <= 0:
                continue
            lines.append(f"{_RESOURCE_NAME_BY_ID.get(item_id, item_id or '未知物品')} +{quantity}")
        return lines

    @staticmethod
    def _format_ratio(value: Any) -> str:
        try:
            return f"{float(value) * 100:.1f}%"
        except (TypeError, ValueError):
            return "0.0%"


class BreakthroughPublicSettlementPresenter:
    """负责生成公开频道中的突破秘境高光播报。"""

    @classmethod
    def build_embed(cls, *, snapshot: BreakthroughPanelSnapshot) -> discord.Embed | None:
        recent_settlement = snapshot.recent_settlement
        if recent_settlement is None:
            return None
        highlight_lines = cls._collect_highlight_lines(snapshot=snapshot, recent_settlement=recent_settlement)
        if not highlight_lines:
            return None
        embed = discord.Embed(
            title=f"{snapshot.overview.character_name}｜突破秘境高光播报",
            description="公开频道播报",
            color=discord.Color.orange(),
        )
        embed.add_field(name="高光结果", value="\n".join(highlight_lines), inline=False)
        embed.add_field(
            name="本次结算摘要",
            value=cls._build_public_result_block(snapshot=snapshot, recent_settlement=recent_settlement),
            inline=False,
        )
        reward_lines = cls._build_public_reward_lines(recent_settlement=recent_settlement)
        if reward_lines:
            embed.add_field(name="公开奖励摘要", value="\n".join(reward_lines), inline=False)
        return embed

    @classmethod
    def _collect_highlight_lines(
        cls,
        *,
        snapshot: BreakthroughPanelSnapshot,
        recent_settlement: BreakthroughRecentSettlementSnapshot,
    ) -> tuple[str, ...]:
        settlement = recent_settlement.settlement
        lines: list[str] = []
        if settlement.qualification_granted:
            target_realm_name = snapshot.precheck.target_realm_name or recent_settlement.trial_name
            lines.append(f"首次通过 {recent_settlement.trial_name}，获得突破 {target_realm_name} 的资格")
        public_reward_lines = cls._build_public_reward_lines(recent_settlement=recent_settlement)
        if public_reward_lines and not settlement.qualification_granted:
            lines.append("本次获得可公开展示的高价值奖励")
        return tuple(lines)

    @staticmethod
    def _build_public_result_block(
        *,
        snapshot: BreakthroughPanelSnapshot,
        recent_settlement: BreakthroughRecentSettlementSnapshot,
    ) -> str:
        settlement = recent_settlement.settlement
        lines = [
            f"关卡：{recent_settlement.trial_name}",
            f"结果：{'胜利' if settlement.victory else '失败'}｜{_SETTLEMENT_NAME_BY_VALUE.get(settlement.settlement_type, settlement.settlement_type)}",
            f"当前境界：{snapshot.overview.realm_name}·{snapshot.overview.stage_name}",
        ]
        if settlement.qualification_granted:
            lines.append("资格状态：已获得下一境界突破资格")
        return "\n".join(lines)

    @staticmethod
    def _build_public_reward_lines(*, recent_settlement: BreakthroughRecentSettlementSnapshot) -> list[str]:
        settlement = recent_settlement.settlement
        lines: list[str] = []
        if settlement.qualification_granted:
            lines.append("突破资格：已达成")
        spirit_stone_delta = _read_int(settlement.currency_changes.get("spirit_stone"))
        if spirit_stone_delta >= _PUBLIC_SPIRIT_STONE_THRESHOLD:
            lines.append(f"灵石 +{spirit_stone_delta}")
        for item in settlement.item_changes:
            item_id = str(item.get("item_id") or "")
            quantity = _read_int(item.get("quantity"))
            if item_id in _PUBLIC_HIGH_VALUE_ITEM_IDS and quantity > 0:
                lines.append(f"{_RESOURCE_NAME_BY_ID.get(item_id, item_id)} +{quantity}")
        return lines


class BreakthroughTrialSelect(discord.ui.Select):
    """突破试炼选择器。"""

    def __init__(self, *, trial_options: Sequence, selected_mapping_id: str | None) -> None:
        options = []
        for trial in trial_options:
            state_parts = []
            if trial.is_current_trial:
                state_parts.append("当前关卡")
            if trial.is_cleared:
                state_parts.append("已通关")
            if trial.can_challenge:
                state_parts.append("可挑战")
            description = "｜".join(state_parts) or "可查看详情"
            options.append(
                discord.SelectOption(
                    label=trial.trial_name[:100],
                    value=trial.mapping_id,
                    description=description[:100],
                    default=trial.mapping_id == selected_mapping_id,
                )
            )
        super().__init__(
            placeholder="选择突破试炼",
            min_values=1,
            max_values=1,
            options=options,
            row=2,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, BreakthroughPanelView):
            await interaction.response.defer()
            return
        view.selected_mapping_id = self.values[0]
        view.sync_component_state()
        await interaction.response.edit_message(embed=view.build_embed(), view=view)


class BreakthroughPanelView(discord.ui.View):
    """突破秘境私有面板视图。"""

    def __init__(
        self,
        *,
        controller: BreakthroughPanelController,
        owner_user_id: int,
        character_id: int,
        snapshot: BreakthroughPanelSnapshot,
        selected_mapping_id: str | None,
        display_mode: BreakthroughDisplayMode,
        timeout: float = _PANEL_TIMEOUT_SECONDS,
    ) -> None:
        super().__init__(timeout=timeout)
        self._controller = controller
        self.owner_user_id = owner_user_id
        self.character_id = character_id
        self.snapshot = snapshot
        self.selected_mapping_id = selected_mapping_id
        self.display_mode = display_mode
        trial_options = _build_selectable_trials(snapshot=snapshot)
        if trial_options:
            self.add_item(BreakthroughTrialSelect(trial_options=trial_options, selected_mapping_id=selected_mapping_id))
        self.sync_component_state()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_user_id:
            return True
        await self._controller.responder.send_private_error(interaction, message="该私有面板仅允许发起者操作。")
        return False

    def build_embed(self) -> discord.Embed:
        if self.display_mode is BreakthroughDisplayMode.SETTLEMENT and self.snapshot.recent_settlement is not None:
            return BreakthroughPanelPresenter.build_settlement_embed(
                snapshot=self.snapshot,
                selected_mapping_id=self.selected_mapping_id,
            )
        return BreakthroughPanelPresenter.build_hub_embed(
            snapshot=self.snapshot,
            selected_mapping_id=self.selected_mapping_id,
        )

    def sync_component_state(self) -> None:
        selected_trial = _find_trial_snapshot(snapshot=self.snapshot, mapping_id=self.selected_mapping_id)
        self.start_trial.disabled = selected_trial is None or not selected_trial.can_challenge
        self.execute_breakthrough.disabled = not self.snapshot.precheck.passed
        self.view_recent_settlement.disabled = (
            self.snapshot.recent_settlement is None or self.display_mode is BreakthroughDisplayMode.SETTLEMENT
        )
        self.return_to_hub.disabled = self.display_mode is BreakthroughDisplayMode.HUB

    @discord.ui.button(label="开始突破试炼", style=discord.ButtonStyle.success, row=0)
    async def start_trial(  # type: ignore[override]
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        await self._controller.start_trial(
            interaction,
            character_id=self.character_id,
            owner_user_id=self.owner_user_id,
            selected_mapping_id=self.selected_mapping_id,
        )

    @discord.ui.button(label="执行突破", style=discord.ButtonStyle.primary, row=0)
    async def execute_breakthrough(  # type: ignore[override]
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        await self._controller.execute_breakthrough(
            interaction,
            character_id=self.character_id,
            owner_user_id=self.owner_user_id,
        )

    @discord.ui.button(label="刷新状态", style=discord.ButtonStyle.secondary, row=1)
    async def refresh_panel(  # type: ignore[override]
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        await self._controller.refresh_panel(
            interaction,
            character_id=self.character_id,
            owner_user_id=self.owner_user_id,
            selected_mapping_id=self.selected_mapping_id,
            display_mode=self.display_mode,
        )

    @discord.ui.button(label="查看资格前置", style=discord.ButtonStyle.secondary, row=1)
    async def show_precheck(  # type: ignore[override]
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        await self._controller.show_precheck(
            interaction,
            character_id=self.character_id,
            owner_user_id=self.owner_user_id,
            selected_mapping_id=self.selected_mapping_id,
        )

    @discord.ui.button(label="查看最近结算", style=discord.ButtonStyle.secondary, row=1)
    async def view_recent_settlement(  # type: ignore[override]
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        await self._controller.show_recent_settlement(
            interaction,
            character_id=self.character_id,
            owner_user_id=self.owner_user_id,
            selected_mapping_id=self.selected_mapping_id,
        )

    @discord.ui.button(label="返回入口", style=discord.ButtonStyle.secondary, row=1)
    async def return_to_hub(  # type: ignore[override]
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        await self._controller.show_hub(
            interaction,
            character_id=self.character_id,
            owner_user_id=self.owner_user_id,
            selected_mapping_id=self.selected_mapping_id,
        )


class BreakthroughPanelController:
    """组织突破秘境私有面板交互。"""

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
        """按 Discord 用户标识打开突破秘境面板。"""
        try:
            character_id = self._load_character_id_by_discord_user_id(discord_user_id=str(interaction.user.id))
            snapshot = self._load_panel_snapshot(character_id=character_id)
        except (
            CharacterPanelQueryServiceError,
            CharacterProgressionServiceError,
            BreakthroughPanelServiceError,
            BreakthroughTrialServiceError,
        ) as exc:
            await self.responder.send_private_error(interaction, message=str(exc))
            return
        await self._send_panel(
            interaction,
            snapshot=snapshot,
            owner_user_id=interaction.user.id,
            selected_mapping_id=None,
            display_mode=BreakthroughDisplayMode.HUB,
        )

    async def open_panel(self, interaction: discord.Interaction, *, character_id: int) -> None:
        """按角色标识打开突破秘境面板。"""
        try:
            snapshot = self._load_panel_snapshot(character_id=character_id)
        except (
            CharacterProgressionServiceError, BreakthroughPanelServiceError, BreakthroughTrialServiceError
        ) as exc:
            await self.responder.send_private_error(interaction, message=str(exc))
            return
        await self._send_panel(
            interaction,
            snapshot=snapshot,
            owner_user_id=interaction.user.id,
            selected_mapping_id=None,
            display_mode=BreakthroughDisplayMode.HUB,
        )

    async def refresh_panel(
        self,
        interaction: discord.Interaction,
        *,
        character_id: int,
        owner_user_id: int,
        selected_mapping_id: str | None,
        display_mode: BreakthroughDisplayMode,
    ) -> None:
        """刷新突破秘境面板。"""
        try:
            snapshot = self._load_panel_snapshot(character_id=character_id)
        except (
            CharacterProgressionServiceError, BreakthroughPanelServiceError, BreakthroughTrialServiceError
        ) as exc:
            await self.responder.send_private_error(interaction, message=str(exc))
            return
        await self._edit_panel(
            interaction,
            snapshot=snapshot,
            owner_user_id=owner_user_id,
            selected_mapping_id=selected_mapping_id,
            display_mode=display_mode,
        )

    async def show_precheck(
        self,
        interaction: discord.Interaction,
        *,
        character_id: int,
        owner_user_id: int,
        selected_mapping_id: str | None,
    ) -> None:
        """重新展示突破资格与前置条件。"""
        try:
            snapshot = self._load_panel_snapshot(character_id=character_id)
        except (
            CharacterProgressionServiceError, BreakthroughPanelServiceError, BreakthroughTrialServiceError
        ) as exc:
            await self.responder.send_private_error(interaction, message=str(exc))
            return
        action_note = BreakthroughActionNote(
            title="突破资格与前置",
            lines=_build_precheck_note(snapshot=snapshot),
        )
        await self._edit_panel(
            interaction,
            snapshot=snapshot,
            owner_user_id=owner_user_id,
            selected_mapping_id=selected_mapping_id,
            display_mode=BreakthroughDisplayMode.HUB,
            action_note=action_note,
        )

    async def start_trial(
        self,
        interaction: discord.Interaction,
        *,
        character_id: int,
        owner_user_id: int,
        selected_mapping_id: str | None,
    ) -> None:
        """执行一次突破试炼，并按条件公开高光播报。"""
        try:
            result, snapshot = self._challenge_trial(
                character_id=character_id,
                mapping_id=selected_mapping_id,
            )
        except (
            CharacterProgressionServiceError, BreakthroughPanelServiceError, BreakthroughTrialServiceError
        ) as exc:
            await self.responder.send_private_error(interaction, message=str(exc))
            return
        action_note = BreakthroughActionNote(
            title="本次试炼结果",
            lines=self._build_challenge_lines(result=result),
        )
        await self._edit_panel(
            interaction,
            snapshot=snapshot,
            owner_user_id=owner_user_id,
            selected_mapping_id=result.mapping_id,
            display_mode=BreakthroughDisplayMode.SETTLEMENT,
            action_note=action_note,
        )
        await self._send_public_highlight_if_needed(interaction, snapshot=snapshot)

    async def execute_breakthrough(
        self,
        interaction: discord.Interaction,
        *,
        character_id: int,
        owner_user_id: int,
    ) -> None:
        """执行正式突破，并刷新突破入口面板。"""
        try:
            result, snapshot = self._execute_breakthrough(character_id=character_id)
        except (
            CharacterProgressionServiceError, BreakthroughPanelServiceError, BreakthroughTrialServiceError
        ) as exc:
            await self.responder.send_private_error(interaction, message=str(exc))
            return
        action_note = BreakthroughActionNote(
            title="突破完成",
            lines=self._build_execution_lines(result=result),
        )
        await self._edit_panel(
            interaction,
            snapshot=snapshot,
            owner_user_id=owner_user_id,
            selected_mapping_id=None,
            display_mode=BreakthroughDisplayMode.HUB,
            action_note=action_note,
        )

    async def show_recent_settlement(
        self,
        interaction: discord.Interaction,
        *,
        character_id: int,
        owner_user_id: int,
        selected_mapping_id: str | None,
    ) -> None:
        """切换到最近一次突破结算详情视图。"""
        try:
            snapshot = self._load_panel_snapshot(character_id=character_id)
        except (
            CharacterProgressionServiceError, BreakthroughPanelServiceError, BreakthroughTrialServiceError
        ) as exc:
            await self.responder.send_private_error(interaction, message=str(exc))
            return
        if snapshot.recent_settlement is None:
            await self.responder.send_private_error(interaction, message="当前没有可复读的突破结算结果。")
            return
        await self._edit_panel(
            interaction,
            snapshot=snapshot,
            owner_user_id=owner_user_id,
            selected_mapping_id=selected_mapping_id or snapshot.recent_settlement.mapping_id,
            display_mode=BreakthroughDisplayMode.SETTLEMENT,
        )

    async def show_hub(
        self,
        interaction: discord.Interaction,
        *,
        character_id: int,
        owner_user_id: int,
        selected_mapping_id: str | None,
    ) -> None:
        """切换回突破秘境入口视图。"""
        try:
            snapshot = self._load_panel_snapshot(character_id=character_id)
        except (
            CharacterProgressionServiceError, BreakthroughPanelServiceError, BreakthroughTrialServiceError
        ) as exc:
            await self.responder.send_private_error(interaction, message=str(exc))
            return
        await self._edit_panel(
            interaction,
            snapshot=snapshot,
            owner_user_id=owner_user_id,
            selected_mapping_id=selected_mapping_id,
            display_mode=BreakthroughDisplayMode.HUB,
        )

    def _load_character_id_by_discord_user_id(self, *, discord_user_id: str) -> int:
        with session_scope(self._session_factory) as session:
            services: BreakthroughPanelServiceBundle = self._service_bundle_factory(session)
            overview = services.character_panel_query_service.get_overview_by_discord_user_id(
                discord_user_id=discord_user_id,
            )
            return overview.character_id

    def _load_panel_snapshot(self, *, character_id: int) -> BreakthroughPanelSnapshot:
        with session_scope(self._session_factory) as session:
            services: BreakthroughPanelServiceBundle = self._service_bundle_factory(session)
            return services.breakthrough_panel_service.get_panel_snapshot(character_id=character_id)

    def _challenge_trial(
        self,
        *,
        character_id: int,
        mapping_id: str | None,
    ) -> tuple[BreakthroughTrialChallengeResult, BreakthroughPanelSnapshot]:
        with session_scope(self._session_factory) as session:
            services: BreakthroughPanelServiceBundle = self._service_bundle_factory(session)
            result = services.breakthrough_trial_service.challenge_trial(
                character_id=character_id,
                mapping_id=mapping_id,
            )
            snapshot = services.breakthrough_panel_service.get_panel_snapshot(character_id=character_id)
            return result, snapshot

    def _execute_breakthrough(
        self,
        *,
        character_id: int,
    ) -> tuple[BreakthroughExecutionResult, BreakthroughPanelSnapshot]:
        with session_scope(self._session_factory) as session:
            services: BreakthroughPanelServiceBundle = self._service_bundle_factory(session)
            result = services.character_progression_service.execute_breakthrough(character_id=character_id)
            snapshot = services.breakthrough_panel_service.get_panel_snapshot(character_id=character_id)
            return result, snapshot

    async def _send_panel(
        self,
        interaction: discord.Interaction,
        *,
        snapshot: BreakthroughPanelSnapshot,
        owner_user_id: int,
        selected_mapping_id: str | None,
        display_mode: BreakthroughDisplayMode,
        action_note: BreakthroughActionNote | None = None,
    ) -> None:
        payload = self._build_payload(
            snapshot=snapshot,
            owner_user_id=owner_user_id,
            selected_mapping_id=selected_mapping_id,
            display_mode=display_mode,
            action_note=action_note,
        )
        await self.responder.send_message(
            interaction,
            payload=payload,
            visibility=PanelVisibility.PRIVATE,
        )

    async def _edit_panel(
        self,
        interaction: discord.Interaction,
        *,
        snapshot: BreakthroughPanelSnapshot,
        owner_user_id: int,
        selected_mapping_id: str | None,
        display_mode: BreakthroughDisplayMode,
        action_note: BreakthroughActionNote | None = None,
    ) -> None:
        payload = self._build_payload(
            snapshot=snapshot,
            owner_user_id=owner_user_id,
            selected_mapping_id=selected_mapping_id,
            display_mode=display_mode,
            action_note=action_note,
        )
        await self.responder.edit_message(interaction, payload=payload)

    def _build_payload(
        self,
        *,
        snapshot: BreakthroughPanelSnapshot,
        owner_user_id: int,
        selected_mapping_id: str | None,
        display_mode: BreakthroughDisplayMode,
        action_note: BreakthroughActionNote | None,
    ) -> PanelMessagePayload:
        normalized_mapping_id = self._resolve_selected_mapping_id(
            snapshot=snapshot,
            selected_mapping_id=selected_mapping_id,
        )
        normalized_display_mode = self._resolve_display_mode(snapshot=snapshot, display_mode=display_mode)
        view = BreakthroughPanelView(
            controller=self,
            owner_user_id=owner_user_id,
            character_id=snapshot.overview.character_id,
            snapshot=snapshot,
            selected_mapping_id=normalized_mapping_id,
            display_mode=normalized_display_mode,
            timeout=self._panel_timeout,
        )
        if normalized_display_mode is BreakthroughDisplayMode.SETTLEMENT:
            embed = BreakthroughPanelPresenter.build_settlement_embed(
                snapshot=snapshot,
                selected_mapping_id=normalized_mapping_id,
                action_note=action_note,
            )
        else:
            embed = BreakthroughPanelPresenter.build_hub_embed(
                snapshot=snapshot,
                selected_mapping_id=normalized_mapping_id,
                action_note=action_note,
            )
        return PanelMessagePayload(embed=embed, view=view)

    @staticmethod
    def _resolve_display_mode(
        *,
        snapshot: BreakthroughPanelSnapshot,
        display_mode: BreakthroughDisplayMode,
    ) -> BreakthroughDisplayMode:
        if display_mode is BreakthroughDisplayMode.SETTLEMENT and snapshot.recent_settlement is None:
            return BreakthroughDisplayMode.HUB
        return display_mode

    @staticmethod
    def _resolve_selected_mapping_id(
        *,
        snapshot: BreakthroughPanelSnapshot,
        selected_mapping_id: str | None,
    ) -> str | None:
        selectable_trials = _build_selectable_trials(snapshot=snapshot)
        selectable_mapping_ids = {trial.mapping_id for trial in selectable_trials}
        if selected_mapping_id in selectable_mapping_ids:
            return selected_mapping_id
        if snapshot.hub.current_trial is not None:
            return snapshot.hub.current_trial.mapping_id
        if snapshot.recent_settlement is not None and snapshot.recent_settlement.mapping_id in selectable_mapping_ids:
            return snapshot.recent_settlement.mapping_id
        if selectable_trials:
            return selectable_trials[0].mapping_id
        return None

    async def _send_public_highlight_if_needed(
        self,
        interaction: discord.Interaction,
        *,
        snapshot: BreakthroughPanelSnapshot,
    ) -> None:
        embed = BreakthroughPublicSettlementPresenter.build_embed(snapshot=snapshot)
        if embed is None or interaction.channel is None:
            return
        try:
            await interaction.channel.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException):
            return

    @staticmethod
    def _build_challenge_lines(*, result: BreakthroughTrialChallengeResult) -> tuple[str, ...]:
        settlement = result.settlement
        lines = [
            f"关卡：{result.trial_name}",
            f"结果：{'胜利' if settlement.victory else '失败'}｜{_SETTLEMENT_NAME_BY_VALUE.get(settlement.settlement_type, settlement.settlement_type)}",
            f"资格变化：{'获得突破资格' if settlement.qualification_granted else '无新增资格'}",
            f"当前生命：{BreakthroughPanelPresenter._format_ratio(result.current_hp_ratio)}",
            f"当前灵力：{BreakthroughPanelPresenter._format_ratio(result.current_mp_ratio)}",
        ]
        if settlement.battle_report_id is not None:
            lines.append(f"战报标识：#{settlement.battle_report_id}")
        reward_summary = BreakthroughPanelPresenter._build_compact_reward_summary(settlement=settlement)
        lines.append(f"资源摘要：{reward_summary}")
        return tuple(lines)

    @staticmethod
    def _build_execution_lines(*, result: BreakthroughExecutionResult) -> tuple[str, ...]:
        consumed_items = [
            f"{_RESOURCE_NAME_BY_ID.get(item.item_id, item.item_id)} -{item.quantity}"
            for item in result.consumed_items
            if item.quantity > 0
        ]
        lines = [
            f"境界：{result.from_realm_name} → {result.to_realm_name}",
            f"新阶段：{result.new_stage_name}",
            f"修为：{result.previous_cultivation_value} → {result.new_cultivation_value}",
            (
                "感悟："
                f"{result.previous_comprehension_value} - {result.consumed_comprehension_value} = {result.remaining_comprehension_value}"
            ),
            f"资格状态：{'已消耗' if result.qualification_consumed else '未消耗'}",
            f"材料消耗：{'｜'.join(consumed_items) if consumed_items else '无'}",
        ]
        return tuple(lines)



def _build_selectable_trials(*, snapshot: BreakthroughPanelSnapshot) -> tuple[Any, ...]:
    selectable_trials: list[Any] = []
    seen_mapping_ids: set[str] = set()
    if snapshot.hub.current_trial is not None:
        selectable_trials.append(snapshot.hub.current_trial)
        seen_mapping_ids.add(snapshot.hub.current_trial.mapping_id)
    for trial in snapshot.hub.repeatable_trials:
        if trial.mapping_id in seen_mapping_ids:
            continue
        selectable_trials.append(trial)
        seen_mapping_ids.add(trial.mapping_id)
    return tuple(selectable_trials)



def _find_trial_snapshot(*, snapshot: BreakthroughPanelSnapshot, mapping_id: str | None):
    if mapping_id is None:
        return None
    for group in snapshot.hub.groups:
        for trial in group.trials:
            if trial.mapping_id == mapping_id:
                return trial
    return None



def _resolve_group_name(*, snapshot: BreakthroughPanelSnapshot, group_id: str) -> str:
    for group in snapshot.hub.groups:
        if group.group_id == group_id:
            return group.group_name
    return group_id



def _build_precheck_note(*, snapshot: BreakthroughPanelSnapshot) -> tuple[str, ...]:
    precheck = snapshot.precheck
    lines = [
        f"目标境界：{precheck.target_realm_name or '当前已到开放上限'}",
        f"前置判定：{'已满足全部前置' if precheck.passed else '仍有缺口'}",
        f"突破资格：{'已具备' if precheck.qualification_obtained else '尚未具备'}",
    ]
    gap_lines = BreakthroughPanelPresenter._build_gap_lines(snapshot=snapshot)
    if gap_lines:
        lines.append("缺口：" + "；".join(gap_lines))
    return tuple(lines)



def _format_datetime(value) -> str:
    if value is None:
        return "-"
    return f"{discord.utils.format_dt(value, style='f')}｜{discord.utils.format_dt(value, style='R')}"



def _build_trial_history_status(*, trial_snapshot) -> str:
    if trial_snapshot.is_cleared:
        return "已通关"
    if trial_snapshot.attempt_count > 0:
        return "未通关"
    return "无记录"



def _build_trial_footer(*, snapshot: BreakthroughPanelSnapshot, selected_mapping_id: str | None) -> str:
    trial_snapshot = _find_trial_snapshot(snapshot=snapshot, mapping_id=selected_mapping_id)
    if trial_snapshot is None:
        return "当前未选择试炼"
    return f"当前试炼：{trial_snapshot.trial_name}"



def _read_int(value: Any, *, default: int = 0) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    return default


__all__ = [
    "BreakthroughDisplayMode",
    "BreakthroughPanelController",
    "BreakthroughPanelPresenter",
    "BreakthroughPanelView",
    "BreakthroughPublicSettlementPresenter",
]
