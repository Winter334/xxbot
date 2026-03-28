"""战报生成与战损评估。"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json

from domain.battle.models import (
    BattleEvent,
    BattleOutcome,
    BattleRandomCall,
    BattleResult,
    BattleSide,
    BattleSnapshot,
    BattleStatusEffect,
    BattleUnitState,
    CompiledBehaviorTemplate,
)

_REPORT_SCHEMA_VERSION = "1.0.0"
_RATIO_QUANTIZE = Decimal("0.0001")
_HP_HEAVY_THRESHOLD = Decimal("0.2500")
_HP_MEDIUM_THRESHOLD = Decimal("0.5000")
_HP_LIGHT_THRESHOLD = Decimal("0.8000")
_MP_DRY_THRESHOLD = Decimal("0.1000")
_MP_LOW_THRESHOLD = Decimal("0.2500")
_MP_LIGHT_THRESHOLD = Decimal("0.3500")
_STATUS_CHANGE_EVENT_TYPES = frozenset(
    {
        "status_applied",
        "status_replaced",
        "status_stacked",
        "status_refreshed",
        "status_expired",
        "status_kept_existing",
        "turn_skipped_by_control",
    }
)
_STATUS_MUTATION_EVENT_TYPES = frozenset(
    {
        "status_applied",
        "status_replaced",
        "status_stacked",
        "status_refreshed",
        "status_expired",
    }
)
_REACTION_EVENT_TYPES = frozenset({"pursuit_triggered", "counter_check", "counter_triggered"})


@dataclass(frozen=True, slots=True)
class BattleReportSummary:
    """可持久化的战斗摘要。"""

    schema_version: str
    result: str
    outcome: str
    completed_rounds: int
    main_path_id: str
    main_paths: tuple[dict[str, object], ...]
    key_trigger_counts: dict[str, int]
    damage_summary: dict[str, int]
    healing_summary: dict[str, int]
    final_hp_ratio: str
    final_mp_ratio: str
    seed: int
    template_config_version: str
    snapshot_summary_hash: str
    focus_unit_id: str
    focus_unit_name: str

    def to_payload(self) -> dict[str, object]:
        """导出适合写入 JSON 字段的摘要载荷。"""
        return _json_ready(
            {
                "schema_version": self.schema_version,
                "result": self.result,
                "outcome": self.outcome,
                "completed_rounds": self.completed_rounds,
                "main_path_id": self.main_path_id,
                "main_paths": self.main_paths,
                "key_trigger_counts": self.key_trigger_counts,
                "damage_summary": self.damage_summary,
                "healing_summary": self.healing_summary,
                "final_hp_ratio": self.final_hp_ratio,
                "final_mp_ratio": self.final_mp_ratio,
                "seed": self.seed,
                "template_config_version": self.template_config_version,
                "snapshot_summary_hash": self.snapshot_summary_hash,
                "focus_unit_id": self.focus_unit_id,
                "focus_unit_name": self.focus_unit_name,
            }
        )


@dataclass(frozen=True, slots=True)
class BattleReportDetail:
    """可持久化的战斗明细。"""

    schema_version: str
    seed: int
    template_config_version: str
    snapshot_summary_hash: str
    focus_unit_id: str
    environment_snapshot: dict[str, str | int | bool | None]
    input_snapshot_summary: dict[str, object]
    rounds: tuple[dict[str, object], ...]
    terminal_statistics: dict[str, object]
    random_calls: tuple[dict[str, object], ...]
    event_sequence: tuple[dict[str, object], ...]

    def to_payload(self) -> dict[str, object]:
        """导出适合写入 JSON 字段的明细载荷。"""
        return _json_ready(
            {
                "schema_version": self.schema_version,
                "seed": self.seed,
                "template_config_version": self.template_config_version,
                "snapshot_summary_hash": self.snapshot_summary_hash,
                "focus_unit_id": self.focus_unit_id,
                "environment_snapshot": self.environment_snapshot,
                "input_snapshot_summary": self.input_snapshot_summary,
                "rounds": self.rounds,
                "terminal_statistics": self.terminal_statistics,
                "random_calls": self.random_calls,
                "event_sequence": self.event_sequence,
            }
        )


@dataclass(frozen=True, slots=True)
class BattleLossResult:
    """战斗结束后的标准化战损结果。"""

    final_hp_ratio: str
    final_mp_ratio: str
    injury_level: str
    can_continue: bool
    loss_tags: tuple[str, ...]

    def to_payload(self) -> dict[str, object]:
        """导出战损结果的序列化载荷。"""
        return _json_ready(
            {
                "final_hp_ratio": self.final_hp_ratio,
                "final_mp_ratio": self.final_mp_ratio,
                "injury_level": self.injury_level,
                "can_continue": self.can_continue,
                "loss_tags": self.loss_tags,
            }
        )

    def to_progress_update_payload(self) -> dict[str, str]:
        """导出角色当前血蓝比回写所需的载荷。"""
        return {
            "current_hp_ratio": self.final_hp_ratio,
            "current_mp_ratio": self.final_mp_ratio,
        }


@dataclass(frozen=True, slots=True)
class BattleReportArtifacts:
    """战报摘要、明细与战损对象的聚合结果。"""

    summary: BattleReportSummary
    detail: BattleReportDetail
    loss: BattleLossResult


class BattleLossEvaluator:
    """根据终局单位状态生成标准化战损结果。"""

    def evaluate(
        self,
        *,
        final_units: Sequence[BattleUnitState],
        focus_unit_id: str,
    ) -> BattleLossResult:
        """对指定焦点单位生成战损评估。"""
        focus_unit = _require_unit(final_units=final_units, focus_unit_id=focus_unit_id)
        hp_ratio = _resolve_ratio_decimal(focus_unit.current_hp, focus_unit.base_snapshot.max_hp)
        mp_ratio = _resolve_ratio_decimal(focus_unit.current_resource, focus_unit.base_snapshot.max_resource)

        if not focus_unit.is_alive or focus_unit.current_hp <= 0:
            injury_level = "defeated"
        elif hp_ratio < _HP_HEAVY_THRESHOLD:
            injury_level = "heavy"
        elif hp_ratio < _HP_MEDIUM_THRESHOLD or mp_ratio < _MP_DRY_THRESHOLD:
            injury_level = "medium"
        elif hp_ratio < _HP_LIGHT_THRESHOLD or mp_ratio < _MP_LIGHT_THRESHOLD:
            injury_level = "light"
        else:
            injury_level = "none"

        loss_tags: list[str] = []
        if not focus_unit.is_alive:
            loss_tags.append("defeated")
        if focus_unit.current_hp <= 0:
            loss_tags.append("hp_depleted")
        elif hp_ratio < _HP_HEAVY_THRESHOLD:
            loss_tags.append("hp_critical")
        elif hp_ratio < _HP_MEDIUM_THRESHOLD:
            loss_tags.append("hp_low")
        if focus_unit.base_snapshot.max_resource > 0:
            if focus_unit.current_resource <= 0:
                loss_tags.append("mp_depleted")
            elif mp_ratio < _MP_LOW_THRESHOLD:
                loss_tags.append("mp_low")
        if focus_unit.base_snapshot.current_shield > 0 and focus_unit.current_shield <= 0:
            loss_tags.append("shield_lost")

        can_continue = focus_unit.is_alive and hp_ratio >= _HP_HEAVY_THRESHOLD
        if focus_unit.base_snapshot.max_resource > 0:
            can_continue = can_continue and mp_ratio >= _MP_DRY_THRESHOLD

        return BattleLossResult(
            final_hp_ratio=_format_ratio(hp_ratio),
            final_mp_ratio=_format_ratio(mp_ratio),
            injury_level=injury_level,
            can_continue=can_continue,
            loss_tags=tuple(loss_tags),
        )


class BattleReportBuilder:
    """根据结构化战斗结果构建摘要、明细与战损对象。"""

    def __init__(self, *, loss_evaluator: BattleLossEvaluator | None = None) -> None:
        self._loss_evaluator = loss_evaluator or BattleLossEvaluator()

    def build(
        self,
        *,
        snapshot: BattleSnapshot,
        result: BattleResult,
        behavior_templates: Mapping[str, CompiledBehaviorTemplate],
        template_config_version: str,
        focus_unit_id: str,
        environment_snapshot: Mapping[str, str | int | bool | None] | None = None,
    ) -> BattleReportArtifacts:
        """构建完整战报对象。"""
        focus_unit = _require_unit(final_units=result.final_units, focus_unit_id=focus_unit_id)
        normalized_environment = _normalize_environment_snapshot(environment_snapshot)
        snapshot_summary = self._build_snapshot_summary(
            snapshot=snapshot,
            behavior_templates=behavior_templates,
        )
        snapshot_summary_hash = _hash_payload(snapshot_summary)
        loss_result = self._loss_evaluator.evaluate(
            final_units=result.final_units,
            focus_unit_id=focus_unit_id,
        )
        summary = self._build_summary(
            result=result,
            focus_unit=focus_unit,
            focus_unit_id=focus_unit_id,
            template_config_version=template_config_version,
            snapshot_summary_hash=snapshot_summary_hash,
            loss_result=loss_result,
            seed=snapshot.seed,
        )
        detail = self._build_detail(
            snapshot=snapshot,
            result=result,
            focus_unit_id=focus_unit_id,
            behavior_templates=behavior_templates,
            template_config_version=template_config_version,
            snapshot_summary=snapshot_summary,
            snapshot_summary_hash=snapshot_summary_hash,
            environment_snapshot=normalized_environment,
        )
        return BattleReportArtifacts(summary=summary, detail=detail, loss=loss_result)

    def _build_summary(
        self,
        *,
        result: BattleResult,
        focus_unit: BattleUnitState,
        focus_unit_id: str,
        template_config_version: str,
        snapshot_summary_hash: str,
        loss_result: BattleLossResult,
        seed: int,
    ) -> BattleReportSummary:
        action_usage_by_actor = _build_action_usage(result.events)
        key_trigger_counts = self._build_key_trigger_counts(result.events)
        damage_summary, healing_summary = self._build_damage_and_healing_summary(result=result)
        main_paths = tuple(
            self._build_main_path_payload(
                unit=unit,
                action_usage=action_usage_by_actor.get(unit.unit_id),
            )
            for unit in result.final_units
            if unit.side is BattleSide.ALLY
        )
        return BattleReportSummary(
            schema_version=_REPORT_SCHEMA_VERSION,
            result=_resolve_perspective_result(outcome=result.outcome, focus_side=focus_unit.side),
            outcome=result.outcome.value,
            completed_rounds=result.completed_rounds,
            main_path_id=focus_unit.behavior_template.path_id,
            main_paths=main_paths,
            key_trigger_counts=key_trigger_counts,
            damage_summary=damage_summary,
            healing_summary=healing_summary,
            final_hp_ratio=loss_result.final_hp_ratio,
            final_mp_ratio=loss_result.final_mp_ratio,
            seed=seed,
            template_config_version=template_config_version,
            snapshot_summary_hash=snapshot_summary_hash,
            focus_unit_id=focus_unit_id,
            focus_unit_name=focus_unit.unit_name,
        )

    def _build_detail(
        self,
        *,
        snapshot: BattleSnapshot,
        result: BattleResult,
        focus_unit_id: str,
        behavior_templates: Mapping[str, CompiledBehaviorTemplate],
        template_config_version: str,
        snapshot_summary: dict[str, object],
        snapshot_summary_hash: str,
        environment_snapshot: dict[str, str | int | bool | None],
    ) -> BattleReportDetail:
        event_sequence = tuple(_normalize_event(event) for event in result.events)
        random_calls = tuple(_normalize_random_call(call) for call in result.random_calls)
        rounds = self._build_round_payloads(events=result.events)
        terminal_statistics = self._build_terminal_statistics(
            result=result,
            behavior_templates=behavior_templates,
        )
        return BattleReportDetail(
            schema_version=_REPORT_SCHEMA_VERSION,
            seed=snapshot.seed,
            template_config_version=template_config_version,
            snapshot_summary_hash=snapshot_summary_hash,
            focus_unit_id=focus_unit_id,
            environment_snapshot=environment_snapshot,
            input_snapshot_summary=snapshot_summary,
            rounds=rounds,
            terminal_statistics=terminal_statistics,
            random_calls=random_calls,
            event_sequence=event_sequence,
        )

    @staticmethod
    def _build_key_trigger_counts(events: Sequence[BattleEvent]) -> dict[str, int]:
        hit_checks = [event for event in events if event.event_type == "hit_check"]
        crit_checks = [event for event in events if event.event_type == "crit_check"]
        successful_hits = sum(1 for event in hit_checks if _event_detail_bool(event, "success"))
        critical_hits = sum(1 for event in crit_checks if _event_detail_bool(event, "success"))
        return {
            "successful_hits": successful_hits,
            "missed_hits": len(hit_checks) - successful_hits,
            "critical_hits": critical_hits,
            "pursuit_triggered": sum(1 for event in events if event.event_type == "pursuit_triggered"),
            "counter_triggered": sum(1 for event in events if event.event_type == "counter_triggered"),
            "special_effect_triggered": sum(1 for event in events if event.event_type == "special_effect_triggered"),
            "status_changed": sum(1 for event in events if event.event_type in _STATUS_MUTATION_EVENT_TYPES),
            "control_skips": sum(1 for event in events if event.event_type == "turn_skipped_by_control"),
            "unit_defeated": sum(1 for event in events if event.event_type == "unit_defeated"),
        }

    @staticmethod
    def _build_damage_and_healing_summary(*, result: BattleResult) -> tuple[dict[str, int], dict[str, int]]:
        side_map = {unit.unit_id: unit.side.value for unit in result.final_units}
        damage_summary = {
            "ally_damage_dealt": 0,
            "ally_damage_taken": 0,
            "enemy_damage_dealt": 0,
            "enemy_damage_taken": 0,
        }
        healing_summary = {
            "ally_healing_done": 0,
            "ally_healing_received": 0,
            "enemy_healing_done": 0,
            "enemy_healing_received": 0,
        }
        for item in result.statistics.unit_statistics:
            side = side_map[item.unit_id]
            damage_summary[f"{side}_damage_dealt"] += item.damage_dealt
            damage_summary[f"{side}_damage_taken"] += item.damage_taken
            healing_summary[f"{side}_healing_done"] += item.healing_done
            healing_summary[f"{side}_healing_received"] += item.healing_received
        return damage_summary, healing_summary

    @staticmethod
    def _build_main_path_payload(
        *,
        unit: BattleUnitState,
        action_usage: Counter[str] | None,
    ) -> dict[str, object]:
        ordered_usage = ()
        if action_usage:
            ordered_usage = tuple(
                {
                    "action_id": action_id,
                    "count": count,
                }
                for action_id, count in sorted(action_usage.items())
            )
        return {
            "unit_id": unit.unit_id,
            "unit_name": unit.unit_name,
            "template_id": unit.behavior_template.template_id,
            "path_id": unit.behavior_template.path_id,
            "axis_id": unit.behavior_template.axis_id,
            "template_name": unit.behavior_template.name,
            "template_tags": unit.behavior_template.template_tags,
            "applied_patch_ids": unit.behavior_template.applied_patch_ids,
            "actions_used": ordered_usage,
        }

    def _build_snapshot_summary(
        self,
        *,
        snapshot: BattleSnapshot,
        behavior_templates: Mapping[str, CompiledBehaviorTemplate],
    ) -> dict[str, object]:
        return {
            "seed": snapshot.seed,
            "round_limit": snapshot.round_limit,
            "environment_tags": snapshot.environment_tags,
            "allies": tuple(
                self._build_snapshot_unit_payload(unit=unit, behavior_templates=behavior_templates)
                for unit in snapshot.allies
            ),
            "enemies": tuple(
                self._build_snapshot_unit_payload(unit=unit, behavior_templates=behavior_templates)
                for unit in snapshot.enemies
            ),
        }

    @staticmethod
    def _build_snapshot_unit_payload(
        *,
        unit,
        behavior_templates: Mapping[str, CompiledBehaviorTemplate],
    ) -> dict[str, object]:
        template = behavior_templates[unit.behavior_template_id]
        return {
            "unit_id": unit.unit_id,
            "unit_name": unit.unit_name,
            "side": unit.side.value,
            "behavior_template_id": unit.behavior_template_id,
            "path_id": template.path_id,
            "axis_id": template.axis_id,
            "template_name": template.name,
            "template_tags": template.template_tags,
            "applied_patch_ids": template.applied_patch_ids,
            "realm_id": unit.realm_id,
            "stage_id": unit.stage_id,
            "max_hp": unit.max_hp,
            "current_hp": unit.current_hp,
            "current_shield": unit.current_shield,
            "max_resource": unit.max_resource,
            "current_resource": unit.current_resource,
            "attack_power": unit.attack_power,
            "guard_power": unit.guard_power,
            "speed": unit.speed,
            "crit_rate_permille": unit.crit_rate_permille,
            "crit_damage_bonus_permille": unit.crit_damage_bonus_permille,
            "hit_rate_permille": unit.hit_rate_permille,
            "dodge_rate_permille": unit.dodge_rate_permille,
            "control_bonus_permille": unit.control_bonus_permille,
            "control_resist_permille": unit.control_resist_permille,
            "healing_power_permille": unit.healing_power_permille,
            "shield_power_permille": unit.shield_power_permille,
            "damage_bonus_permille": unit.damage_bonus_permille,
            "damage_reduction_permille": unit.damage_reduction_permille,
            "counter_rate_permille": unit.counter_rate_permille,
        }

    def _build_round_payloads(self, *, events: Sequence[BattleEvent]) -> tuple[dict[str, object], ...]:
        events_by_round: dict[int, list[BattleEvent]] = defaultdict(list)
        for event in events:
            if event.round_index > 0:
                events_by_round[event.round_index].append(event)

        round_payloads: list[dict[str, object]] = []
        for round_index in sorted(events_by_round):
            round_events = tuple(sorted(events_by_round[round_index], key=lambda item: item.sequence))
            round_payloads.append(
                {
                    "round_index": round_index,
                    "action_queue": tuple(
                        _normalize_event(event)
                        for event in round_events
                        if event.event_type == "action_queue_entry"
                    ),
                    "selected_actions": tuple(
                        _normalize_event(event)
                        for event in round_events
                        if event.event_type == "action_selected"
                    ),
                    "hit_checks": tuple(
                        _normalize_event(event)
                        for event in round_events
                        if event.event_type == "hit_check"
                    ),
                    "crit_checks": tuple(
                        _normalize_event(event)
                        for event in round_events
                        if event.event_type == "crit_check"
                    ),
                    "status_checks": tuple(
                        _normalize_event(event)
                        for event in round_events
                        if event.event_type == "status_check"
                    ),
                    "status_changes": tuple(
                        _normalize_event(event)
                        for event in round_events
                        if event.event_type in _STATUS_CHANGE_EVENT_TYPES
                    ),
                    "reaction_chain": tuple(
                        _normalize_event(event)
                        for event in round_events
                        if event.event_type in _REACTION_EVENT_TYPES
                    ),
                    "round_finished": next(
                        (
                            _normalize_event(event)
                            for event in round_events
                            if event.event_type == "round_finished"
                        ),
                        None,
                    ),
                    "event_sequences": tuple(event.sequence for event in round_events),
                }
            )
        return tuple(round_payloads)

    def _build_terminal_statistics(
        self,
        *,
        result: BattleResult,
        behavior_templates: Mapping[str, CompiledBehaviorTemplate],
    ) -> dict[str, object]:
        final_units = tuple(
            self._build_final_unit_payload(unit=unit)
            for unit in result.final_units
        )
        side_totals = tuple(
            self._build_side_total_payload(result=result, side=side)
            for side in (BattleSide.ALLY, BattleSide.ENEMY)
        )
        unit_side_map = {unit.unit_id: unit.side.value for unit in result.final_units}
        unit_name_map = {unit.unit_id: unit.unit_name for unit in result.final_units}
        unit_template_map = {unit.unit_id: unit.behavior_template.path_id for unit in result.final_units}
        return {
            "outcome": result.outcome.value,
            "completed_rounds": result.completed_rounds,
            "total_events": result.statistics.total_events,
            "total_random_calls": result.statistics.total_random_calls,
            "side_totals": side_totals,
            "unit_statistics": tuple(
                {
                    "unit_id": item.unit_id,
                    "unit_name": unit_name_map[item.unit_id],
                    "side": unit_side_map[item.unit_id],
                    "path_id": unit_template_map[item.unit_id],
                    "damage_dealt": item.damage_dealt,
                    "damage_taken": item.damage_taken,
                    "healing_done": item.healing_done,
                    "healing_received": item.healing_received,
                    "shield_gained": item.shield_gained,
                    "shield_absorbed": item.shield_absorbed,
                    "actions_executed": item.actions_executed,
                    "pursuits_triggered": item.pursuits_triggered,
                    "counters_triggered": item.counters_triggered,
                    "statuses_applied": item.statuses_applied,
                    "special_effects_triggered": item.special_effects_triggered,
                    "kills": item.kills,
                    "deaths": item.deaths,
                }
                for item in result.statistics.unit_statistics
            ),
            "final_units": final_units,
            "behavior_templates": tuple(
                {
                    "template_id": template.template_id,
                    "path_id": template.path_id,
                    "axis_id": template.axis_id,
                    "name": template.name,
                    "template_tags": template.template_tags,
                    "applied_patch_ids": template.applied_patch_ids,
                    "actions": tuple(
                        {
                            "action_id": action.action_id,
                            "action_type": action.action_type.value,
                            "priority": action.priority,
                            "weight_permille": action.weight_permille,
                            "resource_cost": action.resource_cost,
                            "cooldown_rounds": action.cooldown_rounds,
                            "labels": action.labels,
                        }
                        for action in sorted(
                            template.actions,
                            key=lambda item: (item.execution_order, item.source_order, item.action_id),
                        )
                    ),
                }
                for template in sorted(
                    behavior_templates.values(),
                    key=lambda item: (item.path_id, item.template_id),
                )
            ),
        }

    @staticmethod
    def _build_final_unit_payload(*, unit: BattleUnitState) -> dict[str, object]:
        return {
            "unit_id": unit.unit_id,
            "unit_name": unit.unit_name,
            "side": unit.side.value,
            "template_id": unit.behavior_template.template_id,
            "path_id": unit.behavior_template.path_id,
            "axis_id": unit.behavior_template.axis_id,
            "template_tags": unit.behavior_template.template_tags,
            "current_hp": unit.current_hp,
            "max_hp": unit.base_snapshot.max_hp,
            "final_hp_ratio": _format_ratio(
                _resolve_ratio_decimal(unit.current_hp, unit.base_snapshot.max_hp)
            ),
            "current_resource": unit.current_resource,
            "max_resource": unit.base_snapshot.max_resource,
            "final_mp_ratio": _format_ratio(
                _resolve_ratio_decimal(unit.current_resource, unit.base_snapshot.max_resource)
            ),
            "current_shield": unit.current_shield,
            "is_alive": unit.is_alive,
            "statuses": tuple(_normalize_status(status) for status in unit.ordered_statuses()),
            "special_effects": tuple(_normalize_special_effect(effect) for effect in unit.ordered_special_effects()),
        }

    @staticmethod
    def _build_side_total_payload(*, result: BattleResult, side: BattleSide) -> dict[str, object]:
        side_unit_ids = {unit.unit_id for unit in result.final_units if unit.side is side}
        final_units = tuple(unit for unit in result.final_units if unit.side is side)
        related_statistics = tuple(
            item for item in result.statistics.unit_statistics if item.unit_id in side_unit_ids
        )
        return {
            "side": side.value,
            "total_units": len(final_units),
            "alive_units": sum(1 for unit in final_units if unit.is_alive),
            "defeated_units": sum(1 for unit in final_units if not unit.is_alive),
            "damage_dealt": sum(item.damage_dealt for item in related_statistics),
            "damage_taken": sum(item.damage_taken for item in related_statistics),
            "healing_done": sum(item.healing_done for item in related_statistics),
            "healing_received": sum(item.healing_received for item in related_statistics),
            "shield_gained": sum(item.shield_gained for item in related_statistics),
            "shield_absorbed": sum(item.shield_absorbed for item in related_statistics),
            "kills": sum(item.kills for item in related_statistics),
            "deaths": sum(item.deaths for item in related_statistics),
        }


def _build_action_usage(events: Sequence[BattleEvent]) -> dict[str, Counter[str]]:
    """按单位汇总实际执行过的动作次数。"""
    usage_by_actor: dict[str, Counter[str]] = defaultdict(Counter)
    for event in events:
        if event.event_type != "action_started":
            continue
        if event.actor_unit_id is None or event.action_id is None:
            continue
        usage_by_actor[event.actor_unit_id][event.action_id] += 1
    return dict(usage_by_actor)


def _normalize_status(status: BattleStatusEffect) -> dict[str, object]:
    """导出最终状态快照。"""
    return {
        "status_id": status.status_id,
        "status_name": status.status_name,
        "category": status.category.value,
        "holder_unit_id": status.holder_unit_id,
        "source_unit_id": status.source_unit_id,
        "source_action_id": status.source_action_id,
        "intensity_permille": status.intensity_permille,
        "duration_rounds": status.duration_rounds,
        "stack_count": status.stack_count,
        "max_stacks": status.max_stacks,
        "base_value": status.base_value,
        "applied_round": status.applied_round,
    }


def _normalize_special_effect(effect) -> dict[str, object]:
    """导出最终特殊效果运行态。"""
    return {
        "effect_id": effect.effect_id,
        "effect_name": effect.effect_name,
        "effect_type": effect.effect_type,
        "trigger_event": effect.trigger_event,
        "owner_unit_id": effect.owner_unit_id,
        "source_affix_id": effect.source_affix_id,
        "payload": dict(effect.payload),
        "cooldown_remaining": effect.cooldown_remaining,
        "stack_count": effect.stack_count,
        "max_stacks": effect.max_stacks,
        "triggers_used_this_round": effect.triggers_used_this_round,
        "triggers_used_this_battle": effect.triggers_used_this_battle,
        "internal_counters": dict(effect.internal_counters),
        "disabled": effect.disabled,
    }


def _normalize_random_call(call: BattleRandomCall) -> dict[str, object]:
    """把随机调用记录导出为稳定结构。"""
    return {
        "sequence": call.sequence,
        "purpose": call.purpose,
        "minimum": call.minimum,
        "maximum": call.maximum,
        "result": call.result,
    }


def _normalize_event(event: BattleEvent) -> dict[str, object]:
    """把结构化事件导出为稳定结构。"""
    return {
        "sequence": event.sequence,
        "round_index": event.round_index,
        "phase": event.phase.value,
        "event_type": event.event_type,
        "actor_unit_id": event.actor_unit_id,
        "target_unit_id": event.target_unit_id,
        "action_id": event.action_id,
        "detail": {key: value for key, value in event.detail_items},
    }


def _normalize_environment_snapshot(
    environment_snapshot: Mapping[str, str | int | bool | None] | None,
) -> dict[str, str | int | bool | None]:
    """按稳定顺序整理环境快照。"""
    if environment_snapshot is None:
        return {}
    return {
        key: environment_snapshot[key]
        for key in sorted(environment_snapshot)
    }


def _resolve_seed(*, result: BattleResult) -> int:
    """从随机调用序列回退解析种子占位值。"""
    if not result.random_calls:
        return 0
    return result.random_calls[0].sequence - 1 + 1 if result.random_calls else 0


def _resolve_perspective_result(*, outcome: BattleOutcome, focus_side: BattleSide) -> str:
    """根据焦点单位视角映射持久化结果文本。"""
    if outcome is BattleOutcome.DRAW:
        return "draw"
    if outcome is BattleOutcome.ALLY_VICTORY:
        return "victory" if focus_side is BattleSide.ALLY else "defeat"
    return "victory" if focus_side is BattleSide.ENEMY else "defeat"


def _require_unit(*, final_units: Sequence[BattleUnitState], focus_unit_id: str) -> BattleUnitState:
    """从终局单位列表中读取焦点单位。"""
    for unit in final_units:
        if unit.unit_id == focus_unit_id:
            return unit
    raise ValueError(f"未在终局单位中找到焦点单位：{focus_unit_id}")


def _resolve_ratio_decimal(current_value: int, maximum_value: int) -> Decimal:
    """返回四位小数精度的比例值。"""
    if maximum_value <= 0:
        return Decimal("1.0000")
    ratio = Decimal(current_value) / Decimal(maximum_value)
    return ratio.quantize(_RATIO_QUANTIZE, rounding=ROUND_HALF_UP)


def _format_ratio(ratio: Decimal) -> str:
    """格式化比例字符串。"""
    return format(ratio.quantize(_RATIO_QUANTIZE, rounding=ROUND_HALF_UP), ".4f")


def _event_detail_bool(event: BattleEvent, key: str) -> bool:
    """读取事件详情中的布尔字段。"""
    for detail_key, value in event.detail_items:
        if detail_key == key:
            return bool(value)
    return False


def _hash_payload(payload: dict[str, object]) -> str:
    """基于稳定 JSON 序列生成摘要哈希。"""
    serialized = json.dumps(
        _json_ready(payload),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _json_ready(value: object) -> object:
    """把嵌套对象递归转换为 JSON 友好结构。"""
    if isinstance(value, tuple):
        return [_json_ready(item) for item in value]
    if isinstance(value, list):
        return [_json_ready(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_ready(item) for key, item in value.items()}
    return value


__all__ = [
    "BattleLossEvaluator",
    "BattleLossResult",
    "BattleReportArtifacts",
    "BattleReportBuilder",
    "BattleReportDetail",
    "BattleReportSummary",
]
