"""共享战斗回放叙事构建服务。"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

_MAX_REPLAY_HIGHLIGHTS = 6
_DEFAULT_FRAME_PAUSE_SECONDS = 0.9
_SHORT_FRAME_PAUSE_SECONDS = 0.65
_RESULT_LABEL_BY_VALUE = {
    "ally_victory": "胜势已定",
    "enemy_victory": "败退收场",
    "draw": "鏖战未分",
}


@dataclass(frozen=True, slots=True)
class BattleReplayDisplayContext:
    """构建战斗回放时的展示上下文。"""

    source_name: str
    scene_name: str
    group_name: str | None = None
    environment_name: str | None = None
    focus_unit_name: str | None = None


@dataclass(frozen=True, slots=True)
class BattleReplayFrame:
    """单条消息一次累计编辑后的完整帧。"""

    title: str
    lines: tuple[str, ...]
    footer: str
    pause_seconds: float = 0.0


@dataclass(frozen=True, slots=True)
class BattleReplayPresentation:
    """适合 Discord 单条消息回放的战斗演出结果。"""

    battle_report_id: int
    result: str
    focus_unit_name: str
    summary_line: str
    highlight_lines: tuple[str, ...]
    frames: tuple[BattleReplayFrame, ...]


@dataclass(frozen=True, slots=True)
class _RoundHighlight:
    round_index: int
    score: int
    line: str


class BattleReplayService:
    """把结构化战报转换为可回放的游戏化文案。"""

    def build_presentation(
        self,
        *,
        battle_report_id: int,
        result: str,
        summary_payload: Mapping[str, Any],
        detail_payload: Mapping[str, Any],
        context: BattleReplayDisplayContext,
    ) -> BattleReplayPresentation | None:
        """构建适合单条消息累计编辑的回放帧。"""
        normalized_summary = _normalize_mapping(summary_payload)
        normalized_detail = _normalize_mapping(detail_payload)
        focus_unit_name = str(
            context.focus_unit_name
            or normalized_summary.get("focus_unit_name")
            or "你"
        )
        completed_rounds = _read_int(normalized_summary.get("completed_rounds"))
        final_hp_ratio = str(normalized_summary.get("final_hp_ratio") or "0.0000")
        title = f"{context.source_name}｜{context.scene_name}"[:120]
        opening_line = self._build_opening_line(
            detail_payload=normalized_detail,
            context=context,
            focus_unit_name=focus_unit_name,
        )
        highlight_candidates = self._extract_round_highlights(
            detail_payload=normalized_detail,
            focus_unit_name=focus_unit_name,
        )
        highlight_lines = self._compress_highlights(
            highlight_candidates=highlight_candidates,
            completed_rounds=completed_rounds,
        )
        closing_line = self._build_closing_line(
            result=result,
            completed_rounds=completed_rounds,
            summary_payload=normalized_summary,
            focus_unit_name=focus_unit_name,
        )
        summary_line = self._build_summary_line(
            result=result,
            completed_rounds=completed_rounds,
            final_hp_ratio=final_hp_ratio,
        )
        frames = self._build_frames(
            title=title,
            opening_line=opening_line,
            highlight_lines=highlight_lines,
            closing_line=closing_line,
            summary_line=summary_line,
        )
        if not frames:
            return None
        return BattleReplayPresentation(
            battle_report_id=battle_report_id,
            result=result,
            focus_unit_name=focus_unit_name,
            summary_line=summary_line,
            highlight_lines=highlight_lines,
            frames=frames,
        )

    def _build_opening_line(
        self,
        *,
        detail_payload: Mapping[str, Any],
        context: BattleReplayDisplayContext,
        focus_unit_name: str,
    ) -> str:
        enemy_names = self._resolve_enemy_names(detail_payload=detail_payload)
        enemy_segment = "守关敌影" if not enemy_names else "、".join(enemy_names[:2])
        if len(enemy_names) > 2:
            enemy_segment = f"{enemy_segment}等敌影"
        environment_segment = str(context.environment_name or "四周灵压骤然收拢")
        group_segment = ""
        if context.group_name:
            group_segment = f"，这片{context.group_name}的杀机先一步压了下来"
        focus_text = self._display_unit_name(unit_name=focus_unit_name, focus_unit_name=focus_unit_name)
        return (
            f"🌌 {focus_text}踏入“{context.scene_name}”{group_segment}。"
            f"{environment_segment}，拦在前方的正是{enemy_segment}。"
        )

    def _extract_round_highlights(
        self,
        *,
        detail_payload: Mapping[str, Any],
        focus_unit_name: str,
    ) -> tuple[_RoundHighlight, ...]:
        event_sequence = _normalize_mapping_list(detail_payload.get("event_sequence"))
        if not event_sequence:
            return ()
        unit_name_by_id = self._build_unit_name_mapping(detail_payload=detail_payload)
        unit_side_by_id = self._build_unit_side_mapping(detail_payload=detail_payload)
        action_label_by_id = self._build_action_label_mapping(detail_payload=detail_payload)
        events_by_round: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for event in event_sequence:
            round_index = _read_int(event.get("round_index"))
            if round_index <= 0:
                continue
            events_by_round[round_index].append(event)
        highlights: list[_RoundHighlight] = []
        for round_index in sorted(events_by_round):
            round_events = sorted(events_by_round[round_index], key=lambda item: _read_int(item.get("sequence")))
            strongest_candidate: _RoundHighlight | None = None
            current_action_by_actor: dict[str, str] = {}
            pending_critical_hits: dict[tuple[str, str, str], int] = defaultdict(int)
            defeated_targets = {
                str(event.get("target_unit_id") or "")
                for event in round_events
                if str(event.get("event_type") or "") == "unit_defeated"
            }
            for event in round_events:
                event_type = str(event.get("event_type") or "")
                detail = _normalize_optional_mapping(event.get("detail")) or {}
                actor_id = str(event.get("actor_unit_id") or "")
                target_id = str(event.get("target_unit_id") or "")
                action_id = str(event.get("action_id") or "")
                if event_type in {"action_selected", "action_started"}:
                    if actor_id:
                        current_action_by_actor[actor_id] = action_label_by_id.get(action_id, "一式攻势")
                    continue
                if event_type == "crit_check" and actor_id and target_id and bool(detail.get("success")):
                    pending_critical_hits[(actor_id, target_id, action_id)] += 1
                    continue
                candidate = self._build_round_candidate(
                    event_type=event_type,
                    detail=detail,
                    actor_id=actor_id,
                    target_id=target_id,
                    action_id=action_id,
                    defeated_targets=defeated_targets,
                    current_action_by_actor=current_action_by_actor,
                    pending_critical_hits=pending_critical_hits,
                    unit_name_by_id=unit_name_by_id,
                    unit_side_by_id=unit_side_by_id,
                    action_label_by_id=action_label_by_id,
                    focus_unit_name=focus_unit_name,
                    round_index=round_index,
                )
                if candidate is None:
                    continue
                if strongest_candidate is None or candidate.score > strongest_candidate.score:
                    strongest_candidate = candidate
            if strongest_candidate is not None:
                highlights.append(strongest_candidate)
        return tuple(highlights)

    def _build_round_candidate(
        self,
        *,
        event_type: str,
        detail: Mapping[str, Any],
        actor_id: str,
        target_id: str,
        action_id: str,
        defeated_targets: set[str],
        current_action_by_actor: Mapping[str, str],
        pending_critical_hits: dict[tuple[str, str, str], int],
        unit_name_by_id: Mapping[str, str],
        unit_side_by_id: Mapping[str, str],
        action_label_by_id: Mapping[str, str],
        focus_unit_name: str,
        round_index: int,
    ) -> _RoundHighlight | None:
        actor_name = self._display_unit_name(
            unit_name=unit_name_by_id.get(actor_id, actor_id or "未知修士"),
            focus_unit_name=focus_unit_name,
        )
        target_name = self._display_unit_name(
            unit_name=unit_name_by_id.get(target_id, target_id or "目标"),
            focus_unit_name=focus_unit_name,
        )
        action_label = current_action_by_actor.get(actor_id, action_label_by_id.get(action_id, "一式攻势"))
        actor_side = unit_side_by_id.get(actor_id, "")
        target_side = unit_side_by_id.get(target_id, "")
        if event_type == "damage_resolved":
            damage = _read_int(detail.get("final_damage"))
            if damage <= 0 or not actor_id or not target_id:
                return None
            crit_key = (actor_id, target_id, action_id)
            is_critical = pending_critical_hits.get(crit_key, 0) > 0
            if is_critical:
                pending_critical_hits[crit_key] -= 1
            if actor_side == "ally":
                if target_id in defeated_targets:
                    event_emoji = "💥" if is_critical else "⚔️"
                    crit_text = "暴击" if is_critical else "重击"
                    line = f"{event_emoji} {actor_name}催动{action_label}斩出 {damage} 点{crit_text}，{target_name}当场溃散。"
                    score = damage + (4200 if is_critical else 3000)
                elif is_critical:
                    line = f"💥 {actor_name}借{action_label}轰出 {damage} 点暴击，逼得{target_name}气机大乱。"
                    score = damage + 2200
                else:
                    line = f"⚔️ {actor_name}以{action_label}斩落 {damage} 点伤害，压得{target_name}步步后退。"
                    score = damage + 1200
                return _RoundHighlight(round_index=round_index, score=score, line=line)
            if actor_side == "enemy":
                if is_critical:
                    line = f"⚠️ {actor_name}猛地反压过来，{target_name}硬吃 {damage} 点暴击，气血都被震散。"
                    score = damage + 2100
                else:
                    line = f"⚠️ {actor_name}抢下一记先手，{target_name}被打掉 {damage} 点气血。"
                    score = damage + 900
                return _RoundHighlight(round_index=round_index, score=score, line=line)
            return _RoundHighlight(
                round_index=round_index,
                score=damage,
                line=f"⚔️ 场中忽有一击落下，{target_name}被轰掉 {damage} 点气血。",
            )
        if event_type == "healing_applied":
            healed = _read_int(detail.get("healed_hp"))
            if healed <= 0 or not actor_id:
                return None
            if actor_side == "ally":
                line = f"💚 {actor_name}借{action_label}回稳 {healed} 点气血，硬是把将散的气息拽了回来。"
                score = healed + 780
            else:
                line = f"💚 {actor_name}缓回 {healed} 点气血，原本摇晃的架势又稳住了。"
                score = healed + 360
            return _RoundHighlight(round_index=round_index, score=score, line=line)
        if event_type == "turn_skipped_by_control" and actor_id:
            if actor_side == "enemy":
                line = f"🌀 {actor_name}神魂一滞，被压得整轮都没能真正出手。"
                score = 900
            else:
                line = f"🌀 {actor_name}气机忽乱，原本要递出的攻势被硬生生截断。"
                score = 760
            return _RoundHighlight(round_index=round_index, score=score, line=line)
        if event_type == "counter_triggered" and actor_id and target_id:
            if actor_side == "ally":
                line = f"🔁 {actor_name}借势回身一震，{target_name}刚起的攻势被当场反撕回去。"
                score = 920
            else:
                line = f"🔁 {actor_name}顺手反震一击，逼得{target_name}连忙收势。"
                score = 720
            return _RoundHighlight(round_index=round_index, score=score, line=line)
        if event_type == "pursuit_triggered" and actor_id and target_id:
            if actor_side == "ally":
                line = f"🔻 {actor_name}杀势未尽，身形一错，又朝{target_name}补上后手。"
                score = 860
            else:
                line = f"🔻 {actor_name}凶性不收，追着{target_name}又补了一击。"
                score = 640
            return _RoundHighlight(round_index=round_index, score=score, line=line)
        if event_type == "damage_over_time_tick" and target_id:
            damage = max(_read_int(detail.get("hp_damage")), _read_int(detail.get("total_damage")))
            if damage <= 0:
                return None
            if target_side == "enemy":
                line = f"🩸 残留劲气继续翻涌，又从{target_name}身上撕下 {damage} 点气血。"
                score = damage + 500
            else:
                line = f"🩸 残存煞气还在体内翻滚，{target_name}又被蚀走 {damage} 点气血。"
                score = damage + 420
            return _RoundHighlight(round_index=round_index, score=score, line=line)
        if event_type == "special_effect_triggered" and actor_id:
            effect_type = str(detail.get("effect_type") or "")
            if actor_side == "ally":
                line = f"✨ {actor_name}身上异象一闪，{self._build_effect_story(effect_type=effect_type, ally_side=True)}"
                score = 420
            else:
                line = f"✨ {actor_name}忽然异光暴涨，{self._build_effect_story(effect_type=effect_type, ally_side=False)}"
                score = 320
            return _RoundHighlight(round_index=round_index, score=score, line=line)
        if event_type == "unit_defeated" and target_id and target_id not in defeated_targets:
            return _RoundHighlight(
                round_index=round_index,
                score=1800,
                line=f"☠️ {target_name}气息断绝，身形彻底从战局里垮了下去。",
            )
        return None

    @staticmethod
    def _build_effect_story(*, effect_type: str, ally_side: bool) -> str:
        if effect_type == "attribute_suppression":
            return "对面的气机被压得明显一沉。" if ally_side else "压迫感顿时更重了几分。"
        if effect_type == "shield_on_hit":
            return "护体灵光顺势撑起一层新幕。" if ally_side else "护体灵光猛地又厚了一层。"
        if effect_type == "damage_on_hit":
            return "余劲顺着攻势一并炸开。" if ally_side else "残余反震又跟着扑了上来。"
        return "场上的灵压忽然被再推高了一截。"

    def _compress_highlights(
        self,
        *,
        highlight_candidates: Sequence[_RoundHighlight],
        completed_rounds: int,
    ) -> tuple[str, ...]:
        if not highlight_candidates:
            return ()
        max_highlights = min(_MAX_REPLAY_HIGHLIGHTS, max(3, 2 + min(4, max(0, completed_rounds // 2))))
        ordered_candidates = sorted(highlight_candidates, key=lambda item: item.round_index)
        if len(ordered_candidates) <= max_highlights:
            return tuple(item.line for item in ordered_candidates)
        selected_by_round: dict[int, _RoundHighlight] = {
            ordered_candidates[0].round_index: ordered_candidates[0],
            ordered_candidates[-1].round_index: ordered_candidates[-1],
        }
        remaining = [
            item for item in ordered_candidates
            if item.round_index not in selected_by_round
        ]
        for item in sorted(remaining, key=lambda current: (-current.score, current.round_index)):
            if len(selected_by_round) >= max_highlights:
                break
            selected_by_round[item.round_index] = item
        selected = sorted(selected_by_round.values(), key=lambda item: item.round_index)
        return tuple(item.line for item in selected[:max_highlights])

    def _build_closing_line(
        self,
        *,
        result: str,
        completed_rounds: int,
        summary_payload: Mapping[str, Any],
        focus_unit_name: str,
    ) -> str:
        critical_hits = _read_int(_normalize_int_mapping(summary_payload.get("key_trigger_counts")).get("critical_hits"))
        damage_summary = _normalize_int_mapping(summary_payload.get("damage_summary"))
        healing_summary = _normalize_int_mapping(summary_payload.get("healing_summary"))
        ally_damage_dealt = _read_int(damage_summary.get("ally_damage_dealt"))
        ally_damage_taken = _read_int(damage_summary.get("ally_damage_taken"))
        ally_healing_done = _read_int(healing_summary.get("ally_healing_done"))
        focus_text = self._display_unit_name(unit_name=focus_unit_name, focus_unit_name=focus_unit_name)
        round_text = max(1, completed_rounds)
        if result == "ally_victory":
            if ally_healing_done > 0 and ally_damage_taken >= max(1, ally_damage_dealt // 2):
                return f"🏁 苦战 {round_text} 回合后，{focus_text}总算稳住最后一口气，把这场试炼硬生生拖成了自己的胜局。"
            if critical_hits >= 2:
                return f"🏁 打到第 {round_text} 回合，对面的压阵气机终于被{focus_text}一口气斩碎，这一关算是彻底拿下。"
            return f"🏁 {round_text} 回合鏖战之后，守关威压终于崩开，{focus_text}把这场试炼压到了最后。"
        if result == "enemy_victory":
            return f"🏁 苦撑到第 {round_text} 回合，{focus_text}还是被这场试炼的反扑逼退，只能先收势后撤。"
        return f"⏳ 连战 {round_text} 回合后，双方气机都乱成一团，这一场暂时还没真正分出最后高下。"

    @staticmethod
    def _build_summary_line(
        *,
        result: str,
        completed_rounds: int,
        final_hp_ratio: str,
    ) -> str:
        return (
            f"{_RESULT_LABEL_BY_VALUE.get(result, '战局已定')}"
            f"｜{max(1, completed_rounds)} 回合"
            f"｜余留气血 {_format_ratio(final_hp_ratio)}"
        )

    def _build_frames(
        self,
        *,
        title: str,
        opening_line: str,
        highlight_lines: Sequence[str],
        closing_line: str,
        summary_line: str,
    ) -> tuple[BattleReplayFrame, ...]:
        frames: list[BattleReplayFrame] = []
        accumulated_lines = [opening_line]
        if highlight_lines:
            first_take = 1 if len(highlight_lines) > 2 else len(highlight_lines)
            accumulated_lines.extend(highlight_lines[:first_take])
            frames.append(
                BattleReplayFrame(
                    title=title,
                    lines=tuple(accumulated_lines),
                    footer=f"战后回放｜{summary_line}",
                    pause_seconds=_SHORT_FRAME_PAUSE_SECONDS,
                )
            )
            cursor = first_take
            while cursor < len(highlight_lines):
                step = min(2, len(highlight_lines) - cursor)
                accumulated_lines.extend(highlight_lines[cursor : cursor + step])
                cursor += step
                frames.append(
                    BattleReplayFrame(
                        title=title,
                        lines=tuple(accumulated_lines),
                        footer=f"战后回放｜{summary_line}",
                        pause_seconds=_DEFAULT_FRAME_PAUSE_SECONDS,
                    )
                )
        final_lines = tuple((*accumulated_lines, closing_line))
        if not frames or frames[-1].lines != final_lines:
            frames.append(
                BattleReplayFrame(
                    title=title,
                    lines=final_lines,
                    footer=f"战后回放｜{summary_line}",
                    pause_seconds=0.0,
                )
            )
        return tuple(frames)

    @staticmethod
    def _display_unit_name(*, unit_name: str, focus_unit_name: str) -> str:
        if unit_name and unit_name == focus_unit_name:
            return "你"
        return unit_name or "未知修士"

    @staticmethod
    def _resolve_enemy_names(detail_payload: Mapping[str, Any]) -> tuple[str, ...]:
        snapshot_summary = _normalize_optional_mapping(detail_payload.get("input_snapshot_summary")) or {}
        enemies = _normalize_mapping_list(snapshot_summary.get("enemies"))
        names = [
            str(item.get("unit_name") or "")
            for item in enemies
            if str(item.get("unit_name") or "")
        ]
        return tuple(names)

    @staticmethod
    def _build_unit_name_mapping(*, detail_payload: Mapping[str, Any]) -> dict[str, str]:
        mapping: dict[str, str] = {}
        snapshot_summary = _normalize_optional_mapping(detail_payload.get("input_snapshot_summary")) or {}
        for key in ("allies", "enemies"):
            for item in _normalize_mapping_list(snapshot_summary.get(key)):
                unit_id = str(item.get("unit_id") or "")
                unit_name = str(item.get("unit_name") or "")
                if unit_id and unit_name:
                    mapping[unit_id] = unit_name
        terminal_statistics = _normalize_optional_mapping(detail_payload.get("terminal_statistics")) or {}
        for item in _normalize_mapping_list(terminal_statistics.get("final_units")):
            unit_id = str(item.get("unit_id") or "")
            unit_name = str(item.get("unit_name") or "")
            if unit_id and unit_name:
                mapping[unit_id] = unit_name
        return mapping

    @staticmethod
    def _build_unit_side_mapping(*, detail_payload: Mapping[str, Any]) -> dict[str, str]:
        mapping: dict[str, str] = {}
        snapshot_summary = _normalize_optional_mapping(detail_payload.get("input_snapshot_summary")) or {}
        for key in ("allies", "enemies"):
            for item in _normalize_mapping_list(snapshot_summary.get(key)):
                unit_id = str(item.get("unit_id") or "")
                side = str(item.get("side") or "")
                if unit_id and side:
                    mapping[unit_id] = side
        terminal_statistics = _normalize_optional_mapping(detail_payload.get("terminal_statistics")) or {}
        for item in _normalize_mapping_list(terminal_statistics.get("final_units")):
            unit_id = str(item.get("unit_id") or "")
            side = str(item.get("side") or "")
            if unit_id and side:
                mapping[unit_id] = side
        return mapping

    @staticmethod
    def _build_action_label_mapping(*, detail_payload: Mapping[str, Any]) -> dict[str, str]:
        mapping: dict[str, str] = {}
        terminal_statistics = _normalize_optional_mapping(detail_payload.get("terminal_statistics")) or {}
        for template in _normalize_mapping_list(terminal_statistics.get("behavior_templates")):
            for action in _normalize_mapping_list(template.get("actions")):
                action_id = str(action.get("action_id") or "")
                action_name = str(action.get("name") or "")
                if action_id and action_name:
                    mapping[action_id] = action_name
        return mapping



def _normalize_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    return {}



def _normalize_optional_mapping(value: Any) -> dict[str, Any] | None:
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    return None



def _normalize_mapping_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    normalized: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, Mapping):
            normalized.append({str(key): entry for key, entry in item.items()})
    return normalized



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



def _format_ratio(value: Any) -> str:
    try:
        return f"{float(value) * 100:.1f}%"
    except (TypeError, ValueError):
        return "0.0%"


__all__ = [
    "BattleReplayDisplayContext",
    "BattleReplayFrame",
    "BattleReplayPresentation",
    "BattleReplayService",
]
