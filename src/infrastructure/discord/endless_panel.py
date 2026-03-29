"""Discord 无尽副本私有入口与结算面板。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Protocol

import discord
from sqlalchemy.orm import Session, sessionmaker

from application.character.panel_query_service import CharacterPanelQueryService, CharacterPanelQueryServiceError
from application.dungeon import (
    EndlessDungeonService,
    EndlessDungeonServiceError,
    EndlessRunSettlementResult,
    EndlessRunStatusSnapshot,
)
from application.dungeon.endless_panel_service import (
    EndlessAdvancePresentation,
    EndlessBattleReportDigest,
    EndlessFloorPanelSnapshot,
    EndlessPanelQueryService,
    EndlessPanelQueryServiceError,
    EndlessPanelSnapshot,
    EndlessRecentSettlementSnapshot,
)
from infrastructure.db.session import session_scope
from infrastructure.discord.character_panel import (
    DiscordInteractionVisibilityResponder,
    PanelMessagePayload,
    PanelVisibility,
)

_PANEL_TIMEOUT_SECONDS = 20 * 60
_STATUS_RUNNING = "running"
_STATUS_PENDING_DEFEAT_SETTLEMENT = "pending_defeat_settlement"
_SETTLEMENT_RETREAT = "retreat"
_SETTLEMENT_DEFEAT = "defeat"
_PUBLIC_HIGHLIGHT_EQUIPMENT_QUALITY_IDS = frozenset({"epic", "legendary"})
_NODE_TYPE_NAME_BY_VALUE = {
    "normal": "常规层",
    "elite": "精英层",
    "anchor_boss": "首领层",
}
_STATUS_NAME_BY_VALUE = {
    None: "未运行",
    _STATUS_RUNNING: "运行中",
    _STATUS_PENDING_DEFEAT_SETTLEMENT: "待战败结算",
}
_SETTLEMENT_NAME_BY_VALUE = {
    _SETTLEMENT_RETREAT: "结算撤离",
    _SETTLEMENT_DEFEAT: "战败结算",
}
_STABLE_REWARD_ORDER = ("cultivation", "insight", "refining_essence")
_PENDING_REWARD_ORDER = ("drop_progress",)
_STABLE_REWARD_NAME_BY_KEY = {
    "cultivation": "修为",
    "insight": "感悟",
    "refining_essence": "炼华精粹",
}
_PENDING_REWARD_NAME_BY_KEY = {"drop_progress": "统一掉落进度"}
_ENDLESS_EQUIPMENT_ENTRY_TYPE = "equipment_drop"
_ENDLESS_ARTIFACT_ENTRY_TYPE = "artifact_drop"
_ENDLESS_SKILL_ENTRY_TYPE = "skill_drop"
_AUXILIARY_SLOT_NAME_BY_ID = {
    "guard": "护体",
    "movement": "身法",
    "spirit": "神识",
}


class EndlessDisplayMode(StrEnum):
    """无尽副本私有面板展示模式。"""

    HUB = "hub"
    SETTLEMENT = "settlement"


class EndlessPanelServiceBundle(Protocol):
    """无尽副本面板所需的最小服务集合。"""

    character_panel_query_service: CharacterPanelQueryService
    endless_dungeon_service: EndlessDungeonService
    endless_panel_query_service: EndlessPanelQueryService


@dataclass(frozen=True, slots=True)
class EndlessActionNote:
    """无尽副本面板动作反馈。"""

    title: str
    lines: tuple[str, ...]


class EndlessPanelPresenter:
    """负责把无尽副本聚合快照投影为 Discord Embed。"""

    @classmethod
    def build_hub_embed(
        cls,
        *,
        snapshot: EndlessPanelSnapshot,
        selected_start_floor: int,
        action_note: EndlessActionNote | None = None,
    ) -> discord.Embed:
        presentation = snapshot.run_presentation
        embed = discord.Embed(
            title=f"{snapshot.overview.character_name}｜无涯渊境",
            description="仅操作者可见",
            color=discord.Color.dark_magenta(),
        )
        embed.add_field(name="当前渊行状态", value=cls._build_status_block(snapshot=snapshot), inline=False)
        embed.add_field(
            name="下一战敌阵" if snapshot.run_status.has_active_run else "挑战准备",
            value=cls._build_challenge_block(snapshot=snapshot, selected_start_floor=selected_start_floor),
            inline=False,
        )
        embed.add_field(name="本轮战况", value=cls._build_reward_ledger_block(snapshot=snapshot), inline=False)
        embed.add_field(name="最近战斗记录", value=cls._build_recent_floor_history_block(snapshot=snapshot), inline=False)
        embed.add_field(name="最近结算摘要", value=cls._build_recent_settlement_summary(snapshot=snapshot), inline=False)
        if action_note is not None and action_note.lines:
            embed.add_field(name=action_note.title, value="\n".join(action_note.lines), inline=False)
        footer_text = f"当前选择起始层：第 {selected_start_floor} 层"
        if presentation.decision_floor is not None:
            footer_text += f"｜已停在第 {presentation.decision_floor} 层决策点"
        elif presentation.next_floor is not None and snapshot.run_status.has_active_run:
            footer_text += f"｜下一战：第 {presentation.next_floor} 层"
        embed.set_footer(text=footer_text)
        return embed

    @classmethod
    def build_settlement_embed(
        cls,
        *,
        snapshot: EndlessPanelSnapshot,
        selected_start_floor: int,
        action_note: EndlessActionNote | None = None,
    ) -> discord.Embed:
        recent_settlement = snapshot.recent_settlement
        if recent_settlement is None:
            return cls.build_hub_embed(
                snapshot=snapshot,
                selected_start_floor=selected_start_floor,
                action_note=action_note,
            )
        settlement = recent_settlement.settlement_result
        embed = discord.Embed(
            title=f"{snapshot.overview.character_name}｜无涯渊境结算",
            description="仅操作者可见",
            color=discord.Color.purple(),
        )
        embed.add_field(
            name="结算概览",
            value=cls._build_settlement_overview_block(recent_settlement=recent_settlement),
            inline=False,
        )
        embed.add_field(
            name="资源与掉落",
            value=cls._build_settlement_resource_block(settlement=settlement),
            inline=False,
        )
        embed.add_field(
            name="终局战况",
            value=cls._build_settlement_progress_block(snapshot=snapshot, recent_settlement=recent_settlement),
            inline=False,
        )
        embed.add_field(
            name="关键战斗过程",
            value=cls._build_battle_report_block(recent_settlement=recent_settlement),
            inline=False,
        )
        embed.add_field(
            name="主要掉落",
            value=cls._build_private_drop_block(settlement=settlement),
            inline=False,
        )
        if action_note is not None and action_note.lines:
            embed.add_field(name=action_note.title, value="\n".join(action_note.lines), inline=False)
        embed.set_footer(
            text=(
                f"最近结算时间：{_format_datetime(settlement.settled_at)}"
                f"｜下次起始层选择：第 {selected_start_floor} 层"
            )
        )
        return embed

    @classmethod
    def _build_status_block(cls, *, snapshot: EndlessPanelSnapshot) -> str:
        projection = snapshot.overview.battle_projection
        hp_ratio = cls._format_ratio_by_current_and_max(projection.current_hp, projection.max_hp)
        mp_ratio = cls._format_ratio_by_current_and_max(projection.current_resource, projection.max_resource)
        run_status = snapshot.run_status
        presentation = snapshot.run_presentation
        lines = [
            f"状态：{presentation.phase_label}",
            f"生命：{projection.current_hp}/{projection.max_hp}｜{hp_ratio}",
            f"灵力：{projection.current_resource}/{projection.max_resource}｜{mp_ratio}",
        ]
        if not run_status.has_active_run:
            lines.append("当前未在渊行中，可从已解锁起始层重新开始挑战。")
            return "\n".join(lines)
        current_region = run_status.current_region
        if current_region is not None:
            lines.extend(
                (
                    f"当前区域：{current_region.region_name}（{current_region.start_floor}-{current_region.end_floor} 层）",
                    f"区域主题：{current_region.theme_summary}",
                )
            )
        lines.append(f"本轮起始层：第 {run_status.selected_start_floor} 层")
        if presentation.phase == "pending_defeat_settlement":
            lines.append(f"本轮止步：第 {presentation.stopped_floor} 层，当前只能执行战败结算。")
        elif presentation.decision_floor is not None:
            lines.append(
                f"本轮止步：第 {presentation.decision_floor} 层决策点｜下一战为第 {presentation.next_floor} 层"
            )
        else:
            lines.append(f"当前待挑战：第 {run_status.current_floor} 层")
        lines.append(
            f"已完成战斗：{presentation.battle_count} 场｜已跨越楼层：{presentation.advanced_floor_count}"
        )
        lines.append(
            f"累计统一掉落进度：{presentation.pending_drop_progress}｜可结算掉落 {presentation.claimable_drop_count} 次"
        )
        if run_status.started_at is not None:
            lines.append(f"开始时间：{_format_datetime(run_status.started_at)}")
        return "\n".join(lines)

    @classmethod
    def _build_challenge_block(cls, *, snapshot: EndlessPanelSnapshot, selected_start_floor: int) -> str:
        run_status = snapshot.run_status
        presentation = snapshot.run_presentation
        available_start_floors = "、".join(f"第 {floor} 层" for floor in run_status.anchor_status.available_start_floors)
        if not run_status.has_active_run:
            entry_floor = 1 if selected_start_floor <= 1 else selected_start_floor + 1
            return "\n".join(
                (
                    f"可选起始层：{available_start_floors}",
                    f"当前选择：第 {selected_start_floor} 层",
                    f"本次入场：第 {entry_floor} 层",
                    "点击“开始挑战”后，将从所选起始层接续无涯渊境。",
                )
            )
        preview = presentation.upcoming_floor_preview
        if preview is None:
            if presentation.phase == "pending_defeat_settlement":
                return "当前已进入战败待结算状态，需先完成本轮结算。"
            return "当前暂无可预览的下一战敌阵。"
        lines = [
            cls._format_floor_enemy_header(floor_snapshot=preview),
            cls._format_enemy_style_line(floor_snapshot=preview),
            cls._format_enemy_unit_summary(floor_snapshot=preview),
        ]
        if presentation.can_settle_retreat and presentation.decision_floor is not None:
            lines.append("当前位于决策点，可继续挑战或结算撤离。")
        else:
            lines.append("点击“继续挑战”后，将自动推进至下一处决策点或战败。")
        return "\n".join(lines)

    @classmethod
    def _build_reward_ledger_block(cls, *, snapshot: EndlessPanelSnapshot) -> str:
        reward_ledger = snapshot.run_status.reward_ledger
        presentation = snapshot.run_presentation
        if reward_ledger is None:
            return "当前没有进行中的渊行账本。"
        lines = [
            "稳定收益："
            + cls._format_reward_mapping_by_keys(
                reward_mapping={
                    "cultivation": reward_ledger.stable_cultivation,
                    "insight": reward_ledger.stable_insight,
                    "refining_essence": reward_ledger.stable_refining_essence,
                },
                key_order=_STABLE_REWARD_ORDER,
                name_mapping=_STABLE_REWARD_NAME_BY_KEY,
            ),
            f"累计统一掉落进度：{presentation.pending_drop_progress}｜可结算掉落 {presentation.claimable_drop_count} 次",
            f"已完成战斗：{presentation.battle_count} 场｜已跨越楼层：{presentation.advanced_floor_count}",
        ]
        latest_floor_result = presentation.latest_floor_result
        if latest_floor_result is not None:
            lines.append("最近结果：" + cls._format_floor_result_summary(floor_snapshot=latest_floor_result))
        return "\n".join(lines)

    @classmethod
    def _build_recent_floor_history_block(cls, *, snapshot: EndlessPanelSnapshot) -> str:
        floor_results = snapshot.run_presentation.recent_floor_results
        if not floor_results:
            return "当前暂无已完成的楼层战斗。"
        lines: list[str] = []
        for floor_snapshot in floor_results[-3:]:
            lines.append("• " + cls._format_floor_result_summary(floor_snapshot=floor_snapshot))
            process_lines = cls._format_floor_process_lines(floor_snapshot=floor_snapshot, max_round_lines=1)
            lines.extend(f"  {line}" for line in process_lines)
        return "\n".join(lines)

    @classmethod
    def _build_recent_settlement_summary(cls, *, snapshot: EndlessPanelSnapshot) -> str:
        recent_settlement = snapshot.recent_settlement
        if recent_settlement is None:
            return "暂无最近一次无尽终结结算。"
        settlement = recent_settlement.settlement_result
        lines = [
            f"结算类型：{_SETTLEMENT_NAME_BY_VALUE.get(settlement.settlement_type, settlement.settlement_type)}",
            f"终止层数：第 {settlement.terminated_floor} 层",
            "稳定入账："
            + cls._format_reward_mapping_by_keys(
                reward_mapping=settlement.stable_rewards.settled,
                key_order=_STABLE_REWARD_ORDER,
                name_mapping=_STABLE_REWARD_NAME_BY_KEY,
            ),
            f"统一掉落进度兑现：{max(0, _read_int(settlement.pending_rewards.settled.get('drop_progress')))}",
            f"结算时间：{_format_datetime(settlement.settled_at)}",
        ]
        if recent_settlement.last_floor_result is not None:
            lines.append("终局结果：" + cls._format_floor_result_summary(floor_snapshot=recent_settlement.last_floor_result))
        return "\n".join(lines)

    @classmethod
    def _build_settlement_overview_block(cls, *, recent_settlement: EndlessRecentSettlementSnapshot) -> str:
        settlement = recent_settlement.settlement_result
        record_floor_before_run = recent_settlement.record_floor_before_run
        current_record_floor = max(record_floor_before_run, settlement.terminated_floor)
        lines = [
            f"结算类型：{_SETTLEMENT_NAME_BY_VALUE.get(settlement.settlement_type, settlement.settlement_type)}",
            f"终止层数：第 {settlement.terminated_floor} 层",
            f"区域：{settlement.current_region.region_name}（{settlement.current_region.start_floor}-{settlement.current_region.end_floor} 层）",
            f"起始层：{cls._format_floor(recent_settlement.selected_start_floor)}",
            f"本轮自动推进战斗：{recent_settlement.advanced_floor_count} 场",
            f"个人纪录：第 {record_floor_before_run} 层 → 第 {current_record_floor} 层",
            f"可重复查看：{'是' if settlement.can_repeat_read else '否'}",
        ]
        return "\n".join(lines)

    @classmethod
    def _build_settlement_resource_block(cls, *, settlement: EndlessRunSettlementResult) -> str:
        lines = [
            "稳定收益",
            "- 原值："
            + cls._format_reward_mapping_by_keys(
                reward_mapping=settlement.stable_rewards.original,
                key_order=_STABLE_REWARD_ORDER,
                name_mapping=_STABLE_REWARD_NAME_BY_KEY,
            ),
            "- 扣除："
            + cls._format_reward_mapping_by_keys(
                reward_mapping=settlement.stable_rewards.deducted,
                key_order=_STABLE_REWARD_ORDER,
                name_mapping=_STABLE_REWARD_NAME_BY_KEY,
            ),
            "- 入账："
            + cls._format_reward_mapping_by_keys(
                reward_mapping=settlement.stable_rewards.settled,
                key_order=_STABLE_REWARD_ORDER,
                name_mapping=_STABLE_REWARD_NAME_BY_KEY,
            ),
            "统一掉落进度",
            "- 原值："
            + cls._format_reward_mapping_by_keys(
                reward_mapping=settlement.pending_rewards.original,
                key_order=_PENDING_REWARD_ORDER,
                name_mapping=_PENDING_REWARD_NAME_BY_KEY,
            ),
            "- 扣除："
            + cls._format_reward_mapping_by_keys(
                reward_mapping=settlement.pending_rewards.deducted,
                key_order=_PENDING_REWARD_ORDER,
                name_mapping=_PENDING_REWARD_NAME_BY_KEY,
            ),
            "- 兑现："
            + cls._format_reward_mapping_by_keys(
                reward_mapping=settlement.pending_rewards.settled,
                key_order=_PENDING_REWARD_ORDER,
                name_mapping=_PENDING_REWARD_NAME_BY_KEY,
            ),
        ]
        return "\n".join(lines)

    @classmethod
    def _build_settlement_progress_block(
        cls,
        *,
        snapshot: EndlessPanelSnapshot,
        recent_settlement: EndlessRecentSettlementSnapshot,
    ) -> str:
        del snapshot
        settlement = recent_settlement.settlement_result
        record_floor_before_run = recent_settlement.record_floor_before_run
        current_record_floor = max(record_floor_before_run, settlement.terminated_floor)
        lines = [
            f"本轮前个人纪录：第 {record_floor_before_run} 层",
            f"本轮后个人纪录：第 {current_record_floor} 层",
        ]
        if settlement.terminated_floor > record_floor_before_run:
            lines.append(f"纪录刷新：抵达第 {settlement.terminated_floor} 层")
        else:
            lines.append("纪录变化：本次未刷新个人纪录")
        last_floor_result = recent_settlement.last_floor_result
        if last_floor_result is not None:
            lines.append("终局敌阵：" + cls._format_floor_enemy_header(floor_snapshot=last_floor_result))
            lines.append("终局结果：" + cls._format_floor_result_summary(floor_snapshot=last_floor_result))
        return "\n".join(lines)

    @classmethod
    def _build_private_drop_block(cls, *, settlement: EndlessRunSettlementResult) -> str:
        lines = cls._extract_final_drop_lines(settlement=settlement, public_mode=False)
        if not lines:
            return "本次没有可展示的主要掉落。"
        return "\n".join(lines)

    @classmethod
    def _build_battle_report_block(cls, *, recent_settlement: EndlessRecentSettlementSnapshot) -> str:
        last_floor_result = recent_settlement.last_floor_result
        if last_floor_result is None:
            return "本次结算没有关联的终局战斗摘要。"
        battle_report_digest = last_floor_result.battle_report_digest
        lines = [cls._format_floor_result_summary(floor_snapshot=last_floor_result)]
        if battle_report_digest is None:
            return "\n".join(lines)
        lines.extend(
            (
                f"战报标识：#{battle_report_digest.battle_report_id}",
                f"聚焦角色：{battle_report_digest.focus_unit_name}",
                f"战斗结果：{battle_report_digest.result}｜回合数：{battle_report_digest.completed_rounds}",
                f"终局血蓝：生命 {cls._format_ratio_text(battle_report_digest.final_hp_ratio)}｜灵力 {cls._format_ratio_text(battle_report_digest.final_mp_ratio)}",
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
                    f"控场 {battle_report_digest.control_skips}｜"
                    f"击破 {battle_report_digest.unit_defeated}"
                ),
            )
        )
        if battle_report_digest.action_highlights:
            lines.append("关键技能：" + "、".join(battle_report_digest.action_highlights))
        lines.extend(battle_report_digest.round_highlights[:2])
        return "\n".join(lines)

    @classmethod
    def _format_floor_enemy_header(cls, *, floor_snapshot: EndlessFloorPanelSnapshot) -> str:
        return (
            f"第 {floor_snapshot.floor} 层｜{floor_snapshot.node_label}｜{floor_snapshot.region_name}｜"
            f"{floor_snapshot.race_name}·{floor_snapshot.template_name}×{floor_snapshot.enemy_count}"
        )

    @classmethod
    def _format_enemy_style_line(cls, *, floor_snapshot: EndlessFloorPanelSnapshot) -> str:
        parts = [f"成长层级：{floor_snapshot.realm_name}·{floor_snapshot.stage_name}"]
        if floor_snapshot.style_tags:
            parts.append("标签：" + " / ".join(floor_snapshot.style_tags))
        profiles = [
            item
            for item in (floor_snapshot.race_profile, floor_snapshot.template_profile)
            if item and item != "-"
        ]
        if profiles:
            parts.append("风格：" + "｜".join(profiles))
        return "｜".join(parts)

    @staticmethod
    def _format_enemy_unit_summary(*, floor_snapshot: EndlessFloorPanelSnapshot) -> str:
        if not floor_snapshot.enemy_units:
            return "属性摘要：暂缺"
        parts = [
            (
                f"{unit.unit_name} 气血 {unit.max_hp}｜攻力 {unit.attack_power}｜"
                f"护体 {unit.guard_power}｜迅捷 {unit.speed}"
            )
            for unit in floor_snapshot.enemy_units[:2]
        ]
        if len(floor_snapshot.enemy_units) > 2:
            parts.append(f"其余 {len(floor_snapshot.enemy_units) - 2} 名同系敌人")
        return "属性摘要：" + "；".join(parts)

    @classmethod
    def _format_floor_result_summary(cls, *, floor_snapshot: EndlessFloorPanelSnapshot) -> str:
        parts = [
            f"第 {floor_snapshot.floor} 层",
            floor_snapshot.node_label,
            f"{floor_snapshot.race_name}·{floor_snapshot.template_name}×{floor_snapshot.enemy_count}",
        ]
        if floor_snapshot.battle_outcome_label is not None:
            parts.append(f"结果 {floor_snapshot.battle_outcome_label}")
        if floor_snapshot.current_hp_ratio is not None or floor_snapshot.current_mp_ratio is not None:
            parts.append(
                f"血蓝 {cls._format_ratio_text(floor_snapshot.current_hp_ratio)}"
                f"/{cls._format_ratio_text(floor_snapshot.current_mp_ratio)}"
            )
        if floor_snapshot.cumulative_drop_progress is not None:
            parts.append(
                f"进度 {floor_snapshot.cumulative_drop_progress}（可结算 {max(0, floor_snapshot.claimable_drop_count or 0)} 次）"
            )
        return "｜".join(parts)

    @staticmethod
    def _format_floor_process_lines(
        *,
        floor_snapshot: EndlessFloorPanelSnapshot,
        max_round_lines: int,
    ) -> tuple[str, ...]:
        digest = floor_snapshot.battle_report_digest
        if digest is None:
            return ()
        lines: list[str] = []
        if digest.action_highlights:
            lines.append("关键技能：" + "、".join(digest.action_highlights[:3]))
        if digest.round_highlights:
            lines.extend(digest.round_highlights[:max_round_lines])
        if not lines:
            lines.append(
                f"战斗信号：命中 {digest.successful_hits}｜暴击 {digest.critical_hits}｜击破 {digest.unit_defeated}"
            )
        return tuple(lines)

    @classmethod
    def _format_latest_node_result(cls, *, latest_node_result: Mapping[str, Any]) -> str:
        floor = _read_int(latest_node_result.get("floor"))
        node_name = _NODE_TYPE_NAME_BY_VALUE.get(str(latest_node_result.get("node_type") or ""), "未知节点")
        battle_outcome = str(latest_node_result.get("battle_outcome") or "-")
        hp_ratio = cls._format_ratio_text(latest_node_result.get("current_hp_ratio"))
        mp_ratio = cls._format_ratio_text(latest_node_result.get("current_mp_ratio"))
        return (
            f"第 {floor} 层｜{node_name}｜结果 {battle_outcome}｜"
            f"生命 {hp_ratio}｜灵力 {mp_ratio}"
        )

    @classmethod
    def _extract_final_drop_lines(
        cls,
        *,
        settlement: EndlessRunSettlementResult,
        public_mode: bool,
    ) -> list[str]:
        lines: list[str] = []
        for entry in settlement.final_drop_list:
            entry_type = str(entry.get("entry_type") or "")
            settled_mapping = _normalize_int_mapping(entry.get("settled"))
            if entry_type == "stable_reward_bundle":
                if public_mode:
                    continue
                formatted = cls._format_reward_mapping_by_keys(
                    reward_mapping=settled_mapping,
                    key_order=_STABLE_REWARD_ORDER,
                    name_mapping=_STABLE_REWARD_NAME_BY_KEY,
                )
                lines.append(f"稳定资源包：{formatted}")
                continue
            if entry_type == "pending_reward_bundle":
                if public_mode:
                    continue
                formatted = cls._format_reward_mapping_by_keys(
                    reward_mapping=settled_mapping,
                    key_order=_PENDING_REWARD_ORDER,
                    name_mapping=_PENDING_REWARD_NAME_BY_KEY,
                )
                lines.append(f"统一掉落进度：{formatted}")
                continue
            if entry_type in {_ENDLESS_EQUIPMENT_ENTRY_TYPE, _ENDLESS_ARTIFACT_ENTRY_TYPE}:
                line = cls._format_instance_drop_entry(entry=entry, public_mode=public_mode)
                if line is not None:
                    lines.append(line)
                continue
            if entry_type == _ENDLESS_SKILL_ENTRY_TYPE:
                line = cls._format_skill_drop_entry(entry=entry, public_mode=public_mode)
                if line is not None:
                    lines.append(line)
        return lines

    @staticmethod
    def _format_instance_drop_entry(*, entry: Mapping[str, Any], public_mode: bool) -> str | None:
        display_name = str(entry.get("display_name") or entry.get("template_name") or "").strip()
        if not display_name:
            return None
        is_artifact = bool(entry.get("is_artifact"))
        source_progress = max(0, _read_int(entry.get("source_progress")))
        quality_id = str(entry.get("quality_id") or "").strip()
        quality_name = str(entry.get("quality_name") or "").strip()
        if public_mode:
            if source_progress <= 0:
                return None
            if (
                not is_artifact
                and quality_id not in _PUBLIC_HIGHLIGHT_EQUIPMENT_QUALITY_IDS
                and quality_name not in {"史诗", "传说"}
            ):
                return None
            if not quality_name:
                return ("法宝实例：" if is_artifact else "装备实例：") + display_name
            return ("法宝实例：" if is_artifact else "装备实例：") + f"{display_name}｜{quality_name}"
        parts = [display_name]
        rank_name = str(entry.get("rank_name") or "").strip()
        slot_name = str(entry.get("slot_name") or "").strip()
        resonance_name = str(entry.get("resonance_name") or "").strip()
        if quality_name:
            parts.append(quality_name)
        if rank_name:
            parts.append(rank_name)
        if slot_name and not is_artifact:
            parts.append(slot_name)
        if resonance_name:
            parts.append(f"共鸣 {resonance_name}")
        return ("法宝实例：" if is_artifact else "装备实例：") + "｜".join(parts)

    @staticmethod
    def _format_skill_drop_entry(*, entry: Mapping[str, Any], public_mode: bool) -> str | None:
        skill_name = str(entry.get("skill_name") or "").strip()
        if not skill_name:
            return None
        if public_mode:
            return None
        parts = [skill_name]
        rank_name = str(entry.get("rank_name") or "").strip()
        quality_name = str(entry.get("quality_name") or "").strip()
        skill_type = str(entry.get("skill_type") or "").strip()
        auxiliary_slot_id = str(entry.get("auxiliary_slot_id") or "").strip()
        if rank_name:
            parts.append(rank_name)
        if quality_name:
            parts.append(quality_name)
        if skill_type == "auxiliary" and auxiliary_slot_id:
            parts.append(f"辅位 {_AUXILIARY_SLOT_NAME_BY_ID.get(auxiliary_slot_id, '未知辅位')}")
        return "功法实例：" + "｜".join(parts)

    @staticmethod
    def _extract_high_value_pending_reward_mapping(*, settled_mapping: Mapping[str, int]) -> dict[str, int]:
        del settled_mapping
        return {}

    @staticmethod
    def _format_reward_mapping(*, reward_ledger_to_mapping: Mapping[str, int] | None) -> str:
        if reward_ledger_to_mapping is None:
            return "-"
        if not reward_ledger_to_mapping:
            return "无"
        return "｜".join(f"{key} {value}" for key, value in reward_ledger_to_mapping.items())

    @staticmethod
    def _format_reward_mapping_by_keys(
        *,
        reward_mapping: Mapping[str, int],
        key_order: tuple[str, ...],
        name_mapping: Mapping[str, str],
    ) -> str:
        parts = []
        for key in key_order:
            parts.append(f"{name_mapping.get(key, key)} {max(0, _read_int(reward_mapping.get(key)))}")
        return "｜".join(parts)

    @staticmethod
    def _format_floor(value: int | None) -> str:
        if value is None:
            return "-"
        return f"第 {value} 层"

    @staticmethod
    def _format_ratio_by_current_and_max(current: int, maximum: int) -> str:
        if maximum <= 0:
            return "0.0%"
        return f"{current / maximum * 100:.1f}%"

    @staticmethod
    def _format_ratio_text(value: Any) -> str:
        return _format_ratio_text(value)


class EndlessPublicSettlementPresenter:
    """负责生成公开频道中的无尽高光播报。"""

    @classmethod
    def build_embed(cls, *, snapshot: EndlessPanelSnapshot) -> discord.Embed | None:
        recent_settlement = snapshot.recent_settlement
        if recent_settlement is None:
            return None
        highlight_lines = cls._collect_highlight_lines(recent_settlement=recent_settlement)
        if not highlight_lines:
            return None
        settlement = recent_settlement.settlement_result
        embed = discord.Embed(
            title=f"{snapshot.overview.character_name}｜无涯渊境高光战绩",
            description="公开频道播报",
            color=discord.Color.orange(),
        )
        embed.add_field(name="高光结果", value="\n".join(highlight_lines), inline=False)
        embed.add_field(
            name="本次结算摘要",
            value=cls._build_public_result_block(recent_settlement=recent_settlement),
            inline=False,
        )
        public_drop_lines = EndlessPanelPresenter._extract_final_drop_lines(
            settlement=settlement,
            public_mode=True,
        )
        if public_drop_lines:
            embed.add_field(name="高价值掉落", value="\n".join(public_drop_lines), inline=False)
        embed.add_field(
            name="关键资源摘要",
            value=cls._build_public_resource_block(settlement=settlement),
            inline=False,
        )
        return embed

    @classmethod
    def _collect_highlight_lines(cls, *, recent_settlement: EndlessRecentSettlementSnapshot) -> tuple[str, ...]:
        settlement = recent_settlement.settlement_result
        lines: list[str] = []
        if settlement.terminated_floor > recent_settlement.record_floor_before_run:
            lines.append(
                f"个人纪录刷新：第 {settlement.terminated_floor} 层（原纪录第 {recent_settlement.record_floor_before_run} 层）"
            )
        public_drop_lines = EndlessPanelPresenter._extract_final_drop_lines(
            settlement=settlement,
            public_mode=True,
        )
        if public_drop_lines:
            lines.append("本轮出现主要掉落")
        return tuple(lines)

    @staticmethod
    def _build_public_result_block(*, recent_settlement: EndlessRecentSettlementSnapshot) -> str:
        settlement = recent_settlement.settlement_result
        lines = [
            f"结算类型：{_SETTLEMENT_NAME_BY_VALUE.get(settlement.settlement_type, settlement.settlement_type)}",
            f"终止层数：第 {settlement.terminated_floor} 层",
            f"区域：{settlement.current_region.region_name}",
        ]
        if recent_settlement.advanced_floor_count > 0:
            lines.append(f"本轮自动推进战斗：{recent_settlement.advanced_floor_count} 场")
        return "\n".join(lines)

    @staticmethod
    def _build_public_resource_block(*, settlement: EndlessRunSettlementResult) -> str:
        stable_summary = EndlessPanelPresenter._format_reward_mapping_by_keys(
            reward_mapping=settlement.stable_rewards.settled,
            key_order=_STABLE_REWARD_ORDER,
            name_mapping=_STABLE_REWARD_NAME_BY_KEY,
        )
        drop_progress = max(0, _read_int(settlement.pending_rewards.settled.get("drop_progress")))
        if drop_progress <= 0:
            return f"稳定入账：{stable_summary}"
        return f"稳定入账：{stable_summary}\n统一掉落进度兑现：{drop_progress}"


class EndlessStartFloorSelect(discord.ui.Select):
    """无尽副本起始层选择器。"""

    def __init__(self, *, available_start_floors: tuple[int, ...], selected_start_floor: int) -> None:
        options = [
            discord.SelectOption(
                label=f"第 {floor} 层",
                value=str(floor),
                default=floor == selected_start_floor,
            )
            for floor in available_start_floors
        ]
        super().__init__(
            placeholder="选择起始层",
            min_values=1,
            max_values=1,
            options=options,
            row=2,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, EndlessPanelView):
            await interaction.response.defer()
            return
        view.selected_start_floor = int(self.values[0])
        await interaction.response.edit_message(embed=view.build_embed(), view=view)


class EndlessPanelView(discord.ui.View):
    """无尽副本私有面板视图。"""

    def __init__(
        self,
        *,
        controller: EndlessPanelController,
        owner_user_id: int,
        character_id: int,
        snapshot: EndlessPanelSnapshot,
        selected_start_floor: int,
        display_mode: EndlessDisplayMode,
        timeout: float = _PANEL_TIMEOUT_SECONDS,
    ) -> None:
        super().__init__(timeout=timeout)
        self._controller = controller
        self.owner_user_id = owner_user_id
        self.character_id = character_id
        self.snapshot = snapshot
        self.selected_start_floor = selected_start_floor
        self.display_mode = display_mode
        self.add_item(
            EndlessStartFloorSelect(
                available_start_floors=snapshot.run_status.anchor_status.available_start_floors,
                selected_start_floor=selected_start_floor,
            )
        )
        self._sync_component_state()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_user_id:
            return True
        await self._controller.responder.send_private_error(interaction, message="该私有面板仅允许发起者操作。")
        return False

    def build_embed(self) -> discord.Embed:
        if self.display_mode is EndlessDisplayMode.SETTLEMENT and self.snapshot.recent_settlement is not None:
            return EndlessPanelPresenter.build_settlement_embed(
                snapshot=self.snapshot,
                selected_start_floor=self.selected_start_floor,
            )
        return EndlessPanelPresenter.build_hub_embed(
            snapshot=self.snapshot,
            selected_start_floor=self.selected_start_floor,
        )

    def _sync_component_state(self) -> None:
        run_status = self.snapshot.run_status
        presentation = self.snapshot.run_presentation
        self.start_run.disabled = run_status.has_active_run
        self.advance_next_floor.disabled = not presentation.can_continue or run_status.status != _STATUS_RUNNING
        self.settle_retreat.disabled = not presentation.can_settle_retreat
        self.settle_defeat.disabled = not presentation.can_settle_defeat
        self._set_item_visibility(self.settle_retreat, visible=presentation.can_settle_retreat)
        self._set_item_visibility(self.settle_defeat, visible=presentation.can_settle_defeat)
        self.view_recent_settlement.disabled = (
            self.snapshot.recent_settlement is None or self.display_mode is EndlessDisplayMode.SETTLEMENT
        )
        self.return_to_hub.disabled = self.display_mode is EndlessDisplayMode.HUB
        for item in self.children:
            if isinstance(item, EndlessStartFloorSelect):
                item.disabled = run_status.has_active_run

    def _set_item_visibility(self, item: discord.ui.Item[Any], *, visible: bool) -> None:
        if visible:
            if item not in self.children:
                self.add_item(item)
            return
        if item in self.children:
            self.remove_item(item)

    @discord.ui.button(label="开始挑战", style=discord.ButtonStyle.success, row=0)
    async def start_run(  # type: ignore[override]
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        await self._controller.start_run(
            interaction,
            character_id=self.character_id,
            owner_user_id=self.owner_user_id,
            selected_start_floor=self.selected_start_floor,
        )

    @discord.ui.button(label="继续挑战", style=discord.ButtonStyle.primary, row=0)
    async def advance_next_floor(  # type: ignore[override]
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        await self._controller.advance_next_floor(
            interaction,
            character_id=self.character_id,
            owner_user_id=self.owner_user_id,
            selected_start_floor=self.selected_start_floor,
            display_mode=self.display_mode,
        )

    @discord.ui.button(label="结算撤离", style=discord.ButtonStyle.danger, row=0)
    async def settle_retreat(  # type: ignore[override]
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        await self._controller.settle_retreat(
            interaction,
            character_id=self.character_id,
            owner_user_id=self.owner_user_id,
            selected_start_floor=self.selected_start_floor,
        )

    @discord.ui.button(label="战败结算", style=discord.ButtonStyle.danger, row=0)
    async def settle_defeat(  # type: ignore[override]
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        await self._controller.settle_defeat(
            interaction,
            character_id=self.character_id,
            owner_user_id=self.owner_user_id,
            selected_start_floor=self.selected_start_floor,
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
            selected_start_floor=self.selected_start_floor,
            display_mode=self.display_mode,
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
            selected_start_floor=self.selected_start_floor,
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
            selected_start_floor=self.selected_start_floor,
        )


class EndlessPanelController:
    """组织无尽副本私有面板交互。"""

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
        """按 Discord 用户标识打开无尽副本面板。"""
        try:
            character_id = self._load_character_id_by_discord_user_id(discord_user_id=str(interaction.user.id))
            snapshot = self._load_panel_snapshot(character_id=character_id)
        except (CharacterPanelQueryServiceError, EndlessPanelQueryServiceError, EndlessDungeonServiceError) as exc:
            await self.responder.send_private_error(interaction, message=str(exc))
            return
        await self._send_panel(
            interaction,
            snapshot=snapshot,
            owner_user_id=interaction.user.id,
            selected_start_floor=None,
            display_mode=EndlessDisplayMode.HUB,
        )

    async def open_panel(self, interaction: discord.Interaction, *, character_id: int) -> None:
        """按角色标识打开无尽副本面板。"""
        try:
            snapshot = self._load_panel_snapshot(character_id=character_id)
        except (EndlessPanelQueryServiceError, EndlessDungeonServiceError) as exc:
            await self.responder.send_private_error(interaction, message=str(exc))
            return
        await self._send_panel(
            interaction,
            snapshot=snapshot,
            owner_user_id=interaction.user.id,
            selected_start_floor=None,
            display_mode=EndlessDisplayMode.HUB,
        )

    async def refresh_panel(
        self,
        interaction: discord.Interaction,
        *,
        character_id: int,
        owner_user_id: int,
        selected_start_floor: int,
        display_mode: EndlessDisplayMode,
    ) -> None:
        """刷新无尽副本面板。"""
        try:
            snapshot = self._load_panel_snapshot(character_id=character_id)
        except (EndlessPanelQueryServiceError, EndlessDungeonServiceError) as exc:
            await self.responder.send_private_error(interaction, message=str(exc))
            return
        await self._edit_panel(
            interaction,
            snapshot=snapshot,
            owner_user_id=owner_user_id,
            selected_start_floor=selected_start_floor,
            display_mode=display_mode,
        )

    async def start_run(
        self,
        interaction: discord.Interaction,
        *,
        character_id: int,
        owner_user_id: int,
        selected_start_floor: int,
    ) -> None:
        """开始一条新的无尽副本运行。"""
        try:
            run_status, snapshot = self._start_run(
                character_id=character_id,
                selected_start_floor=selected_start_floor,
            )
        except (EndlessPanelQueryServiceError, EndlessDungeonServiceError) as exc:
            await self.responder.send_private_error(interaction, message=str(exc))
            return
        action_note = EndlessActionNote(
            title="挑战已开始",
            lines=self._build_start_lines(snapshot=snapshot, selected_start_floor=selected_start_floor),
        )
        await self._edit_panel(
            interaction,
            snapshot=snapshot,
            owner_user_id=owner_user_id,
            selected_start_floor=selected_start_floor,
            display_mode=EndlessDisplayMode.HUB,
            action_note=action_note,
        )

    async def advance_next_floor(
        self,
        interaction: discord.Interaction,
        *,
        character_id: int,
        owner_user_id: int,
        selected_start_floor: int,
        display_mode: EndlessDisplayMode,
    ) -> None:
        """推进当前无尽运行到下一处决策点或待结算态。"""
        del display_mode
        try:
            advance_presentation, snapshot = self._advance_next_floor(character_id=character_id)
        except (EndlessPanelQueryServiceError, EndlessDungeonServiceError) as exc:
            await self.responder.send_private_error(interaction, message=str(exc))
            return
        action_note = EndlessActionNote(
            title="本次自动推进",
            lines=self._build_advance_lines(advance_presentation=advance_presentation),
        )
        await self._edit_panel(
            interaction,
            snapshot=snapshot,
            owner_user_id=owner_user_id,
            selected_start_floor=selected_start_floor,
            display_mode=EndlessDisplayMode.HUB,
            action_note=action_note,
        )

    async def settle_retreat(
        self,
        interaction: discord.Interaction,
        *,
        character_id: int,
        owner_user_id: int,
        selected_start_floor: int,
    ) -> None:
        """执行主动撤离结算，并按条件公开高光播报。"""
        try:
            settlement, snapshot = self._settle_retreat(character_id=character_id)
        except (EndlessPanelQueryServiceError, EndlessDungeonServiceError) as exc:
            await self.responder.send_private_error(interaction, message=str(exc))
            return
        action_note = EndlessActionNote(
            title="本次结算",
            lines=self._build_settlement_lines(settlement=settlement),
        )
        await self._edit_panel(
            interaction,
            snapshot=snapshot,
            owner_user_id=owner_user_id,
            selected_start_floor=selected_start_floor,
            display_mode=EndlessDisplayMode.SETTLEMENT,
            action_note=action_note,
        )
        await self._send_public_highlight_if_needed(interaction, snapshot=snapshot)

    async def settle_defeat(
        self,
        interaction: discord.Interaction,
        *,
        character_id: int,
        owner_user_id: int,
        selected_start_floor: int,
    ) -> None:
        """执行战败结算，并按条件公开高光播报。"""
        try:
            settlement, snapshot = self._settle_defeat(character_id=character_id)
        except (EndlessPanelQueryServiceError, EndlessDungeonServiceError) as exc:
            await self.responder.send_private_error(interaction, message=str(exc))
            return
        action_note = EndlessActionNote(
            title="本次结算",
            lines=self._build_settlement_lines(settlement=settlement),
        )
        await self._edit_panel(
            interaction,
            snapshot=snapshot,
            owner_user_id=owner_user_id,
            selected_start_floor=selected_start_floor,
            display_mode=EndlessDisplayMode.SETTLEMENT,
            action_note=action_note,
        )
        await self._send_public_highlight_if_needed(interaction, snapshot=snapshot)

    async def show_recent_settlement(
        self,
        interaction: discord.Interaction,
        *,
        character_id: int,
        owner_user_id: int,
        selected_start_floor: int,
    ) -> None:
        """切换到最近一次无尽结算详情视图。"""
        try:
            snapshot = self._load_panel_snapshot(character_id=character_id)
        except (EndlessPanelQueryServiceError, EndlessDungeonServiceError) as exc:
            await self.responder.send_private_error(interaction, message=str(exc))
            return
        if snapshot.recent_settlement is None:
            await self.responder.send_private_error(interaction, message="当前没有可复读的无尽结算结果。")
            return
        await self._edit_panel(
            interaction,
            snapshot=snapshot,
            owner_user_id=owner_user_id,
            selected_start_floor=selected_start_floor,
            display_mode=EndlessDisplayMode.SETTLEMENT,
        )

    async def show_hub(
        self,
        interaction: discord.Interaction,
        *,
        character_id: int,
        owner_user_id: int,
        selected_start_floor: int,
    ) -> None:
        """切换回无尽副本入口视图。"""
        try:
            snapshot = self._load_panel_snapshot(character_id=character_id)
        except (EndlessPanelQueryServiceError, EndlessDungeonServiceError) as exc:
            await self.responder.send_private_error(interaction, message=str(exc))
            return
        await self._edit_panel(
            interaction,
            snapshot=snapshot,
            owner_user_id=owner_user_id,
            selected_start_floor=selected_start_floor,
            display_mode=EndlessDisplayMode.HUB,
        )

    def _load_character_id_by_discord_user_id(self, *, discord_user_id: str) -> int:
        with session_scope(self._session_factory) as session:
            services: EndlessPanelServiceBundle = self._service_bundle_factory(session)
            overview = services.character_panel_query_service.get_overview_by_discord_user_id(
                discord_user_id=discord_user_id,
            )
            return overview.character_id

    def _load_panel_snapshot(self, *, character_id: int) -> EndlessPanelSnapshot:
        with session_scope(self._session_factory) as session:
            services: EndlessPanelServiceBundle = self._service_bundle_factory(session)
            return services.endless_panel_query_service.get_panel_snapshot(character_id=character_id)

    def _start_run(self, *, character_id: int, selected_start_floor: int) -> tuple[EndlessRunStatusSnapshot, EndlessPanelSnapshot]:
        with session_scope(self._session_factory) as session:
            services: EndlessPanelServiceBundle = self._service_bundle_factory(session)
            run_status = services.endless_dungeon_service.start_run(
                character_id=character_id,
                selected_start_floor=selected_start_floor,
            )
            snapshot = services.endless_panel_query_service.get_panel_snapshot(character_id=character_id)
            return run_status, snapshot

    def _advance_next_floor(self, *, character_id: int) -> tuple[EndlessAdvancePresentation, EndlessPanelSnapshot]:
        with session_scope(self._session_factory) as session:
            services: EndlessPanelServiceBundle = self._service_bundle_factory(session)
            result = services.endless_dungeon_service.advance_next_floor(character_id=character_id)
            advance_presentation = services.endless_panel_query_service.build_advance_presentation(
                character_id=character_id,
                result=result,
            )
            snapshot = services.endless_panel_query_service.get_panel_snapshot(character_id=character_id)
            return advance_presentation, snapshot

    def _settle_retreat(self, *, character_id: int) -> tuple[EndlessRunSettlementResult, EndlessPanelSnapshot]:
        with session_scope(self._session_factory) as session:
            services: EndlessPanelServiceBundle = self._service_bundle_factory(session)
            settlement = services.endless_dungeon_service.settle_retreat(character_id=character_id)
            snapshot = services.endless_panel_query_service.get_panel_snapshot(character_id=character_id)
            return settlement, snapshot

    def _settle_defeat(self, *, character_id: int) -> tuple[EndlessRunSettlementResult, EndlessPanelSnapshot]:
        with session_scope(self._session_factory) as session:
            services: EndlessPanelServiceBundle = self._service_bundle_factory(session)
            settlement = services.endless_dungeon_service.settle_defeat(character_id=character_id)
            snapshot = services.endless_panel_query_service.get_panel_snapshot(character_id=character_id)
            return settlement, snapshot

    async def _send_panel(
        self,
        interaction: discord.Interaction,
        *,
        snapshot: EndlessPanelSnapshot,
        owner_user_id: int,
        selected_start_floor: int | None,
        display_mode: EndlessDisplayMode,
        action_note: EndlessActionNote | None = None,
    ) -> None:
        payload = self._build_payload(
            snapshot=snapshot,
            owner_user_id=owner_user_id,
            selected_start_floor=selected_start_floor,
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
        snapshot: EndlessPanelSnapshot,
        owner_user_id: int,
        selected_start_floor: int | None,
        display_mode: EndlessDisplayMode,
        action_note: EndlessActionNote | None = None,
    ) -> None:
        payload = self._build_payload(
            snapshot=snapshot,
            owner_user_id=owner_user_id,
            selected_start_floor=selected_start_floor,
            display_mode=display_mode,
            action_note=action_note,
        )
        await self.responder.edit_message(interaction, payload=payload)

    def _build_payload(
        self,
        *,
        snapshot: EndlessPanelSnapshot,
        owner_user_id: int,
        selected_start_floor: int | None,
        display_mode: EndlessDisplayMode,
        action_note: EndlessActionNote | None,
    ) -> PanelMessagePayload:
        normalized_start_floor = self._resolve_selected_start_floor(
            snapshot=snapshot,
            selected_start_floor=selected_start_floor,
        )
        normalized_display_mode = self._resolve_display_mode(snapshot=snapshot, display_mode=display_mode)
        view = EndlessPanelView(
            controller=self,
            owner_user_id=owner_user_id,
            character_id=snapshot.overview.character_id,
            snapshot=snapshot,
            selected_start_floor=normalized_start_floor,
            display_mode=normalized_display_mode,
            timeout=self._panel_timeout,
        )
        if normalized_display_mode is EndlessDisplayMode.SETTLEMENT:
            embed = EndlessPanelPresenter.build_settlement_embed(
                snapshot=snapshot,
                selected_start_floor=normalized_start_floor,
                action_note=action_note,
            )
        else:
            embed = EndlessPanelPresenter.build_hub_embed(
                snapshot=snapshot,
                selected_start_floor=normalized_start_floor,
                action_note=action_note,
            )
        return PanelMessagePayload(embed=embed, view=view)

    @staticmethod
    def _resolve_display_mode(*, snapshot: EndlessPanelSnapshot, display_mode: EndlessDisplayMode) -> EndlessDisplayMode:
        if display_mode is EndlessDisplayMode.SETTLEMENT and snapshot.recent_settlement is None:
            return EndlessDisplayMode.HUB
        return display_mode

    @staticmethod
    def _resolve_selected_start_floor(
        *,
        snapshot: EndlessPanelSnapshot,
        selected_start_floor: int | None,
    ) -> int:
        available_start_floors = snapshot.run_status.anchor_status.available_start_floors
        if snapshot.run_status.has_active_run and snapshot.run_status.selected_start_floor in available_start_floors:
            return int(snapshot.run_status.selected_start_floor or available_start_floors[0])
        if selected_start_floor in available_start_floors:
            return int(selected_start_floor)
        recent_settlement = snapshot.recent_settlement
        if recent_settlement is not None and recent_settlement.selected_start_floor in available_start_floors:
            return int(recent_settlement.selected_start_floor or available_start_floors[0])
        return int(available_start_floors[-1])

    async def _send_public_highlight_if_needed(
        self,
        interaction: discord.Interaction,
        *,
        snapshot: EndlessPanelSnapshot,
    ) -> None:
        embed = EndlessPublicSettlementPresenter.build_embed(snapshot=snapshot)
        if embed is None or interaction.channel is None:
            return
        try:
            await interaction.channel.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException):
            return

    @staticmethod
    def _build_start_lines(*, snapshot: EndlessPanelSnapshot, selected_start_floor: int) -> tuple[str, ...]:
        lines = [f"起始层：第 {selected_start_floor} 层"]
        preview = snapshot.run_presentation.upcoming_floor_preview
        if preview is None:
            if snapshot.run_status.current_floor is not None:
                lines.append(f"进入层数：第 {snapshot.run_status.current_floor} 层")
            return tuple(lines)
        lines.extend(
            (
                f"进入层数：第 {preview.floor} 层",
                EndlessPanelPresenter._format_floor_enemy_header(floor_snapshot=preview),
                EndlessPanelPresenter._format_enemy_style_line(floor_snapshot=preview),
                EndlessPanelPresenter._format_enemy_unit_summary(floor_snapshot=preview),
                "点击“继续挑战”后，将自动推进至下一处决策点或战败。",
            )
        )
        return tuple(lines)

    @staticmethod
    def _build_advance_lines(*, advance_presentation: EndlessAdvancePresentation) -> tuple[str, ...]:
        floor_results = advance_presentation.floor_results
        if not floor_results:
            return ("本次自动推进未产生可展示结果。",)
        start_floor = floor_results[0].floor
        end_floor = floor_results[-1].floor
        lines = [
            f"自动推进：第 {start_floor} 层" if start_floor == end_floor else f"自动推进：第 {start_floor}-{end_floor} 层",
            f"停止原因：{advance_presentation.stopped_reason_label}",
            f"累计统一掉落进度：{advance_presentation.pending_drop_progress}｜可结算掉落 {advance_presentation.claimable_drop_count} 次",
        ]
        for floor_snapshot in floor_results:
            lines.append("• " + EndlessPanelPresenter._format_floor_result_summary(floor_snapshot=floor_snapshot))
            process_lines = EndlessPanelPresenter._format_floor_process_lines(
                floor_snapshot=floor_snapshot,
                max_round_lines=1,
            )
            lines.extend(f"  {line}" for line in process_lines)
        if advance_presentation.can_settle_retreat and advance_presentation.decision_floor is not None:
            lines.append(f"已抵达第 {advance_presentation.decision_floor} 层决策点，可继续挑战或结算撤离。")
        else:
            lines.append("本次推进已战败，需先执行战败结算。")
        return tuple(lines)

    @staticmethod
    def _build_settlement_lines(*, settlement: EndlessRunSettlementResult) -> tuple[str, ...]:
        return (
            f"结算类型：{_SETTLEMENT_NAME_BY_VALUE.get(settlement.settlement_type, settlement.settlement_type)}",
            f"终止层数：第 {settlement.terminated_floor} 层",
            "稳定入账："
            + EndlessPanelPresenter._format_reward_mapping_by_keys(
                reward_mapping=settlement.stable_rewards.settled,
                key_order=_STABLE_REWARD_ORDER,
                name_mapping=_STABLE_REWARD_NAME_BY_KEY,
            ),
            f"统一掉落进度兑现：{max(0, _read_int(settlement.pending_rewards.settled.get('drop_progress')))}",
        )


def _format_datetime(value) -> str:
    if value is None:
        return "-"
    return f"{discord.utils.format_dt(value, style='f')}｜{discord.utils.format_dt(value, style='R')}"


def _safe_enum_value(value: Any) -> str | None:
    if value is None:
        return None
    enum_value = getattr(value, "value", None)
    if isinstance(enum_value, str):
        return enum_value
    if isinstance(value, str):
        return value
    return None


def _format_ratio_text(value: Any) -> str:
    decimal_value: Decimal | None = None
    if isinstance(value, Decimal):
        decimal_value = value
    elif isinstance(value, int | float | str):
        try:
            decimal_value = Decimal(str(value))
        except (InvalidOperation, ValueError):
            decimal_value = None
    if decimal_value is None:
        return "-"
    normalized = max(Decimal("0"), min(Decimal("1"), decimal_value))
    return f"{normalized * Decimal('100'):.1f}%"


def _normalize_int_mapping(value: Any) -> dict[str, int]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): _read_int(item) for key, item in value.items()}


def _read_int(value: Any, *, default: int = 0) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    return default


__all__ = [
    "EndlessDisplayMode",
    "EndlessPanelController",
    "EndlessPanelPresenter",
    "EndlessPanelView",
    "EndlessPublicSettlementPresenter",
]
