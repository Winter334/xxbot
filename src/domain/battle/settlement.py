"""统一结算管线与战斗运行态上下文。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from random import Random

from domain.battle.models import (
    BattleActionDecision,
    BattleActionType,
    BattleEvent,
    BattleEventPhase,
    BattleRandomCall,
    BattleRandomSource,
    BattleSide,
    BattleSnapshot,
    BattleStatistics,
    BattleStatusCategory,
    BattleStatusEffect,
    BattleUnitState,
    BattleUnitStatistics,
    CompiledBehaviorAction,
    CompiledBehaviorTemplate,
)
from domain.battle.special_effects import BattleSpecialEffectHook, BattleSpecialEffectRegistry

_SIDE_ID_MAP: dict[BattleSide, int] = {
    BattleSide.ALLY: 0,
    BattleSide.ENEMY: 1,
}
_STATUS_APPLICATION_ORDER: tuple[BattleStatusCategory, ...] = (
    BattleStatusCategory.HARD_CONTROL,
    BattleStatusCategory.DAMAGE_OVER_TIME,
    BattleStatusCategory.ATTRIBUTE_SUPPRESSION,
)
_DOT_STACK_LIMIT = 5
_HARD_CONTROL_DURATION = 1
_DAMAGE_OVER_TIME_DURATION = 2
_ATTRIBUTE_SUPPRESSION_DURATION = 2
_MINIMUM_HIT_CHANCE_PERMILLE = 50
_MAXIMUM_GUARD_REDUCTION_PERMILLE = 850
_BASE_CRIT_DAMAGE_PERMILLE = 1500


@dataclass(slots=True)
class _MutableUnitStatistics:
    """运行期统计累加器。"""

    damage_dealt: int = 0
    damage_taken: int = 0
    healing_done: int = 0
    healing_received: int = 0
    shield_gained: int = 0
    shield_absorbed: int = 0
    actions_executed: int = 0
    pursuits_triggered: int = 0
    counters_triggered: int = 0
    statuses_applied: int = 0
    special_effects_triggered: int = 0
    kills: int = 0
    deaths: int = 0

    def freeze(self, *, unit_id: str) -> BattleUnitStatistics:
        """转换为只读统计对象。"""
        return BattleUnitStatistics(
            unit_id=unit_id,
            damage_dealt=self.damage_dealt,
            damage_taken=self.damage_taken,
            healing_done=self.healing_done,
            healing_received=self.healing_received,
            shield_gained=self.shield_gained,
            shield_absorbed=self.shield_absorbed,
            actions_executed=self.actions_executed,
            pursuits_triggered=self.pursuits_triggered,
            counters_triggered=self.counters_triggered,
            statuses_applied=self.statuses_applied,
            special_effects_triggered=self.special_effects_triggered,
            kills=self.kills,
            deaths=self.deaths,
        )


@dataclass(frozen=True, slots=True)
class CounterCandidate:
    """延后到追击链收敛后执行的反击候选。"""

    counter_actor_unit_id: str
    target_unit_id: str
    origin_action_id: str

    def __post_init__(self) -> None:
        _require_non_blank(self.counter_actor_unit_id, field_name="counter_actor_unit_id")
        _require_non_blank(self.target_unit_id, field_name="target_unit_id")
        _require_non_blank(self.origin_action_id, field_name="origin_action_id")


@dataclass(frozen=True, slots=True)
class PendingDeathCleanup:
    """延后到即时反应链完成后执行的死亡清理项。"""

    killer_unit_id: str
    target_unit_id: str
    action_id: str

    def __post_init__(self) -> None:
        _require_non_blank(self.killer_unit_id, field_name="killer_unit_id")
        _require_non_blank(self.target_unit_id, field_name="target_unit_id")
        _require_non_blank(self.action_id, field_name="action_id")


@dataclass(frozen=True, slots=True)
class ActionSettlementOutcome:
    """单次动作统一结算结果。"""

    can_pursue: bool = False
    counter_candidates: tuple[CounterCandidate, ...] = ()
    pending_deaths: tuple[PendingDeathCleanup, ...] = ()


class SeededBattleRandomSource(BattleRandomSource):
    """记录调用序列的种子随机源。"""

    def __init__(self, *, seed: int) -> None:
        self._random = Random(seed)
        self._sequence = 0
        self._calls: list[BattleRandomCall] = []

    def next_int(self, *, minimum: int, maximum: int, purpose: str) -> int:
        """返回闭区间随机整数，并写入调用记录。"""
        if minimum > maximum:
            raise ValueError("minimum 不能大于 maximum")
        self._sequence += 1
        result = self._random.randint(minimum, maximum)
        self._calls.append(
            BattleRandomCall(
                sequence=self._sequence,
                purpose=purpose,
                minimum=minimum,
                maximum=maximum,
                result=result,
            )
        )
        return result

    def export_calls(self) -> tuple[BattleRandomCall, ...]:
        """导出全部随机调用记录。"""
        return tuple(self._calls)


@dataclass(slots=True)
class BattleRuntimeContext:
    """战斗运行态上下文。"""

    snapshot: BattleSnapshot
    random_source: BattleRandomSource
    special_effect_registry: BattleSpecialEffectRegistry
    units_by_id: dict[str, BattleUnitState]
    ordered_unit_ids: tuple[str, ...]
    round_index: int = 0
    event_sequence: int = 0
    events: list[BattleEvent] = field(default_factory=list)
    statistics_by_unit: dict[str, _MutableUnitStatistics] = field(default_factory=dict)

    @classmethod
    def from_snapshot(
        cls,
        *,
        snapshot: BattleSnapshot,
        behavior_templates: Mapping[str, CompiledBehaviorTemplate],
        random_source: BattleRandomSource,
        special_effect_registry: BattleSpecialEffectRegistry | None = None,
    ) -> BattleRuntimeContext:
        """根据输入快照构造运行态上下文。"""
        resolved_special_effect_registry = special_effect_registry or BattleSpecialEffectRegistry()
        units_by_id: dict[str, BattleUnitState] = {}
        ordered_unit_ids: list[str] = []
        statistics_by_unit: dict[str, _MutableUnitStatistics] = {}

        for stable_order, unit_snapshot in enumerate(snapshot.all_units, start=1):
            template = _resolve_behavior_template(
                behavior_template_id=unit_snapshot.behavior_template_id,
                behavior_templates=behavior_templates,
            )
            stable_first_strike_key = 0 if "first_strike" in template.template_tags else 1
            unit_state = BattleUnitState(
                base_snapshot=unit_snapshot,
                behavior_template=template,
                stable_order=stable_order,
                side_id=_SIDE_ID_MAP[unit_snapshot.side],
                stable_first_strike_key=stable_first_strike_key,
                current_hp=unit_snapshot.current_hp,
                current_shield=unit_snapshot.current_shield,
                current_resource=unit_snapshot.current_resource,
                special_effect_states=list(resolved_special_effect_registry.build_states(unit_snapshot=unit_snapshot)),
            )
            units_by_id[unit_state.unit_id] = unit_state
            ordered_unit_ids.append(unit_state.unit_id)
            statistics_by_unit[unit_state.unit_id] = _MutableUnitStatistics()

        return cls(
            snapshot=snapshot,
            random_source=random_source,
            special_effect_registry=resolved_special_effect_registry,
            units_by_id=units_by_id,
            ordered_unit_ids=tuple(ordered_unit_ids),
            statistics_by_unit=statistics_by_unit,
        )

    def get_unit(self, unit_id: str) -> BattleUnitState:
        """读取指定单位运行态。"""
        return self.units_by_id[unit_id]

    def ordered_units(self) -> tuple[BattleUnitState, ...]:
        """按稳定顺序返回全部单位。"""
        return tuple(self.units_by_id[unit_id] for unit_id in self.ordered_unit_ids)

    def alive_units(self, *, side: BattleSide | None = None) -> tuple[BattleUnitState, ...]:
        """按稳定顺序返回存活单位。"""
        return tuple(
            unit
            for unit in self.ordered_units()
            if unit.is_alive and (side is None or unit.side is side)
        )

    def emit_event(
        self,
        *,
        phase: BattleEventPhase,
        event_type: str,
        actor_unit_id: str | None = None,
        target_unit_id: str | None = None,
        action_id: str | None = None,
        detail_items: tuple[tuple[str, str | int | bool | None], ...] = (),
    ) -> BattleEvent:
        """写入结构化事件。"""
        self.event_sequence += 1
        event = BattleEvent(
            sequence=self.event_sequence,
            round_index=self.round_index,
            phase=phase,
            event_type=event_type,
            actor_unit_id=actor_unit_id,
            target_unit_id=target_unit_id,
            action_id=action_id,
            detail_items=detail_items,
        )
        self.events.append(event)
        return event

    def add_stat(self, *, unit_id: str, field_name: str, delta: int) -> None:
        """累加单个单位统计值。"""
        if delta == 0:
            return
        statistics = self.statistics_by_unit[unit_id]
        current_value = getattr(statistics, field_name)
        setattr(statistics, field_name, current_value + delta)

    def build_statistics(self) -> BattleStatistics:
        """导出当前统计快照。"""
        return BattleStatistics(
            unit_statistics=tuple(
                self.statistics_by_unit[unit_id].freeze(unit_id=unit_id)
                for unit_id in self.ordered_unit_ids
            ),
            total_rounds=self.round_index,
            total_events=len(self.events),
            total_random_calls=len(self.random_source.export_calls()),
        )


class BattleSettlementEngine:
    """处理统一结算、状态与死亡清理。"""

    def process_turn_start(self, *, context: BattleRuntimeContext, actor_unit_id: str) -> bool:
        """处理角色回合开始结算，并返回是否允许行动。"""
        actor = context.get_unit(actor_unit_id)
        if not actor.is_alive:
            return False

        context.special_effect_registry.dispatch(
            hook=BattleSpecialEffectHook.TURN_START,
            runtime_context=context,
            owner_unit_id=actor.unit_id,
            actor_unit_id=actor.unit_id,
        )

        for status in actor.ordered_statuses():
            if status.category is not BattleStatusCategory.DAMAGE_OVER_TIME:
                continue
            self._apply_damage_over_time(context=context, holder=actor, status=status)
            if not actor.is_alive:
                return False

        hard_control = self._get_active_hard_control(actor)
        if hard_control is not None:
            context.emit_event(
                phase=BattleEventPhase.TURN_START,
                event_type="turn_skipped_by_control",
                actor_unit_id=actor.unit_id,
                target_unit_id=actor.unit_id,
                action_id=hard_control.source_action_id,
                detail_items=(
                    ("status_id", hard_control.status_id),
                    ("source_unit_id", hard_control.source_unit_id),
                    ("remaining_rounds", hard_control.duration_rounds),
                ),
            )
            return False
        return True

    def settle_action(
        self,
        *,
        context: BattleRuntimeContext,
        decision: BattleActionDecision,
    ) -> ActionSettlementOutcome:
        """按固定顺序完成单次动作统一结算。"""
        actor = context.get_unit(decision.actor_unit_id)
        if not actor.is_alive:
            context.emit_event(
                phase=BattleEventPhase.SETTLEMENT,
                event_type="action_cancelled_actor_dead",
                actor_unit_id=actor.unit_id,
                action_id=decision.action.action_id,
            )
            return ActionSettlementOutcome()

        if decision.consume_cost and decision.action.resource_cost > actor.current_resource:
            context.emit_event(
                phase=BattleEventPhase.SETTLEMENT,
                event_type="action_cancelled_insufficient_resource",
                actor_unit_id=actor.unit_id,
                action_id=decision.action.action_id,
                detail_items=(("required_resource", decision.action.resource_cost),),
            )
            return ActionSettlementOutcome()

        if decision.consume_cost and decision.action.resource_cost > 0:
            actor.current_resource -= decision.action.resource_cost
            context.emit_event(
                phase=BattleEventPhase.SETTLEMENT,
                event_type="resource_spent",
                actor_unit_id=actor.unit_id,
                action_id=decision.action.action_id,
                detail_items=(
                    ("spent_resource", decision.action.resource_cost),
                    ("remaining_resource", actor.current_resource),
                ),
            )

        if decision.consume_cost and decision.action.cooldown_rounds > 0:
            actor.cooldowns[decision.action.action_id] = decision.action.cooldown_rounds + 1

        context.special_effect_registry.dispatch(
            hook=BattleSpecialEffectHook.BEFORE_ACTION,
            runtime_context=context,
            owner_unit_id=actor.unit_id,
            actor_unit_id=actor.unit_id,
            action_id=decision.action.action_id,
            decision=decision,
        )

        context.add_stat(unit_id=actor.unit_id, field_name="actions_executed", delta=1)
        context.emit_event(
            phase=BattleEventPhase.SETTLEMENT,
            event_type="action_started",
            actor_unit_id=actor.unit_id,
            action_id=decision.action.action_id,
            detail_items=(
                ("target_count", len(decision.target_unit_ids)),
                (
                    "reaction_type",
                    None if decision.reaction_type is None else decision.reaction_type.value,
                ),
                ("reaction_depth", decision.reaction_depth),
                ("is_fallback", decision.is_fallback),
            ),
        )

        can_pursue = False
        counter_candidates: list[CounterCandidate] = []
        pending_deaths: list[PendingDeathCleanup] = []
        is_direct_single_target = decision.action.damage_scale_permille > 0 and len(decision.target_unit_ids) == 1

        for target_unit_id in decision.target_unit_ids:
            target = context.get_unit(target_unit_id)
            if decision.action.damage_scale_permille > 0:
                target_outcome = self._settle_damage_target(
                    context=context,
                    actor=actor,
                    target=target,
                    action=decision.action,
                    origin_action_id=decision.origin_action_id,
                    is_direct_single_target=is_direct_single_target,
                    can_trigger_counter=decision.can_trigger_counter,
                )
                can_pursue = can_pursue or target_outcome.can_pursue
                if target_outcome.counter_candidate is not None:
                    counter_candidates.append(target_outcome.counter_candidate)
                if target_outcome.pending_death is not None:
                    pending_deaths.append(target_outcome.pending_death)
            elif decision.action.heal_scale_permille > 0:
                self._settle_heal_target(
                    context=context,
                    actor=actor,
                    target=target,
                    action=decision.action,
                )
            elif decision.action.shield_scale_permille > 0:
                self._settle_shield_target(
                    context=context,
                    actor=actor,
                    target=target,
                    action=decision.action,
                )

        context.special_effect_registry.dispatch(
            hook=BattleSpecialEffectHook.AFTER_ACTION,
            runtime_context=context,
            owner_unit_id=actor.unit_id,
            actor_unit_id=actor.unit_id,
            action_id=decision.action.action_id,
            decision=decision,
        )
        return ActionSettlementOutcome(
            can_pursue=can_pursue,
            counter_candidates=tuple(counter_candidates),
            pending_deaths=tuple(pending_deaths),
        )

    def process_turn_end(self, *, context: BattleRuntimeContext, actor_unit_id: str) -> None:
        """处理角色回合结束时的持续时间与冷却递减。"""
        actor = context.get_unit(actor_unit_id)
        if not actor.is_alive:
            return

        updated_statuses: list[BattleStatusEffect] = []
        for status in actor.ordered_statuses():
            next_duration = status.duration_rounds - 1
            if next_duration <= 0:
                context.emit_event(
                    phase=BattleEventPhase.TURN_END,
                    event_type="status_expired",
                    actor_unit_id=actor.unit_id,
                    target_unit_id=actor.unit_id,
                    action_id=status.source_action_id,
                    detail_items=(("status_id", status.status_id),),
                )
                continue
            updated_statuses.append(
                BattleStatusEffect(
                    status_id=status.status_id,
                    status_name=status.status_name,
                    category=status.category,
                    holder_unit_id=status.holder_unit_id,
                    source_unit_id=status.source_unit_id,
                    source_action_id=status.source_action_id,
                    intensity_permille=status.intensity_permille,
                    duration_rounds=next_duration,
                    stack_count=status.stack_count,
                    max_stacks=status.max_stacks,
                    base_value=status.base_value,
                    applied_round=status.applied_round,
                )
            )
        actor.statuses = list(_ordered_statuses(updated_statuses))

        for action_id in tuple(sorted(actor.cooldowns)):
            next_cooldown = actor.cooldowns[action_id] - 1
            if next_cooldown <= 0:
                del actor.cooldowns[action_id]
                continue
            actor.cooldowns[action_id] = next_cooldown

        context.special_effect_registry.dispatch(
            hook=BattleSpecialEffectHook.TURN_END,
            runtime_context=context,
            owner_unit_id=actor.unit_id,
            actor_unit_id=actor.unit_id,
        )

    def cleanup_pending_deaths(
        self,
        *,
        context: BattleRuntimeContext,
        pending_deaths: tuple[PendingDeathCleanup, ...] | list[PendingDeathCleanup],
    ) -> None:
        """在即时反应链完成后统一执行死亡清理。"""
        processed_unit_ids: set[str] = set()
        ordered_cleanups = tuple(
            sorted(
                pending_deaths,
                key=lambda item: (
                    context.get_unit(item.target_unit_id).side_id,
                    context.get_unit(item.target_unit_id).stable_order,
                    item.target_unit_id,
                    item.killer_unit_id,
                    item.action_id,
                ),
            )
        )
        for cleanup in ordered_cleanups:
            if cleanup.target_unit_id in processed_unit_ids:
                continue
            target = context.get_unit(cleanup.target_unit_id)
            if target.current_hp > 0:
                continue
            processed_unit_ids.add(cleanup.target_unit_id)
            self._cleanup_death(
                context=context,
                killer_unit_id=cleanup.killer_unit_id,
                target=target,
                action_id=cleanup.action_id,
            )

    def _settle_damage_target(
        self,
        *,
        context: BattleRuntimeContext,
        actor: BattleUnitState,
        target: BattleUnitState,
        action: CompiledBehaviorAction,
        origin_action_id: str | None,
        is_direct_single_target: bool,
        can_trigger_counter: bool,
    ) -> _DamageResolution:
        if not target.is_alive:
            context.emit_event(
                phase=BattleEventPhase.SETTLEMENT,
                event_type="target_already_dead",
                actor_unit_id=actor.unit_id,
                target_unit_id=target.unit_id,
                action_id=action.action_id,
            )
            return _DamageResolution()

        hit_chance_permille = self._resolve_hit_chance_permille(actor=actor, target=target)
        hit_roll, hit_success = self._roll_success(
            context=context,
            chance_permille=hit_chance_permille,
            purpose=f"hit:{context.round_index}:{action.action_id}:{actor.unit_id}:{target.unit_id}",
        )
        context.emit_event(
            phase=BattleEventPhase.SETTLEMENT,
            event_type="hit_check",
            actor_unit_id=actor.unit_id,
            target_unit_id=target.unit_id,
            action_id=action.action_id,
            detail_items=(
                ("chance_permille", hit_chance_permille),
                ("roll", hit_roll),
                ("success", hit_success),
            ),
        )
        if not hit_success:
            return _DamageResolution()

        crit_chance_permille = _clamp(actor.base_snapshot.crit_rate_permille, 0, 1000)
        crit_roll, crit_success = self._roll_success(
            context=context,
            chance_permille=crit_chance_permille,
            purpose=f"crit:{context.round_index}:{action.action_id}:{actor.unit_id}:{target.unit_id}",
        )
        context.emit_event(
            phase=BattleEventPhase.SETTLEMENT,
            event_type="crit_check",
            actor_unit_id=actor.unit_id,
            target_unit_id=target.unit_id,
            action_id=action.action_id,
            detail_items=(
                ("chance_permille", crit_chance_permille),
                ("roll", crit_roll),
                ("success", crit_success),
            ),
        )

        base_damage = actor.effective_attack_power * action.damage_scale_permille // 1000
        context.emit_event(
            phase=BattleEventPhase.SETTLEMENT,
            event_type="damage_base",
            actor_unit_id=actor.unit_id,
            target_unit_id=target.unit_id,
            action_id=action.action_id,
            detail_items=(("base_damage", base_damage),),
        )

        critical_damage = base_damage
        if crit_success:
            crit_multiplier_permille = _BASE_CRIT_DAMAGE_PERMILLE + actor.base_snapshot.crit_damage_bonus_permille
            critical_damage = critical_damage * crit_multiplier_permille // 1000

        bonus_damage = critical_damage * (1000 + actor.base_snapshot.damage_bonus_permille) // 1000
        penetration_permille = self._resolve_penetration_permille(action=action)
        guard_reduction_permille = self._resolve_guard_reduction_permille(actor=actor, target=target)
        effective_guard_reduction = max(0, guard_reduction_permille - penetration_permille)
        reduced_damage = bonus_damage * (1000 - effective_guard_reduction) // 1000
        reduced_damage = reduced_damage * (1000 - target.base_snapshot.damage_reduction_permille) // 1000
        final_damage = max(1, reduced_damage)

        context.emit_event(
            phase=BattleEventPhase.SETTLEMENT,
            event_type="damage_resolved",
            actor_unit_id=actor.unit_id,
            target_unit_id=target.unit_id,
            action_id=action.action_id,
            detail_items=(
                ("critical_damage", critical_damage),
                ("penetration_permille", penetration_permille),
                ("guard_reduction_permille", guard_reduction_permille),
                ("effective_guard_reduction", effective_guard_reduction),
                ("final_damage", final_damage),
            ),
        )

        before_alive = target.is_alive
        shield_absorbed = min(target.current_shield, final_damage)
        if shield_absorbed > 0:
            target.current_shield -= shield_absorbed
            context.add_stat(unit_id=target.unit_id, field_name="shield_absorbed", delta=shield_absorbed)
            context.emit_event(
                phase=BattleEventPhase.SETTLEMENT,
                event_type="shield_absorbed",
                actor_unit_id=actor.unit_id,
                target_unit_id=target.unit_id,
                action_id=action.action_id,
                detail_items=(("absorbed_damage", shield_absorbed),),
            )

        hp_damage = max(0, final_damage - shield_absorbed)
        if hp_damage > 0:
            target.current_hp = max(0, target.current_hp - hp_damage)
            context.emit_event(
                phase=BattleEventPhase.SETTLEMENT,
                event_type="hp_changed",
                actor_unit_id=actor.unit_id,
                target_unit_id=target.unit_id,
                action_id=action.action_id,
                detail_items=(("hp_damage", hp_damage), ("remaining_hp", target.current_hp)),
            )

        context.add_stat(unit_id=actor.unit_id, field_name="damage_dealt", delta=final_damage)
        context.add_stat(unit_id=target.unit_id, field_name="damage_taken", delta=final_damage)
        context.special_effect_registry.dispatch(
            hook=BattleSpecialEffectHook.DAMAGE_RESOLVED,
            runtime_context=context,
            owner_unit_id=actor.unit_id,
            actor_unit_id=actor.unit_id,
            target_unit_id=target.unit_id,
            action_id=action.action_id,
            resolved_value=final_damage,
        )
        context.special_effect_registry.dispatch(
            hook=BattleSpecialEffectHook.DAMAGE_TAKEN,
            runtime_context=context,
            owner_unit_id=target.unit_id,
            actor_unit_id=actor.unit_id,
            target_unit_id=target.unit_id,
            action_id=action.action_id,
            resolved_value=final_damage,
        )

        if target.is_alive and action.control_chance_permille > 0:
            self._apply_status_effects(
                context=context,
                actor=actor,
                target=target,
                action=action,
                resolved_damage=final_damage,
            )

        counter_candidate: CounterCandidate | None = None
        if can_trigger_counter and is_direct_single_target and target.is_alive:
            counter_candidate = CounterCandidate(
                counter_actor_unit_id=target.unit_id,
                target_unit_id=actor.unit_id,
                origin_action_id=origin_action_id or action.action_id,
            )

        pending_death: PendingDeathCleanup | None = None
        if before_alive and not target.is_alive:
            pending_death = PendingDeathCleanup(
                killer_unit_id=actor.unit_id,
                target_unit_id=target.unit_id,
                action_id=action.action_id,
            )

        return _DamageResolution(
            can_pursue=is_direct_single_target and actor.is_alive and self._action_can_pursue(action),
            counter_candidate=counter_candidate,
            pending_death=pending_death,
        )

    def _settle_heal_target(
        self,
        *,
        context: BattleRuntimeContext,
        actor: BattleUnitState,
        target: BattleUnitState,
        action: CompiledBehaviorAction,
    ) -> None:
        if not target.is_alive:
            return
        base_heal = actor.effective_attack_power * action.heal_scale_permille // 1000
        final_heal = base_heal * (1000 + actor.base_snapshot.healing_power_permille) // 1000
        actual_heal = min(target.base_snapshot.max_hp - target.current_hp, final_heal)
        if actual_heal <= 0:
            return
        target.current_hp += actual_heal
        context.add_stat(unit_id=actor.unit_id, field_name="healing_done", delta=actual_heal)
        context.add_stat(unit_id=target.unit_id, field_name="healing_received", delta=actual_heal)
        context.emit_event(
            phase=BattleEventPhase.SETTLEMENT,
            event_type="healing_applied",
            actor_unit_id=actor.unit_id,
            target_unit_id=target.unit_id,
            action_id=action.action_id,
            detail_items=(("healed_hp", actual_heal), ("remaining_hp", target.current_hp)),
        )

    def _settle_shield_target(
        self,
        *,
        context: BattleRuntimeContext,
        actor: BattleUnitState,
        target: BattleUnitState,
        action: CompiledBehaviorAction,
    ) -> None:
        if not target.is_alive:
            return
        base_shield = actor.effective_guard_power * action.shield_scale_permille // 1000
        final_shield = base_shield * (1000 + actor.base_snapshot.shield_power_permille) // 1000
        if final_shield <= 0:
            return
        target.current_shield += final_shield
        context.add_stat(unit_id=target.unit_id, field_name="shield_gained", delta=final_shield)
        context.emit_event(
            phase=BattleEventPhase.SETTLEMENT,
            event_type="shield_applied",
            actor_unit_id=actor.unit_id,
            target_unit_id=target.unit_id,
            action_id=action.action_id,
            detail_items=(("gained_shield", final_shield), ("current_shield", target.current_shield)),
        )

    def _apply_damage_over_time(
        self,
        *,
        context: BattleRuntimeContext,
        holder: BattleUnitState,
        status: BattleStatusEffect,
    ) -> None:
        if not holder.is_alive:
            return
        total_damage = max(1, status.base_value * status.stack_count * status.intensity_permille // 1000)
        shield_absorbed = min(holder.current_shield, total_damage)
        if shield_absorbed > 0:
            holder.current_shield -= shield_absorbed
            context.add_stat(unit_id=holder.unit_id, field_name="shield_absorbed", delta=shield_absorbed)
        hp_damage = max(0, total_damage - shield_absorbed)
        if hp_damage > 0:
            holder.current_hp = max(0, holder.current_hp - hp_damage)
        context.add_stat(unit_id=status.source_unit_id, field_name="damage_dealt", delta=total_damage)
        context.add_stat(unit_id=holder.unit_id, field_name="damage_taken", delta=total_damage)
        context.emit_event(
            phase=BattleEventPhase.TURN_START,
            event_type="damage_over_time_tick",
            actor_unit_id=status.source_unit_id,
            target_unit_id=holder.unit_id,
            action_id=status.source_action_id,
            detail_items=(
                ("status_id", status.status_id),
                ("stack_count", status.stack_count),
                ("total_damage", total_damage),
                ("shield_absorbed", shield_absorbed),
                ("hp_damage", hp_damage),
            ),
        )
        if holder.current_hp == 0:
            self._cleanup_death(
                context=context,
                killer_unit_id=status.source_unit_id,
                target=holder,
                action_id=status.source_action_id,
            )

    def _apply_status_effects(
        self,
        *,
        context: BattleRuntimeContext,
        actor: BattleUnitState,
        target: BattleUnitState,
        action: CompiledBehaviorAction,
        resolved_damage: int,
    ) -> None:
        categories = self._resolve_status_categories(action=action)
        for category in _STATUS_APPLICATION_ORDER:
            if category not in categories:
                continue
            chance_permille = _clamp(
                action.control_chance_permille
                + actor.base_snapshot.control_bonus_permille
                - target.base_snapshot.control_resist_permille,
                0,
                1000,
            )
            roll, success = self._roll_success(
                context=context,
                chance_permille=chance_permille,
                purpose=(
                    f"status:{category.value}:{context.round_index}:{action.action_id}:{actor.unit_id}:{target.unit_id}"
                ),
            )
            context.emit_event(
                phase=BattleEventPhase.SETTLEMENT,
                event_type="status_check",
                actor_unit_id=actor.unit_id,
                target_unit_id=target.unit_id,
                action_id=action.action_id,
                detail_items=(
                    ("status_category", category.value),
                    ("chance_permille", chance_permille),
                    ("roll", roll),
                    ("success", success),
                ),
            )
            if not success:
                continue
            status = self._build_status_effect(
                context=context,
                actor=actor,
                target=target,
                action=action,
                category=category,
                resolved_damage=resolved_damage,
                chance_permille=chance_permille,
            )
            self._merge_status_effect(context=context, target=target, status=status)
            context.add_stat(unit_id=actor.unit_id, field_name="statuses_applied", delta=1)

    @staticmethod
    def _resolve_status_categories(action: CompiledBehaviorAction) -> set[BattleStatusCategory]:
        categories: set[BattleStatusCategory] = set()
        label_set = set(action.labels)
        if action.action_type is BattleActionType.CONTROL_SPELL or "control" in label_set:
            categories.add(BattleStatusCategory.HARD_CONTROL)
        if action.action_type is BattleActionType.DEBUFF_SPELL or {"debuff", "anti_guard"}.intersection(label_set):
            categories.add(BattleStatusCategory.ATTRIBUTE_SUPPRESSION)
        if action.action_type is BattleActionType.DEBUFF_SPELL or "attrition" in label_set:
            categories.add(BattleStatusCategory.DAMAGE_OVER_TIME)
        if not categories and action.control_chance_permille > 0:
            categories.add(BattleStatusCategory.HARD_CONTROL)
        return categories

    def _build_status_effect(
        self,
        *,
        context: BattleRuntimeContext,
        actor: BattleUnitState,
        target: BattleUnitState,
        action: CompiledBehaviorAction,
        category: BattleStatusCategory,
        resolved_damage: int,
        chance_permille: int,
    ) -> BattleStatusEffect:
        if category is BattleStatusCategory.HARD_CONTROL:
            return BattleStatusEffect(
                status_id="hard_control",
                status_name="硬控制",
                category=category,
                holder_unit_id=target.unit_id,
                source_unit_id=actor.unit_id,
                source_action_id=action.action_id,
                intensity_permille=chance_permille,
                duration_rounds=_HARD_CONTROL_DURATION,
                applied_round=context.round_index,
            )
        if category is BattleStatusCategory.DAMAGE_OVER_TIME:
            return BattleStatusEffect(
                status_id="damage_over_time",
                status_name="持续伤害",
                category=category,
                holder_unit_id=target.unit_id,
                source_unit_id=actor.unit_id,
                source_action_id=action.action_id,
                intensity_permille=max(100, chance_permille),
                duration_rounds=_DAMAGE_OVER_TIME_DURATION,
                stack_count=1,
                max_stacks=_DOT_STACK_LIMIT,
                base_value=max(1, resolved_damage // 2),
                applied_round=context.round_index,
            )
        return BattleStatusEffect(
            status_id="attribute_suppression",
            status_name="属性压制",
            category=category,
            holder_unit_id=target.unit_id,
            source_unit_id=actor.unit_id,
            source_action_id=action.action_id,
            intensity_permille=max(100, min(800, chance_permille)),
            duration_rounds=_ATTRIBUTE_SUPPRESSION_DURATION,
            applied_round=context.round_index,
        )

    def _merge_status_effect(
        self,
        *,
        context: BattleRuntimeContext,
        target: BattleUnitState,
        status: BattleStatusEffect,
    ) -> None:
        if status.category is BattleStatusCategory.HARD_CONTROL:
            self._merge_hard_control(context=context, target=target, status=status)
            return
        if status.category is BattleStatusCategory.DAMAGE_OVER_TIME:
            self._merge_damage_over_time(context=context, target=target, status=status)
            return
        self._merge_attribute_suppression(context=context, target=target, status=status)

    def _merge_hard_control(
        self,
        *,
        context: BattleRuntimeContext,
        target: BattleUnitState,
        status: BattleStatusEffect,
    ) -> None:
        existing = self._get_active_hard_control(target)
        if existing is None:
            target.statuses = list(_ordered_statuses(target.statuses + [status]))
            context.emit_event(
                phase=BattleEventPhase.SETTLEMENT,
                event_type="status_applied",
                actor_unit_id=status.source_unit_id,
                target_unit_id=target.unit_id,
                action_id=status.source_action_id,
                detail_items=(("status_id", status.status_id), ("duration_rounds", status.duration_rounds)),
            )
            return
        should_replace = (
            status.intensity_permille > existing.intensity_permille
            or (
                status.intensity_permille == existing.intensity_permille
                and status.duration_rounds > existing.duration_rounds
            )
        )
        if not should_replace:
            context.emit_event(
                phase=BattleEventPhase.SETTLEMENT,
                event_type="status_kept_existing",
                actor_unit_id=status.source_unit_id,
                target_unit_id=target.unit_id,
                action_id=status.source_action_id,
                detail_items=(("status_id", existing.status_id),),
            )
            return
        remaining_statuses = [
            item
            for item in target.statuses
            if item.category is not BattleStatusCategory.HARD_CONTROL
        ]
        target.statuses = list(_ordered_statuses(remaining_statuses + [status]))
        context.emit_event(
            phase=BattleEventPhase.SETTLEMENT,
            event_type="status_replaced",
            actor_unit_id=status.source_unit_id,
            target_unit_id=target.unit_id,
            action_id=status.source_action_id,
            detail_items=(("status_id", status.status_id), ("duration_rounds", status.duration_rounds)),
        )

    def _merge_damage_over_time(
        self,
        *,
        context: BattleRuntimeContext,
        target: BattleUnitState,
        status: BattleStatusEffect,
    ) -> None:
        existing = next(
            (
                item
                for item in target.ordered_statuses()
                if item.category is BattleStatusCategory.DAMAGE_OVER_TIME
            ),
            None,
        )
        if existing is None:
            target.statuses = list(_ordered_statuses(target.statuses + [status]))
            context.emit_event(
                phase=BattleEventPhase.SETTLEMENT,
                event_type="status_applied",
                actor_unit_id=status.source_unit_id,
                target_unit_id=target.unit_id,
                action_id=status.source_action_id,
                detail_items=(
                    ("status_id", status.status_id),
                    ("stack_count", status.stack_count),
                    ("duration_rounds", status.duration_rounds),
                ),
            )
            return
        stacked_status = BattleStatusEffect(
            status_id=existing.status_id,
            status_name=existing.status_name,
            category=existing.category,
            holder_unit_id=existing.holder_unit_id,
            source_unit_id=status.source_unit_id,
            source_action_id=status.source_action_id,
            intensity_permille=max(existing.intensity_permille, status.intensity_permille),
            duration_rounds=status.duration_rounds,
            stack_count=min(existing.max_stacks, existing.stack_count + status.stack_count),
            max_stacks=existing.max_stacks,
            base_value=max(existing.base_value, status.base_value),
            applied_round=status.applied_round,
        )
        remaining_statuses = [
            item
            for item in target.statuses
            if item.category is not BattleStatusCategory.DAMAGE_OVER_TIME
        ]
        target.statuses = list(_ordered_statuses(remaining_statuses + [stacked_status]))
        context.emit_event(
            phase=BattleEventPhase.SETTLEMENT,
            event_type="status_stacked",
            actor_unit_id=status.source_unit_id,
            target_unit_id=target.unit_id,
            action_id=status.source_action_id,
            detail_items=(
                ("status_id", stacked_status.status_id),
                ("stack_count", stacked_status.stack_count),
                ("duration_rounds", stacked_status.duration_rounds),
            ),
        )

    def _merge_attribute_suppression(
        self,
        *,
        context: BattleRuntimeContext,
        target: BattleUnitState,
        status: BattleStatusEffect,
    ) -> None:
        existing = next(
            (
                item
                for item in target.ordered_statuses()
                if item.category is BattleStatusCategory.ATTRIBUTE_SUPPRESSION
            ),
            None,
        )
        if existing is None:
            target.statuses = list(_ordered_statuses(target.statuses + [status]))
            context.emit_event(
                phase=BattleEventPhase.SETTLEMENT,
                event_type="status_applied",
                actor_unit_id=status.source_unit_id,
                target_unit_id=target.unit_id,
                action_id=status.source_action_id,
                detail_items=(("status_id", status.status_id), ("duration_rounds", status.duration_rounds)),
            )
            return
        refreshed_status = BattleStatusEffect(
            status_id=existing.status_id,
            status_name=existing.status_name,
            category=existing.category,
            holder_unit_id=existing.holder_unit_id,
            source_unit_id=status.source_unit_id,
            source_action_id=status.source_action_id,
            intensity_permille=max(existing.intensity_permille, status.intensity_permille),
            duration_rounds=status.duration_rounds,
            stack_count=1,
            max_stacks=1,
            base_value=0,
            applied_round=status.applied_round,
        )
        remaining_statuses = [
            item
            for item in target.statuses
            if item.category is not BattleStatusCategory.ATTRIBUTE_SUPPRESSION
        ]
        target.statuses = list(_ordered_statuses(remaining_statuses + [refreshed_status]))
        context.emit_event(
            phase=BattleEventPhase.SETTLEMENT,
            event_type="status_refreshed",
            actor_unit_id=status.source_unit_id,
            target_unit_id=target.unit_id,
            action_id=status.source_action_id,
            detail_items=(("status_id", refreshed_status.status_id), ("intensity_permille", refreshed_status.intensity_permille)),
        )

    def _cleanup_death(
        self,
        *,
        context: BattleRuntimeContext,
        killer_unit_id: str,
        target: BattleUnitState,
        action_id: str,
    ) -> None:
        target.current_shield = 0
        target.statuses.clear()
        target.current_target_unit_id = None
        context.add_stat(unit_id=killer_unit_id, field_name="kills", delta=1)
        context.add_stat(unit_id=target.unit_id, field_name="deaths", delta=1)
        context.emit_event(
            phase=BattleEventPhase.SETTLEMENT,
            event_type="unit_defeated",
            actor_unit_id=killer_unit_id,
            target_unit_id=target.unit_id,
            action_id=action_id,
            detail_items=(("remaining_hp", target.current_hp),),
        )

    @staticmethod
    def _get_active_hard_control(unit: BattleUnitState) -> BattleStatusEffect | None:
        return next(
            (
                status
                for status in unit.ordered_statuses()
                if status.category is BattleStatusCategory.HARD_CONTROL
            ),
            None,
        )

    @staticmethod
    def _resolve_hit_chance_permille(*, actor: BattleUnitState, target: BattleUnitState) -> int:
        return _clamp(
            1000 + actor.base_snapshot.hit_rate_permille - target.base_snapshot.dodge_rate_permille,
            _MINIMUM_HIT_CHANCE_PERMILLE,
            1000,
        )

    @staticmethod
    def _resolve_guard_reduction_permille(*, actor: BattleUnitState, target: BattleUnitState) -> int:
        attack_power = max(1, actor.effective_attack_power)
        guard_power = max(0, target.effective_guard_power)
        if guard_power <= 0:
            return 0
        return min(
            _MAXIMUM_GUARD_REDUCTION_PERMILLE,
            guard_power * 1000 // (guard_power + attack_power + 1),
        )

    @staticmethod
    def _resolve_penetration_permille(*, action: CompiledBehaviorAction) -> int:
        penetration_permille = 0
        label_set = set(action.labels)
        if "anti_guard" in label_set:
            penetration_permille += 350
        if "guard_convert" in label_set:
            penetration_permille += 220
        if "execute" in label_set:
            penetration_permille += 160
        if action.action_type is BattleActionType.FINISHER:
            penetration_permille += 120
        return min(800, penetration_permille)

    def _roll_success(
        self,
        *,
        context: BattleRuntimeContext,
        chance_permille: int,
        purpose: str,
    ) -> tuple[int, bool]:
        if chance_permille <= 0:
            return 0, False
        if chance_permille >= 1000:
            return 1000, True
        roll = context.random_source.next_int(minimum=1, maximum=1000, purpose=purpose)
        return roll, roll <= chance_permille

    @staticmethod
    def _action_can_pursue(action: CompiledBehaviorAction) -> bool:
        label_set = set(action.labels)
        return "pursuit" in label_set or action.action_type is BattleActionType.COMBO_ATTACK


@dataclass(frozen=True, slots=True)
class _DamageResolution:
    """单目标伤害结算结果。"""

    can_pursue: bool = False
    counter_candidate: CounterCandidate | None = None
    pending_death: PendingDeathCleanup | None = None


def _ordered_statuses(statuses: list[BattleStatusEffect] | tuple[BattleStatusEffect, ...]) -> tuple[BattleStatusEffect, ...]:
    """按稳定顺序返回状态序列。"""
    return tuple(
        sorted(
            statuses,
            key=lambda item: (
                item.category.value,
                item.status_id,
                item.source_unit_id,
                item.source_action_id,
                item.applied_round,
            ),
        )
    )


def _resolve_behavior_template(
    *,
    behavior_template_id: str,
    behavior_templates: Mapping[str, CompiledBehaviorTemplate],
) -> CompiledBehaviorTemplate:
    """按稳定规则解析单位对应的行为模板。"""
    direct_template = behavior_templates.get(behavior_template_id)
    if direct_template is not None:
        return direct_template
    ordered_templates = tuple(
        sorted(
            behavior_templates.values(),
            key=lambda item: (item.template_id, item.path_id, item.name),
        )
    )
    for template in ordered_templates:
        if behavior_template_id in (template.template_id, template.path_id):
            return template
    raise KeyError(f"未找到行为模板：{behavior_template_id}")


def _clamp(value: int, minimum: int, maximum: int) -> int:
    """把整数裁剪到固定区间。"""
    return max(minimum, min(maximum, value))


def _require_non_blank(value: str, *, field_name: str) -> None:
    """校验字符串字段不能为空。"""
    if not value or not value.strip():
        raise ValueError(f"{field_name} 不能为空")


__all__ = [
    "ActionSettlementOutcome",
    "BattleRuntimeContext",
    "BattleSettlementEngine",
    "CounterCandidate",
    "PendingDeathCleanup",
    "SeededBattleRandomSource",
]
