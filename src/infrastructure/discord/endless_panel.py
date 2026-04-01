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
_PUBLIC_HIGHLIGHT_EQUIPMENT_QUALITY_IDS = frozenset({"epic", "earthly", "legendary", "immortal"})
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
_PENDING_REWARD_NAME_BY_KEY = {"drop_progress": "掉落进度"}
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
            title=cls._build_embed_title(snapshot=snapshot, selected_start_floor=selected_start_floor),
            description=cls._build_embed_description(snapshot=snapshot, selected_start_floor=selected_start_floor),
            color=cls._resolve_scene_color(snapshot=snapshot),
        )
        embed.add_field(
            name="👹 敌人",
            value=cls._build_encounter_scene_block(snapshot=snapshot, selected_start_floor=selected_start_floor),
            inline=False,
        )
        embed.add_field(name="🧍 状态", value=cls._build_status_block(snapshot=snapshot), inline=False)
        embed.add_field(name="📜 战况", value=cls._build_battle_scene_block(snapshot=snapshot), inline=False)
        if action_note is not None and action_note.lines:
            embed.add_field(name=action_note.title, value="\n".join(action_note.lines[:5]), inline=False)
        if presentation.can_settle_retreat and presentation.decision_floor is not None:
            embed.add_field(name="🧭 抉择", value=cls._build_decision_block(snapshot=snapshot), inline=False)
        else:
            embed.add_field(name="✨ 战果", value=cls._build_reward_ledger_block(snapshot=snapshot), inline=False)
        footer_parts = ["仅操作者可见"]
        if presentation.phase == "pending_defeat_settlement":
            footer_parts.append("需先战败结算")
        elif presentation.can_settle_retreat and presentation.decision_floor is not None:
            footer_parts.append("可继续深入或撤离")
        embed.set_footer(text="｜".join(footer_parts))
        return embed

    @classmethod
    def _build_status_block(cls, *, snapshot: EndlessPanelSnapshot) -> str:
        projection = snapshot.overview.battle_projection
        hp_ratio = cls._format_ratio_by_current_and_max(projection.current_hp, projection.max_hp)
        mp_ratio = cls._format_ratio_by_current_and_max(projection.current_resource, projection.max_resource)
        hp_percent = cls._ratio_percent(projection.current_hp, projection.max_hp)
        mp_percent = cls._ratio_percent(projection.current_resource, projection.max_resource)
        return "\n".join(
            (
                f"气血 {projection.current_hp}/{projection.max_hp}（{hp_ratio}）",
                f"灵力 {projection.current_resource}/{projection.max_resource}（{mp_ratio}）",
                cls._build_status_feeling(hp_percent=hp_percent, mp_percent=mp_percent, snapshot=snapshot),
            )
        )

    @classmethod
    def _build_encounter_scene_block(cls, *, snapshot: EndlessPanelSnapshot, selected_start_floor: int) -> str:
        run_status = snapshot.run_status
        presentation = snapshot.run_presentation
        available_start_floors = "、".join(f"第 {floor} 层" for floor in run_status.anchor_status.available_start_floors)
        scene_floor = presentation.current_scene_floor
        if not run_status.has_active_run:
            entry_floor = cls._resolve_entry_floor(selected_start_floor)
            return "\n".join(
                (
                    f"你还未踏入渊境，已解锁锚点：{available_start_floors}。",
                    f"若从第 {selected_start_floor} 层落脚，第一战就在第 {entry_floor} 层。",
                    "入内之后，只能一层一层往下闯。",
                )
            )
        if scene_floor is None:
            if presentation.phase == "pending_defeat_settlement":
                return "这一层妖气还没散尽，你得先把败局收束下来。"
            return "眼前暂时没有清晰敌影，但更深处的妖气还在翻涌。"
        source_lines = scene_floor.enemy_scene_lines or scene_floor.enemy_summary_lines
        lines = [line for line in source_lines if line.strip()]
        scene_note = cls._build_encounter_scene_note(snapshot=snapshot, floor_snapshot=scene_floor)
        if scene_note is not None and len(lines) < 5:
            lines.append(scene_note)
        return "\n".join(lines[:5])

    @classmethod
    def _build_reward_ledger_block(cls, *, snapshot: EndlessPanelSnapshot) -> str:
        presentation = snapshot.run_presentation
        scene_floor = presentation.current_scene_floor
        if scene_floor is not None:
            if scene_floor.reward_scene_lines:
                return "\n".join(scene_floor.reward_scene_lines[:2])
            if scene_floor.reward_granted:
                return "这一层有收获，但余波未散。"
            if presentation.current_scene_kind == "upcoming_preview":
                return "这一层还没开打，战果还悬在前头。"
            return "这一层没能再带回新的收获。"
        recent_settlement = snapshot.recent_settlement
        if recent_settlement is not None and not snapshot.run_status.has_active_run:
            settlement = recent_settlement.settlement_result
            settlement_name = _SETTLEMENT_NAME_BY_VALUE.get(settlement.settlement_type, settlement.settlement_type)
            stable_text = cls._format_reward_mapping_by_keys(
                reward_mapping=settlement.stable_rewards.settled,
                key_order=_STABLE_REWARD_ORDER,
                name_mapping=_STABLE_REWARD_NAME_BY_KEY,
            )
            drop_progress = max(0, _read_int(settlement.pending_rewards.settled.get("drop_progress")))
            return "\n".join(
                (
                    f"上次止步第 {settlement.terminated_floor} 层，已完成{settlement_name}。",
                    f"带回 {stable_text}｜掉落进度 {drop_progress}",
                )
            )
        return "眼下还没真正带回什么。"

    @classmethod
    def _build_battle_scene_block(cls, *, snapshot: EndlessPanelSnapshot) -> str:
        presentation = snapshot.run_presentation
        scene_floor = presentation.current_scene_floor
        if scene_floor is None:
            return "此刻还没有能回想的厮杀。"
        if presentation.current_scene_kind == "upcoming_preview":
            return "妖气还没压到眼前。\n你再往前一步，这里才会留下厮杀。"
        if scene_floor.battle_scene_lines:
            return "\n".join(scene_floor.battle_scene_lines[:3])
        digest = scene_floor.battle_report_digest
        if digest is not None and digest.narration_lines:
            return "\n".join(digest.narration_lines[:3])
        if presentation.current_scene_kind == "defeat":
            return "这一层的反扑来得太快，你只记得自己被逼退了下来。"
        return "方才交锋太短，还没来得及留下清晰的回响。"

    @classmethod
    def _build_decision_block(cls, *, snapshot: EndlessPanelSnapshot) -> str:
        presentation = snapshot.run_presentation
        scene_floor = presentation.current_scene_floor
        if presentation.decision_floor is None:
            return "眼下还没有走到能够驻足抉择的节点。"
        lines: list[str] = []
        if scene_floor is not None and scene_floor.reward_scene_lines:
            lines.extend(scene_floor.reward_scene_lines[:2])
        else:
            lines.append(f"第 {presentation.decision_floor} 层已破，眼前短暂安静了下来。")
        lines.append("继续点击即可逐层推进；若想收手，就在这里带着收获撤离。")
        return "\n".join(lines[:3])


    @classmethod
    def _build_embed_title(cls, *, snapshot: EndlessPanelSnapshot, selected_start_floor: int) -> str:
        floor = cls._resolve_title_floor(snapshot=snapshot, selected_start_floor=selected_start_floor)
        badge = cls._resolve_floor_badge(snapshot=snapshot)
        if floor is None:
            return f"{snapshot.overview.character_name}｜🌀无涯渊境·{badge}"
        return f"{snapshot.overview.character_name}｜🌀无涯渊境·第 {floor} 层·{badge}"

    @classmethod
    def _build_embed_description(cls, *, snapshot: EndlessPanelSnapshot, selected_start_floor: int) -> str:
        presentation = snapshot.run_presentation
        scene_floor = presentation.current_scene_floor
        if not snapshot.run_status.has_active_run:
            entry_floor = cls._resolve_entry_floor(selected_start_floor)
            return f"选定锚点后，你会直落第 {entry_floor} 层。"
        if scene_floor is None:
            return "前路暂时沉寂，但真正的去留还在你手里。"
        if presentation.current_scene_kind == "upcoming_preview":
            return f"第 {scene_floor.floor} 层妖气压境，抬脚便是开战。"
        if presentation.current_scene_kind == "decision":
            return f"第 {scene_floor.floor} 层刚破，前路与退路都在脚下。"
        if presentation.current_scene_kind == "defeat":
            return f"你被压在第 {scene_floor.floor} 层前，气息一时还没稳住。"
        return f"第 {scene_floor.floor} 层余波未散，更深处的妖气已经抬头。"

    @classmethod
    def _resolve_title_floor(cls, *, snapshot: EndlessPanelSnapshot, selected_start_floor: int) -> int | None:
        scene_floor = snapshot.run_presentation.current_scene_floor
        if scene_floor is not None:
            return scene_floor.floor
        if snapshot.run_status.current_floor is not None:
            return snapshot.run_status.current_floor
        return cls._resolve_entry_floor(selected_start_floor)

    @staticmethod
    def _resolve_entry_floor(selected_start_floor: int) -> int:
        return 1 if selected_start_floor <= 1 else selected_start_floor + 1

    @staticmethod
    def _resolve_floor_badge(*, snapshot: EndlessPanelSnapshot) -> str:
        presentation = snapshot.run_presentation
        scene_floor = presentation.current_scene_floor
        tone = {
            "normal": "普通层",
            "elite": "精英层",
            "anchor_boss": "首领层",
        }.get("" if scene_floor is None else scene_floor.node_type, "前路未明")
        if presentation.phase == "pending_defeat_settlement":
            return f"{tone}·战败"
        if presentation.decision_floor is not None:
            return f"{tone}·节点"
        if presentation.current_scene_kind == "upcoming_preview" or not snapshot.run_status.has_active_run:
            return f"{tone}·待开战"
        return f"{tone}·已破"

    @staticmethod
    def _resolve_scene_color(*, snapshot: EndlessPanelSnapshot) -> discord.Color:
        presentation = snapshot.run_presentation
        scene_floor = presentation.current_scene_floor
        if presentation.phase == "pending_defeat_settlement":
            return discord.Color.red()
        if presentation.phase == "decision":
            return discord.Color.gold()
        if scene_floor is not None and scene_floor.node_type == "anchor_boss":
            return discord.Color.dark_red()
        if scene_floor is not None and scene_floor.node_type == "elite":
            return discord.Color.orange()
        return discord.Color.dark_magenta()

    @staticmethod
    def _ratio_percent(current: int, maximum: int) -> float:
        if maximum <= 0:
            return 0.0
        return max(0.0, min(1.0, current / maximum))

    @classmethod
    def _build_status_feeling(cls, *, hp_percent: float, mp_percent: float, snapshot: EndlessPanelSnapshot) -> str:
        presentation = snapshot.run_presentation
        scene_floor = presentation.current_scene_floor
        if not snapshot.run_status.has_active_run:
            return "尚未入渊，先定好落脚锚点。"
        if presentation.phase == "pending_defeat_settlement":
            return "你已被压到极限，眼下只能先收束残局。"
        if hp_percent <= 0.18 and mp_percent <= 0.2:
            return "你已逼近极限，再往前很可能当场崩盘。"
        if hp_percent <= 0.18:
            return "你伤得很重，再挨一轮就可能倒下。"
        if hp_percent <= 0.4 and mp_percent <= 0.35:
            return "你气血与灵力都在下滑，继续深入会很险。"
        if hp_percent <= 0.4:
            return "你气息开始乱了，下一轮硬碰会越来越难扛。"
        if mp_percent <= 0.2:
            return "你的灵力快见底了，后手会越来越少。"
        if mp_percent <= 0.4:
            return "你的灵力已开始吃紧，招式不好再放开。"
        if presentation.decision_floor is not None:
            return "这一层刚破，你还能稳住呼吸。"
        if scene_floor is not None and scene_floor.node_type == "anchor_boss":
            return "首领余威还压在身上，气机仍旧发紧。"
        if scene_floor is not None and scene_floor.node_type == "elite":
            return "这一层逼得很紧，但你还站得住。"
        return "你气息还稳，暂时还能再战。"

    @classmethod
    def _build_status_direction_line(cls, *, snapshot: EndlessPanelSnapshot) -> str | None:
        presentation = snapshot.run_presentation
        if not snapshot.run_status.has_active_run:
            return "选好锚点后，便能从那一层开始逐步往下闯。"
        if presentation.decision_floor is not None:
            return "你可以先缓口气，再决定是继续还是暂退。"
        if presentation.phase == "pending_defeat_settlement":
            return "这一层已经失手，眼下只能先执行战败结算。"
        if presentation.next_floor is not None:
            return "继续点击即可逐层推进。"
        return None

    @classmethod
    def _build_encounter_scene_note(
        cls,
        *,
        snapshot: EndlessPanelSnapshot,
        floor_snapshot: EndlessFloorPanelSnapshot,
    ) -> str | None:
        scene_kind = snapshot.run_presentation.current_scene_kind
        if scene_kind == "upcoming_preview":
            return "它们就在前面等你先动。"
        if scene_kind == "decision":
            return "守敌刚散，更深处的动静还在前头翻。"
        if scene_kind == "defeat":
            return "你刚被它们逼退，妖气一时还没散。"
        del floor_snapshot
        return None

    @classmethod
    def _build_reward_result_line(cls, *, floor_snapshot: EndlessFloorPanelSnapshot, scene_kind: str) -> str:
        if scene_kind == "upcoming_preview":
            return f"第 {floor_snapshot.floor} 层还没开打，战果还悬在前头。"
        if floor_snapshot.battle_outcome == "ally_victory":
            return "这一战得胜，你把这一层硬生生踩了过去。"
        if floor_snapshot.battle_outcome == "enemy_victory":
            return "这一战失手，你被这一层硬生生逼停。"
        if floor_snapshot.battle_outcome == "draw":
            return "这一层还僵着，谁也没能立刻压垮谁。"
        return f"第 {floor_snapshot.floor} 层的战果还没有真正落定。"

    @classmethod
    def _build_reward_summary_line(cls, *, reward_mapping: Mapping[str, int]) -> str:
        parts: list[str] = []
        for key in _STABLE_REWARD_ORDER:
            value = max(0, _read_int(reward_mapping.get(key)))
            if value <= 0:
                continue
            parts.append(f"{_STABLE_REWARD_NAME_BY_KEY.get(key, key)} +{value}")
        if not parts:
            return "没有新的稳定收获"
        return "｜".join(parts)

    @staticmethod
    def _build_floor_progress_line(
        *,
        gained: int,
        pending_drop_progress: int,
        claimable_drop_count: int,
    ) -> str:
        if claimable_drop_count > 0:
            return f"掉落进度又涨 {gained}，累计 {pending_drop_progress}，已凝成 {claimable_drop_count} 次掉落。"
        return f"掉落进度又涨 {gained}，累计到了 {pending_drop_progress}。"

    @staticmethod
    def _build_drop_progress_line(*, pending_drop_progress: int, claimable_drop_count: int) -> str:
        if claimable_drop_count > 0:
            return f"累计掉落进度 {pending_drop_progress}，已凝成 {claimable_drop_count} 次掉落。"
        return f"累计掉落进度 {pending_drop_progress}，还得继续往下攒。"

    @staticmethod
    def _build_enemy_brief(*, floor_snapshot: EndlessFloorPanelSnapshot) -> str:
        parts = [
            item
            for item in (floor_snapshot.race_name.strip(), floor_snapshot.template_name.strip())
            if item and item not in {"未知敌类", "未知模板"}
        ]
        if not parts:
            return "这一层守敌"
        return "·".join(parts)

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
                lines.append(f"掉落进度：{formatted}")
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
                and quality_name not in {"玄", "地", "天", "仙"}
            ):
                return None
            if not quality_name:
                return ("法宝" if is_artifact else "异宝") + f"“{display_name}”"
            return ("法宝" if is_artifact else "异宝") + f"“{display_name}”({quality_name})"
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
            title=f"{snapshot.overview.character_name}｜无涯渊境见闻",
            description=cls._build_public_story(snapshot=snapshot, recent_settlement=recent_settlement),
            color=discord.Color.orange(),
        )
        resource_summary = cls._build_public_resource_block(settlement=settlement)
        if resource_summary:
            embed.set_footer(text=resource_summary)
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

    @classmethod
    def _build_public_story(
        cls,
        *,
        snapshot: EndlessPanelSnapshot,
        recent_settlement: EndlessRecentSettlementSnapshot,
    ) -> str:
        settlement = recent_settlement.settlement_result
        segments = [
            (
                f"渊境见闻：{snapshot.overview.character_name}一路闯到第 {settlement.terminated_floor} 层，"
                f"自{settlement.current_region.region_name}带回一身未散的战意"
            )
        ]
        if settlement.terminated_floor > recent_settlement.record_floor_before_run:
            segments.append(
                f"更将个人旧纪录从第 {recent_settlement.record_floor_before_run} 层推至第 {settlement.terminated_floor} 层"
            )
        if recent_settlement.advanced_floor_count > 0:
            segments.append(f"本轮连破 {recent_settlement.advanced_floor_count} 场恶战")
        public_drop_lines = EndlessPanelPresenter._extract_final_drop_lines(
            settlement=settlement,
            public_mode=True,
        )
        if public_drop_lines:
            segments.append("并得" + "、".join(public_drop_lines))
        return "，".join(segments) + "。"

    @staticmethod
    def _build_public_resource_block(*, settlement: EndlessRunSettlementResult) -> str:
        stable_summary = EndlessPanelPresenter._format_reward_mapping_by_keys(
            reward_mapping=settlement.stable_rewards.settled,
            key_order=_STABLE_REWARD_ORDER,
            name_mapping=_STABLE_REWARD_NAME_BY_KEY,
        )
        drop_progress = max(0, _read_int(settlement.pending_rewards.settled.get("drop_progress")))
        if drop_progress <= 0:
            return f"带回：{stable_summary}"
        return f"带回：{stable_summary}｜落宝机缘 {drop_progress}"
        return f"带回：{stable_summary}｜落宝机缘 {drop_progress}"


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
        """推进当前无尽运行一层，并刷新当前层场景面板。"""
        del display_mode
        try:
            advance_presentation, snapshot = self._advance_next_floor(character_id=character_id)
        except (EndlessPanelQueryServiceError, EndlessDungeonServiceError) as exc:
            await self.responder.send_private_error(interaction, message=str(exc))
            return
        await self._edit_panel(
            interaction,
            snapshot=snapshot,
            owner_user_id=owner_user_id,
            selected_start_floor=selected_start_floor,
            display_mode=EndlessDisplayMode.HUB,
            action_note=None,
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
            display_mode=EndlessDisplayMode.HUB,
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
            display_mode=EndlessDisplayMode.HUB,
            action_note=action_note,
        )
        await self._send_public_highlight_if_needed(interaction, snapshot=snapshot)


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
        del display_mode
        view = EndlessPanelView(
            controller=self,
            owner_user_id=owner_user_id,
            character_id=snapshot.overview.character_id,
            snapshot=snapshot,
            selected_start_floor=normalized_start_floor,
            display_mode=EndlessDisplayMode.HUB,
            timeout=self._panel_timeout,
        )
        embed = EndlessPanelPresenter.build_hub_embed(
            snapshot=snapshot,
            selected_start_floor=normalized_start_floor,
            action_note=action_note,
        )
        return PanelMessagePayload(embed=embed, view=view)

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
            await self.responder.send_public_broadcast(interaction.channel, embed=embed)
        except (discord.Forbidden, discord.HTTPException):
            return

    @staticmethod
    def _build_start_lines(*, snapshot: EndlessPanelSnapshot, selected_start_floor: int) -> tuple[str, ...]:
        lines = [f"你已在第 {selected_start_floor} 层锚点落脚。"]
        preview = snapshot.run_presentation.upcoming_floor_preview
        if preview is None:
            if snapshot.run_status.current_floor is not None:
                lines.append(f"踏进去后，第一场厮杀会落在第 {snapshot.run_status.current_floor} 层。")
            lines.append("入内之后，只能一层一层往下闯。")
            return tuple(lines[:3])
        lines.append(f"踏入后会直面第 {preview.floor} 层的{EndlessPanelPresenter._build_enemy_brief(floor_snapshot=preview)}。")
        preview_lines = preview.enemy_scene_lines or preview.enemy_summary_lines
        if len(preview_lines) >= 2:
            lines.append(preview_lines[1])
        elif preview_lines:
            lines.append(preview_lines[0])
        lines.append("入内之后，只能一层一层往下闯。")
        return tuple(lines[:4])

    @staticmethod
    def _build_advance_lines(*, advance_presentation: EndlessAdvancePresentation) -> tuple[str, ...]:
        floor_snapshot = advance_presentation.floor_result
        lines = [f"🏁 第 {floor_snapshot.floor} 层已破。"]
        battle_lines = floor_snapshot.battle_scene_lines
        if battle_lines:
            lines.extend(battle_lines[:2])
        elif floor_snapshot.battle_report_digest is not None and floor_snapshot.battle_report_digest.narration_lines:
            lines.extend(floor_snapshot.battle_report_digest.narration_lines[:2])
        else:
            lines.append(
                EndlessPanelPresenter._build_reward_result_line(
                    floor_snapshot=floor_snapshot,
                    scene_kind="floor_result",
                )
            )
        if floor_snapshot.reward_granted:
            lines.append(
                "✨ 这一层带回 "
                + EndlessPanelPresenter._build_reward_summary_line(
                    reward_mapping=floor_snapshot.stable_reward_summary,
                )
                + "；"
                + EndlessPanelPresenter._build_floor_progress_line(
                    gained=max(0, floor_snapshot.drop_progress_gained),
                    pending_drop_progress=advance_presentation.pending_drop_progress,
                    claimable_drop_count=advance_presentation.claimable_drop_count,
                )
            )
        else:
            lines.append(
                "⚠️ 这一层没能再带回新的收获；"
                + EndlessPanelPresenter._build_drop_progress_line(
                    pending_drop_progress=advance_presentation.pending_drop_progress,
                    claimable_drop_count=advance_presentation.claimable_drop_count,
                )
            )
        if advance_presentation.can_settle_retreat and advance_presentation.decision_floor is not None:
            lines.append("继续点击即可逐层推进；若想收手，就在这里带着收获撤离。")
        elif advance_presentation.stopped_reason == "defeat":
            lines.append("⚠️ 这一层的反扑把你逼停了，得先收束败局。")
        return tuple(lines[:5])

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
            f"掉落进度兑现：{max(0, _read_int(settlement.pending_rewards.settled.get('drop_progress')))}",
        )


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
