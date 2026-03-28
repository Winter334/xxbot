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
    EndlessFloorAdvanceResult,
    EndlessRunSettlementResult,
    EndlessRunStatusSnapshot,
)
from application.equipment.panel_query_service import _ENDLESS_REWARD_NAME_BY_KEY
from application.dungeon.endless_panel_service import (
    EndlessBattleReportDigest,
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
_HIGH_VALUE_EQUIPMENT_SCORE_THRESHOLD = 80
_HIGH_VALUE_DAO_PATTERN_SCORE_THRESHOLD = 16
_NODE_TYPE_NAME_BY_VALUE = {
    "normal": "普通层",
    "elite": "精英层",
    "anchor_boss": "锚点首领",
}
_STATUS_NAME_BY_VALUE = {
    None: "未运行",
    _STATUS_RUNNING: "运行中",
    _STATUS_PENDING_DEFEAT_SETTLEMENT: "待战败结算",
}
_SETTLEMENT_NAME_BY_VALUE = {
    _SETTLEMENT_RETREAT: "主动撤离",
    _SETTLEMENT_DEFEAT: "战败结算",
}
_STABLE_REWARD_ORDER = ("cultivation", "insight", "refining_essence")
_PENDING_REWARD_ORDER = ("equipment_score", "artifact_score", "dao_pattern_score")
_STABLE_REWARD_NAME_BY_KEY = {
    "cultivation": "修为",
    "insight": "感悟",
    "refining_essence": "炼华精粹",
}
_PENDING_REWARD_NAME_BY_KEY = dict(_ENDLESS_REWARD_NAME_BY_KEY)
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
        embed = discord.Embed(
            title=f"{snapshot.overview.character_name}｜无涯渊境入口",
            description="仅操作者可见",
            color=discord.Color.dark_magenta(),
        )
        embed.add_field(name="当前渊境状态", value=cls._build_status_block(snapshot=snapshot), inline=False)
        embed.add_field(
            name="可挑战信息",
            value=cls._build_challenge_block(snapshot=snapshot, selected_start_floor=selected_start_floor),
            inline=False,
        )
        embed.add_field(name="本轮渊行", value=cls._build_reward_ledger_block(snapshot=snapshot), inline=False)
        embed.add_field(name="最近结算摘要", value=cls._build_recent_settlement_summary(snapshot=snapshot), inline=False)
        if action_note is not None and action_note.lines:
            embed.add_field(name=action_note.title, value="\n".join(action_note.lines), inline=False)
        embed.set_footer(text=f"当前无涯渊境入口层位：第 {selected_start_floor} 层")
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
            name="资源变化",
            value=cls._build_settlement_resource_block(settlement=settlement),
            inline=False,
        )
        embed.add_field(
            name="阶段推进",
            value=cls._build_settlement_progress_block(snapshot=snapshot, recent_settlement=recent_settlement),
            inline=False,
        )
        embed.add_field(
            name="主要掉落",
            value=cls._build_private_drop_block(settlement=settlement),
            inline=False,
        )
        embed.add_field(
            name="关键战报摘要",
            value=cls._build_battle_report_block(recent_settlement=recent_settlement),
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
        lines = [
            f"状态：{_STATUS_NAME_BY_VALUE.get(run_status.status, run_status.status or '未运行')}",
            f"生命：{projection.current_hp}/{projection.max_hp}｜{hp_ratio}",
            f"灵力：{projection.current_resource}/{projection.max_resource}｜{mp_ratio}",
            f"已解锁最高锚点：第 {run_status.anchor_status.highest_unlocked_anchor_floor} 层",
        ]
        if not run_status.has_active_run:
            lines.append("当前没有进行中的无涯渊境探索。")
            return "\n".join(lines)
        current_region = run_status.current_region
        if current_region is not None:
            lines.extend(
                (
                    f"当前区域：{current_region.region_name}（{current_region.start_floor}-{current_region.end_floor} 层）",
                    f"区域主题：{current_region.theme_summary}",
                )
            )
        lines.extend(
            (
                f"当前层数：第 {run_status.current_floor} 层",
                f"运行内最高：第 {run_status.highest_floor_reached} 层",
                f"当前节点：{_NODE_TYPE_NAME_BY_VALUE.get(_safe_enum_value(run_status.current_node_type), '-')}",
                f"起始层：第 {run_status.selected_start_floor} 层",
            )
        )
        if run_status.started_at is not None:
            lines.append(f"开始时间：{_format_datetime(run_status.started_at)}")
        if run_status.status == _STATUS_PENDING_DEFEAT_SETTLEMENT:
            lines.append("本次推进已失败，需要先执行战败结算。")
        return "\n".join(lines)

    @classmethod
    def _build_challenge_block(cls, *, snapshot: EndlessPanelSnapshot, selected_start_floor: int) -> str:
        run_status = snapshot.run_status
        anchor_status = run_status.anchor_status
        available_start_floors = "、".join(f"第 {floor} 层" for floor in anchor_status.available_start_floors)
        lines = [
            f"可选起点：{available_start_floors}",
            f"当前锚点：{cls._format_floor(anchor_status.current_anchor_floor)}",
            f"下一锚点：{cls._format_floor(anchor_status.next_anchor_floor)}",
        ]
        if run_status.has_active_run:
            lines.extend(
                (
                    f"当前渊行起点：第 {run_status.selected_start_floor} 层",
                    "运行中不可更改起始层。",
                )
            )
            return "\n".join(lines)
        entry_floor = 1 if selected_start_floor <= 1 else selected_start_floor + 1
        lines.extend(
            (
                f"当前选择起始层：第 {selected_start_floor} 层",
                f"本轮进入层数：第 {entry_floor} 层",
                "选择起始层后，可直接开始运行。",
            )
        )
        return "\n".join(lines)

    @classmethod
    def _build_reward_ledger_block(cls, *, snapshot: EndlessPanelSnapshot) -> str:
        reward_ledger = snapshot.run_status.reward_ledger
        if reward_ledger is None:
            return "当前没有进行中的收益账本。"
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
            "未稳收益："
            + cls._format_reward_mapping_by_keys(
                reward_mapping={
                    "equipment_score": reward_ledger.pending_equipment_score,
                    "artifact_score": reward_ledger.pending_artifact_score,
                    "dao_pattern_score": reward_ledger.pending_dao_pattern_score,
                },
                key_order=_PENDING_REWARD_ORDER,
                name_mapping=_PENDING_REWARD_NAME_BY_KEY,
            ),
            f"已推进层数：{reward_ledger.advanced_floor_count}",
        ]
        latest_node_result = reward_ledger.latest_node_result
        if latest_node_result is not None:
            lines.append(
                "最近节点："
                + cls._format_latest_node_result(latest_node_result=latest_node_result)
            )
        if reward_ledger.latest_anchor_unlock is not None and bool(reward_ledger.latest_anchor_unlock.get("unlocked")):
            anchor_floor = reward_ledger.latest_anchor_unlock.get("anchor_floor")
            lines.append(f"锚点推进：已解锁第 {anchor_floor} 层起点")
        if reward_ledger.drop_display:
            latest_drop = reward_ledger.drop_display[-1]
            lines.append(
                "掉落预览："
                + cls._format_drop_preview(latest_drop=latest_drop)
            )
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
            "未稳收益："
            + cls._format_reward_mapping_by_keys(
                reward_mapping=settlement.pending_rewards.settled,
                key_order=_PENDING_REWARD_ORDER,
                name_mapping=_PENDING_REWARD_NAME_BY_KEY,
            ),
            f"结算时间：{_format_datetime(settlement.settled_at)}",
        ]
        return "\n".join(lines)

    @classmethod
    def _build_settlement_overview_block(cls, *, recent_settlement: EndlessRecentSettlementSnapshot) -> str:
        settlement = recent_settlement.settlement_result
        lines = [
            f"结算类型：{_SETTLEMENT_NAME_BY_VALUE.get(settlement.settlement_type, settlement.settlement_type)}",
            f"终止层数：第 {settlement.terminated_floor} 层",
            f"区域：{settlement.current_region.region_name}（{settlement.current_region.start_floor}-{settlement.current_region.end_floor} 层）",
            f"起始层：{cls._format_floor(recent_settlement.selected_start_floor)}",
            f"本轮推进：{recent_settlement.advanced_floor_count} 层",
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
            "未稳收益",
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
            "- 入账："
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
        latest_anchor_unlock = recent_settlement.latest_anchor_unlock
        if latest_anchor_unlock is not None and bool(latest_anchor_unlock.get("unlocked")):
            lines.append(f"锚点解锁：第 {latest_anchor_unlock.get('anchor_floor')} 层")
        else:
            lines.append(
                f"当前可选最高锚点：第 {snapshot.run_status.anchor_status.highest_unlocked_anchor_floor} 层"
            )
        latest_node_result = recent_settlement.latest_node_result
        if latest_node_result is not None:
            lines.append(
                "终局节点："
                + cls._format_latest_node_result(latest_node_result=latest_node_result)
            )
        return "\n".join(lines)

    @classmethod
    def _build_private_drop_block(cls, *, settlement: EndlessRunSettlementResult) -> str:
        lines = cls._extract_final_drop_lines(settlement=settlement, public_mode=False)
        if not lines:
            return "本次没有可展示的主要掉落。"
        return "\n".join(lines)

    @classmethod
    def _build_battle_report_block(cls, *, recent_settlement: EndlessRecentSettlementSnapshot) -> str:
        latest_node_result = recent_settlement.latest_node_result
        battle_report_digest = recent_settlement.battle_report_digest
        if battle_report_digest is None and latest_node_result is None:
            return "本次结算没有关联的持久化战报摘要。"
        lines: list[str] = []
        if latest_node_result is not None:
            lines.append(
                "节点结果："
                + cls._format_latest_node_result(latest_node_result=latest_node_result)
            )
        if battle_report_digest is not None:
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
                        f"被控跳过 {battle_report_digest.control_skips}"
                    ),
                )
            )
        return "\n".join(lines)

    @classmethod
    def _format_drop_preview(cls, *, latest_drop: Mapping[str, Any]) -> str:
        reward_mapping = {
            "equipment_score": _read_int(latest_drop.get("equipment_score")),
            "artifact_score": _read_int(latest_drop.get("artifact_score")),
            "dao_pattern_score": _read_int(latest_drop.get("dao_pattern_score")),
        }
        prefix = f"第 {_read_int(latest_drop.get('floor'))} 层"
        return (
            f"{prefix}｜"
            + cls._format_reward_mapping_by_keys(
                reward_mapping=reward_mapping,
                key_order=_PENDING_REWARD_ORDER,
                name_mapping=_PENDING_REWARD_NAME_BY_KEY,
            )
        )

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
                highlight_mapping = cls._extract_high_value_pending_reward_mapping(settled_mapping=settled_mapping)
                if public_mode:
                    if highlight_mapping:
                        lines.append(
                            "高价值掉落："
                            + cls._format_reward_mapping_by_keys(
                                reward_mapping=highlight_mapping,
                                key_order=_PENDING_REWARD_ORDER,
                                name_mapping=_PENDING_REWARD_NAME_BY_KEY,
                            )
                        )
                    continue
                formatted = cls._format_reward_mapping_by_keys(
                    reward_mapping=settled_mapping,
                    key_order=_PENDING_REWARD_ORDER,
                    name_mapping=_PENDING_REWARD_NAME_BY_KEY,
                )
                lines.append(f"未稳掉落：{formatted}")
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
        source_score = max(0, _read_int(entry.get("source_score")))
        if public_mode:
            if is_artifact:
                if source_score <= 0:
                    return None
            elif source_score < _HIGH_VALUE_EQUIPMENT_SCORE_THRESHOLD:
                return None
            quality_name = str(entry.get("quality_name") or "").strip()
            if not quality_name:
                return ("法宝实例：" if is_artifact else "装备实例：") + display_name
            return ("法宝实例：" if is_artifact else "装备实例：") + f"{display_name}｜{quality_name}"
        parts = [display_name]
        quality_name = str(entry.get("quality_name") or "").strip()
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
        equipment_score = _read_int(settled_mapping.get("equipment_score"))
        artifact_score = _read_int(settled_mapping.get("artifact_score"))
        dao_pattern_score = _read_int(settled_mapping.get("dao_pattern_score"))
        highlight_mapping: dict[str, int] = {}
        if equipment_score >= _HIGH_VALUE_EQUIPMENT_SCORE_THRESHOLD:
            highlight_mapping["equipment_score"] = equipment_score
        if artifact_score > 0:
            highlight_mapping["artifact_score"] = artifact_score
        if dao_pattern_score >= _HIGH_VALUE_DAO_PATTERN_SCORE_THRESHOLD:
            highlight_mapping["dao_pattern_score"] = dao_pattern_score
        return highlight_mapping

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
        latest_anchor_unlock = recent_settlement.latest_anchor_unlock
        if latest_anchor_unlock is not None and bool(latest_anchor_unlock.get("unlocked")):
            lines.append(f"首次解锁第 {latest_anchor_unlock.get('anchor_floor')} 层锚点")
        high_value_drop_lines = EndlessPanelPresenter._extract_final_drop_lines(
            settlement=settlement,
            public_mode=True,
        )
        if high_value_drop_lines:
            lines.append("出现高价值掉落")
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
            lines.append(f"本轮推进：{recent_settlement.advanced_floor_count} 层")
        return "\n".join(lines)

    @staticmethod
    def _build_public_resource_block(*, settlement: EndlessRunSettlementResult) -> str:
        stable_summary = EndlessPanelPresenter._format_reward_mapping_by_keys(
            reward_mapping=settlement.stable_rewards.settled,
            key_order=_STABLE_REWARD_ORDER,
            name_mapping=_STABLE_REWARD_NAME_BY_KEY,
        )
        pending_mapping = EndlessPanelPresenter._extract_high_value_pending_reward_mapping(
            settled_mapping=settlement.pending_rewards.settled,
        )
        if not pending_mapping:
            return f"稳定入账：{stable_summary}"
        pending_summary = EndlessPanelPresenter._format_reward_mapping_by_keys(
            reward_mapping=pending_mapping,
            key_order=_PENDING_REWARD_ORDER,
            name_mapping=_PENDING_REWARD_NAME_BY_KEY,
        )
        return f"稳定入账：{stable_summary}\n高价值保留：{pending_summary}"


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
        self.start_run.disabled = run_status.has_active_run
        self.advance_next_floor.disabled = run_status.status != _STATUS_RUNNING
        self.settle_retreat.disabled = run_status.status != _STATUS_RUNNING
        self.settle_defeat.disabled = run_status.status != _STATUS_PENDING_DEFEAT_SETTLEMENT
        self.view_recent_settlement.disabled = (
            self.snapshot.recent_settlement is None or self.display_mode is EndlessDisplayMode.SETTLEMENT
        )
        self.return_to_hub.disabled = self.display_mode is EndlessDisplayMode.HUB
        for item in self.children:
            if isinstance(item, EndlessStartFloorSelect):
                item.disabled = run_status.has_active_run

    @discord.ui.button(label="开始运行", style=discord.ButtonStyle.success, row=0)
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

    @discord.ui.button(label="推进一层", style=discord.ButtonStyle.primary, row=0)
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

    @discord.ui.button(label="主动撤离", style=discord.ButtonStyle.danger, row=0)
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
        current_region = run_status.current_region
        action_lines = [
            f"起始层：第 {selected_start_floor} 层",
            f"进入层数：第 {run_status.current_floor} 层",
        ]
        if current_region is not None:
            action_lines.append(f"当前区域：{current_region.region_name}")
        action_lines.append(
            f"下一锚点：{EndlessPanelPresenter._format_floor(run_status.anchor_status.next_anchor_floor)}"
        )
        action_note = EndlessActionNote(title="运行已开始", lines=tuple(action_lines))
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
        """推进当前无尽运行到下一层或待结算态。"""
        del display_mode
        try:
            result, snapshot = self._advance_next_floor(character_id=character_id)
        except (EndlessPanelQueryServiceError, EndlessDungeonServiceError) as exc:
            await self.responder.send_private_error(interaction, message=str(exc))
            return
        action_note = EndlessActionNote(
            title="本层推进结果",
            lines=self._build_advance_lines(result=result),
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

    def _advance_next_floor(self, *, character_id: int) -> tuple[EndlessFloorAdvanceResult, EndlessPanelSnapshot]:
        with session_scope(self._session_factory) as session:
            services: EndlessPanelServiceBundle = self._service_bundle_factory(session)
            result = services.endless_dungeon_service.advance_next_floor(character_id=character_id)
            snapshot = services.endless_panel_query_service.get_panel_snapshot(character_id=character_id)
            return result, snapshot

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
    def _build_advance_lines(*, result: EndlessFloorAdvanceResult) -> tuple[str, ...]:
        lines = [
            f"本层：第 {result.cleared_floor} 层",
            f"结果：{result.battle_outcome}",
        ]
        if result.next_floor is not None:
            lines.append(f"已推进至：第 {result.next_floor} 层")
        else:
            lines.append("当前进入待战败结算态。")
        if result.battle_report_id is not None:
            lines.append(f"战报标识：#{result.battle_report_id}")
        if bool(result.anchor_unlock_result.get("unlocked")):
            lines.append(f"锚点解锁：第 {result.anchor_unlock_result.get('anchor_floor')} 层")
        latest_node_result = result.latest_node_result
        if latest_node_result:
            lines.append(
                "终局状态："
                f"生命 {EndlessPanelPresenter._format_ratio_text(latest_node_result.get('current_hp_ratio'))}｜"
                f"灵力 {EndlessPanelPresenter._format_ratio_text(latest_node_result.get('current_mp_ratio'))}"
            )
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
            "待确认保留："
            + EndlessPanelPresenter._format_reward_mapping_by_keys(
                reward_mapping=settlement.pending_rewards.settled,
                key_order=_PENDING_REWARD_ORDER,
                name_mapping=_PENDING_REWARD_NAME_BY_KEY,
            ),
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
