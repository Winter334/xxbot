"""自动战斗回合驱动与行动队列。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from domain.battle.models import (
    BattleActionDecision,
    BattleActionQueueEntry,
    BattleActionType,
    BattleEventPhase,
    BattleOutcome,
    BattleRandomSource,
    BattleReactionType,
    BattleResult,
    BattleSide,
    BattleSnapshot,
    BattleUnitState,
    CompiledBehaviorAction,
    CompiledBehaviorTemplate,
    TargetSelectionStrategy,
)
from domain.battle.settlement import (
    BattleRuntimeContext,
    BattleSettlementEngine,
    CounterCandidate,
    PendingDeathCleanup,
    SeededBattleRandomSource,
)
from domain.battle.special_effects import BattleSpecialEffectHook, BattleSpecialEffectRegistry


class BattleTurnEngine:
    """负责自动战斗回合推进、行动决策与即时反应链。"""

    def __init__(
        self,
        *,
        settlement_engine: BattleSettlementEngine | None = None,
        special_effect_registry: BattleSpecialEffectRegistry | None = None,
    ) -> None:
        self._settlement_engine = settlement_engine or BattleSettlementEngine()
        self._special_effect_registry = special_effect_registry or BattleSpecialEffectRegistry()

    def execute(
        self,
        *,
        snapshot: BattleSnapshot,
        behavior_templates: Mapping[str, CompiledBehaviorTemplate],
        random_source: BattleRandomSource,
    ) -> BattleResult:
        """按固定回合流程执行整场自动战斗。"""
        context = BattleRuntimeContext.from_snapshot(
            snapshot=snapshot,
            behavior_templates=behavior_templates,
            random_source=random_source,
            special_effect_registry=self._special_effect_registry,
        )
        context.special_effect_registry.dispatch(
            hook=BattleSpecialEffectHook.BATTLE_START,
            runtime_context=context,
        )

        outcome: BattleOutcome | None = self._resolve_outcome(context=context)
        while outcome is None and context.round_index < snapshot.round_limit:
            context.round_index += 1
            self._process_round_start(context=context)
            queue = self._build_action_queue(context=context)
            self._process_action_queue(context=context, queue=queue)
            self._process_round_end(context=context)
            outcome = self._resolve_outcome(context=context)

        if outcome is None:
            outcome = self._resolve_round_limit_outcome(context=context)

        context.special_effect_registry.dispatch(
            hook=BattleSpecialEffectHook.BATTLE_END,
            runtime_context=context,
        )
        context.emit_event(
            phase=BattleEventPhase.BATTLE_END,
            event_type="battle_finished",
            detail_items=(("outcome", outcome.value), ("completed_rounds", context.round_index)),
        )
        return BattleResult(
            outcome=outcome,
            completed_rounds=context.round_index,
            final_units=context.ordered_units(),
            events=tuple(context.events),
            random_calls=context.random_source.export_calls(),
            statistics=context.build_statistics(),
        )

    def _process_round_start(self, *, context: BattleRuntimeContext) -> None:
        """处理回合开始全局事件与状态重置。"""
        for unit in context.ordered_units():
            unit.counter_used_this_round = False
        context.special_effect_registry.dispatch(
            hook=BattleSpecialEffectHook.ROUND_START,
            runtime_context=context,
        )
        context.emit_event(
            phase=BattleEventPhase.ROUND_START,
            event_type="round_started",
            detail_items=(
                ("round_index", context.round_index),
                ("alive_allies", len(context.alive_units(side=BattleSide.ALLY))),
                ("alive_enemies", len(context.alive_units(side=BattleSide.ENEMY))),
            ),
        )

    def _build_action_queue(self, *, context: BattleRuntimeContext) -> tuple[BattleActionQueueEntry, ...]:
        """按固定规则生成当前回合行动队列。"""
        queue = tuple(
            BattleActionQueueEntry(
                actor_unit_id=unit.unit_id,
                effective_speed=unit.effective_speed,
                stable_first_strike_key=unit.stable_first_strike_key,
                side_id=unit.side_id,
                stable_order=unit.stable_order,
            )
            for unit in context.alive_units()
        )
        ordered_queue = tuple(
            sorted(
                queue,
                key=lambda item: (
                    -item.effective_speed,
                    item.stable_first_strike_key,
                    item.side_id,
                    item.stable_order,
                ),
            )
        )
        for index, entry in enumerate(ordered_queue, start=1):
            context.emit_event(
                phase=BattleEventPhase.ACTION_QUEUE,
                event_type="action_queue_entry",
                actor_unit_id=entry.actor_unit_id,
                detail_items=(
                    ("queue_index", index),
                    ("effective_speed", entry.effective_speed),
                    ("stable_first_strike_key", entry.stable_first_strike_key),
                    ("side_id", entry.side_id),
                    ("stable_order", entry.stable_order),
                ),
            )
        return ordered_queue

    def _process_action_queue(
        self,
        *,
        context: BattleRuntimeContext,
        queue: Sequence[BattleActionQueueEntry],
    ) -> bool:
        """按队列逐个处理单位行动。"""
        for entry in queue:
            actor = context.get_unit(entry.actor_unit_id)
            if not actor.is_alive:
                context.emit_event(
                    phase=BattleEventPhase.TURN_START,
                    event_type="turn_skipped_dead",
                    actor_unit_id=actor.unit_id,
                )
                continue

            actor.turn_count += 1
            context.emit_event(
                phase=BattleEventPhase.TURN_START,
                event_type="turn_started",
                actor_unit_id=actor.unit_id,
                detail_items=(
                    ("turn_count", actor.turn_count),
                    ("current_hp", actor.current_hp),
                    ("current_shield", actor.current_shield),
                    ("current_resource", actor.current_resource),
                ),
            )

            can_act = self._settlement_engine.process_turn_start(context=context, actor_unit_id=actor.unit_id)
            if can_act:
                decision = self._select_action(context=context, actor=actor)
                context.emit_event(
                    phase=BattleEventPhase.ACTION_DECISION,
                    event_type="action_selected",
                    actor_unit_id=actor.unit_id,
                    action_id=decision.action.action_id,
                    detail_items=(
                        ("action_type", decision.action.action_type.value),
                        ("is_fallback", decision.is_fallback),
                        ("target_count", len(decision.target_unit_ids)),
                    ),
                )
                self._execute_action(context=context, decision=decision)
            else:
                context.emit_event(
                    phase=BattleEventPhase.ACTION_DECISION,
                    event_type="action_skipped",
                    actor_unit_id=actor.unit_id,
                    detail_items=(("reason", "turn_start_blocked"),),
                )

            self._settlement_engine.process_turn_end(context=context, actor_unit_id=actor.unit_id)
            context.emit_event(
                phase=BattleEventPhase.TURN_END,
                event_type="turn_ended",
                actor_unit_id=actor.unit_id,
                detail_items=(
                    ("current_hp", actor.current_hp),
                    ("current_shield", actor.current_shield),
                    ("current_resource", actor.current_resource),
                ),
            )

            if self._resolve_outcome(context=context) is not None:
                return False
        return True

    def _process_round_end(self, *, context: BattleRuntimeContext) -> None:
        """处理回合结束全局事件。"""
        outcome = self._resolve_outcome(context=context)
        context.special_effect_registry.dispatch(
            hook=BattleSpecialEffectHook.ROUND_END,
            runtime_context=context,
        )
        context.emit_event(
            phase=BattleEventPhase.ROUND_END,
            event_type="round_finished",
            detail_items=(
                ("round_index", context.round_index),
                ("alive_allies", len(context.alive_units(side=BattleSide.ALLY))),
                ("alive_enemies", len(context.alive_units(side=BattleSide.ENEMY))),
                ("outcome", None if outcome is None else outcome.value),
            ),
        )

    def _execute_action(self, *, context: BattleRuntimeContext, decision: BattleActionDecision) -> None:
        """执行动作并处理追击与反击链。"""
        settlement_outcome = self._settlement_engine.settle_action(context=context, decision=decision)
        pending_deaths: list[PendingDeathCleanup] = list(settlement_outcome.pending_deaths)
        counter_candidates: list[CounterCandidate] = list(settlement_outcome.counter_candidates)

        if settlement_outcome.can_pursue and decision.reaction_type is not BattleReactionType.COUNTER:
            pending_deaths, counter_candidates = self._resolve_pursuit_chain(
                context=context,
                base_decision=decision,
                inherited_pending_deaths=pending_deaths,
                inherited_counter_candidates=counter_candidates,
            )

        self._resolve_counter_chain(
            context=context,
            base_decision=decision,
            pending_deaths=pending_deaths,
            counter_candidates=counter_candidates,
        )
        self._settlement_engine.cleanup_pending_deaths(context=context, pending_deaths=pending_deaths)

    def _resolve_pursuit_chain(
        self,
        *,
        context: BattleRuntimeContext,
        base_decision: BattleActionDecision,
        inherited_pending_deaths: list[PendingDeathCleanup],
        inherited_counter_candidates: list[CounterCandidate],
    ) -> tuple[list[PendingDeathCleanup], list[CounterCandidate]]:
        """先收敛同一原始动作触发的追击链。"""
        pending_deaths = list(inherited_pending_deaths)
        pending_counter_candidates = list(inherited_counter_candidates)
        actor = context.get_unit(base_decision.actor_unit_id)
        if not actor.is_alive or len(base_decision.target_unit_ids) != 1:
            return pending_deaths, pending_counter_candidates

        target_unit_id = base_decision.target_unit_ids[0]
        pursuit_count = 0
        while True:
            if pursuit_count >= max(0, base_decision.action.max_triggers - 1):
                break
            pursuit_action = self._select_pursuit_action(actor=actor)
            if pursuit_action is None:
                break
            target = context.get_unit(target_unit_id)
            if not target.is_alive or not actor.is_alive:
                break
            pursuit_count += 1
            context.add_stat(unit_id=actor.unit_id, field_name="pursuits_triggered", delta=1)
            pursuit_decision = BattleActionDecision(
                actor_unit_id=actor.unit_id,
                action=pursuit_action,
                target_unit_ids=(target.unit_id,),
                reaction_type=BattleReactionType.PURSUIT,
                reaction_depth=base_decision.reaction_depth + pursuit_count,
                origin_action_id=base_decision.origin_action_id or base_decision.action.action_id,
                consume_cost=False,
                can_trigger_counter=True,
            )
            context.emit_event(
                phase=BattleEventPhase.REACTION,
                event_type="pursuit_triggered",
                actor_unit_id=actor.unit_id,
                target_unit_id=target.unit_id,
                action_id=pursuit_action.action_id,
                detail_items=(
                    ("origin_action_id", base_decision.action.action_id),
                    ("pursuit_index", pursuit_count),
                ),
            )
            settlement_outcome = self._settlement_engine.settle_action(context=context, decision=pursuit_decision)
            pending_deaths.extend(settlement_outcome.pending_deaths)
            pending_counter_candidates.extend(settlement_outcome.counter_candidates)
            if not settlement_outcome.can_pursue:
                break
        return pending_deaths, pending_counter_candidates

    def _resolve_counter_chain(
        self,
        *,
        context: BattleRuntimeContext,
        base_decision: BattleActionDecision,
        pending_deaths: list[PendingDeathCleanup],
        counter_candidates: Sequence[CounterCandidate],
    ) -> None:
        """在追击链完全收敛后统一处理反击。"""
        if base_decision.reaction_type is BattleReactionType.COUNTER:
            return
        processed_unit_ids: set[str] = set()
        ordered_candidates = tuple(
            sorted(
                counter_candidates,
                key=lambda item: (
                    context.get_unit(item.counter_actor_unit_id).side_id,
                    context.get_unit(item.counter_actor_unit_id).stable_order,
                    item.counter_actor_unit_id,
                    item.target_unit_id,
                    item.origin_action_id,
                ),
            )
        )
        for candidate in ordered_candidates:
            counter_actor = context.get_unit(candidate.counter_actor_unit_id)
            if counter_actor.unit_id in processed_unit_ids:
                continue
            processed_unit_ids.add(counter_actor.unit_id)
            if not counter_actor.is_alive or counter_actor.counter_used_this_round:
                continue
            counter_action = self._select_counter_action(actor=counter_actor)
            if counter_action is None:
                continue
            target = context.get_unit(candidate.target_unit_id)
            if not target.is_alive:
                continue
            if not self._can_trigger_counter(context=context, actor=counter_actor, target=target):
                continue
            counter_actor.counter_used_this_round = True
            context.add_stat(unit_id=counter_actor.unit_id, field_name="counters_triggered", delta=1)
            counter_decision = BattleActionDecision(
                actor_unit_id=counter_actor.unit_id,
                action=counter_action,
                target_unit_ids=(target.unit_id,),
                reaction_type=BattleReactionType.COUNTER,
                reaction_depth=base_decision.reaction_depth + 1,
                origin_action_id=candidate.origin_action_id,
                consume_cost=False,
                can_trigger_counter=False,
            )
            context.emit_event(
                phase=BattleEventPhase.REACTION,
                event_type="counter_triggered",
                actor_unit_id=counter_actor.unit_id,
                target_unit_id=target.unit_id,
                action_id=counter_action.action_id,
                detail_items=(("origin_action_id", candidate.origin_action_id),),
            )
            settlement_outcome = self._settlement_engine.settle_action(context=context, decision=counter_decision)
            pending_deaths.extend(settlement_outcome.pending_deaths)

    def _select_action(
        self,
        *,
        context: BattleRuntimeContext,
        actor: BattleUnitState,
    ) -> BattleActionDecision:
        """根据行为模板选择当前动作，必要时降级为保底动作。"""
        ordered_actions = tuple(
            sorted(
                actor.behavior_template.actions,
                key=lambda item: (item.execution_order, item.source_order, item.action_id),
            )
        )
        eligible_actions = tuple(
            action
            for action in ordered_actions
            if self._action_meets_conditions(context=context, actor=actor, action=action, relaxed=False)
        )
        if eligible_actions:
            selected_action = self._pick_weighted_action(
                context=context,
                actions=eligible_actions,
                purpose=f"action:{context.round_index}:{actor.unit_id}",
            )
            return BattleActionDecision(
                actor_unit_id=actor.unit_id,
                action=selected_action,
                target_unit_ids=self._select_targets(context=context, actor=actor, action=selected_action),
                is_fallback=False,
            )

        fallback_candidates = tuple(
            action
            for action in ordered_actions
            if self._action_meets_conditions(context=context, actor=actor, action=action, relaxed=True)
        )
        fallback_action = fallback_candidates[0] if fallback_candidates else ordered_actions[0]
        return BattleActionDecision(
            actor_unit_id=actor.unit_id,
            action=fallback_action,
            target_unit_ids=self._select_targets(context=context, actor=actor, action=fallback_action),
            is_fallback=True,
        )

    def _pick_weighted_action(
        self,
        *,
        context: BattleRuntimeContext,
        actions: Sequence[CompiledBehaviorAction],
        purpose: str,
    ) -> CompiledBehaviorAction:
        """按稳定顺序使用权重选择动作。"""
        ordered_actions = tuple(
            sorted(actions, key=lambda item: (item.execution_order, item.source_order, item.action_id))
        )
        total_weight = sum(action.weight_permille for action in ordered_actions)
        if total_weight <= 0:
            return ordered_actions[0]
        roll = context.random_source.next_int(minimum=1, maximum=total_weight, purpose=purpose)
        cursor = 0
        for action in ordered_actions:
            cursor += action.weight_permille
            if roll <= cursor:
                return action
        return ordered_actions[-1]

    def _select_targets(
        self,
        *,
        context: BattleRuntimeContext,
        actor: BattleUnitState,
        action: CompiledBehaviorAction,
    ) -> tuple[str, ...]:
        """按目标策略与稳定顺序选择目标。"""
        strategy = action.target_strategy
        if strategy is TargetSelectionStrategy.SELF:
            return (actor.unit_id,)

        if strategy is TargetSelectionStrategy.ALL_ENEMIES:
            enemies = self._ordered_enemy_candidates(context=context, actor=actor)
            if enemies:
                return tuple(unit.unit_id for unit in enemies)
            return (actor.unit_id,)

        if strategy is TargetSelectionStrategy.ALLY_LOWEST_HP_PERCENT:
            allies = self._ordered_ally_candidates(context=context, actor=actor)
            return (allies[0].unit_id,) if allies else (actor.unit_id,)

        enemies = self._ordered_enemy_candidates(context=context, actor=actor)
        if not enemies:
            return (actor.unit_id,)

        if strategy is TargetSelectionStrategy.CURRENT_TARGET:
            current_target = actor.current_target_unit_id
            if current_target is not None:
                current_unit = context.get_unit(current_target)
                if current_unit.is_alive and current_unit.side is not actor.side:
                    return (current_unit.unit_id,)
            selected = enemies[0]
            actor.current_target_unit_id = selected.unit_id
            return (selected.unit_id,)

        if strategy is TargetSelectionStrategy.LOWEST_HP_PERCENT:
            selected = min(
                enemies,
                key=lambda item: (item.hp_ratio_permille, item.side_id, item.stable_order, item.unit_id),
            )
        elif strategy is TargetSelectionStrategy.HIGHEST_ATTACK:
            selected = max(
                enemies,
                key=lambda item: (item.effective_attack_power, -item.side_id, -item.stable_order, item.unit_id),
            )
        else:
            selected = max(
                enemies,
                key=lambda item: (item.effective_guard_power, -item.side_id, -item.stable_order, item.unit_id),
            )
        actor.current_target_unit_id = selected.unit_id
        return (selected.unit_id,)

    @staticmethod
    def _ordered_enemy_candidates(
        *,
        context: BattleRuntimeContext,
        actor: BattleUnitState,
    ) -> tuple[BattleUnitState, ...]:
        return tuple(
            unit
            for unit in context.ordered_units()
            if unit.is_alive and unit.side is not actor.side
        )

    @staticmethod
    def _ordered_ally_candidates(
        *,
        context: BattleRuntimeContext,
        actor: BattleUnitState,
    ) -> tuple[BattleUnitState, ...]:
        return tuple(
            sorted(
                (
                    unit
                    for unit in context.ordered_units()
                    if unit.is_alive and unit.side is actor.side
                ),
                key=lambda item: (item.hp_ratio_permille, item.side_id, item.stable_order, item.unit_id),
            )
        )

    def _action_meets_conditions(
        self,
        *,
        context: BattleRuntimeContext,
        actor: BattleUnitState,
        action: CompiledBehaviorAction,
        relaxed: bool,
    ) -> bool:
        if actor.cooldowns.get(action.action_id, 0) > 0:
            return False
        if not relaxed and action.resource_cost > actor.current_resource:
            return False
        if not relaxed and action.self_hp_below_permille is not None:
            if actor.hp_ratio_permille >= action.self_hp_below_permille:
                return False
        if not relaxed and action.resource_above_permille is not None:
            if actor.resource_ratio_permille < action.resource_above_permille:
                return False
        enemy_count = len(self._ordered_enemy_candidates(context=context, actor=actor))
        if action.enemy_count_at_least is not None and enemy_count < action.enemy_count_at_least:
            return False
        if action.target_hp_below_permille is not None:
            candidate_targets = self._ordered_enemy_candidates(context=context, actor=actor)
            if not candidate_targets:
                return False
            lowest_target_ratio = min(unit.hp_ratio_permille for unit in candidate_targets)
            if lowest_target_ratio >= action.target_hp_below_permille:
                return False
        return True

    @staticmethod
    def _select_pursuit_action(actor: BattleUnitState) -> CompiledBehaviorAction | None:
        ordered_actions = tuple(
            sorted(
                actor.behavior_template.actions,
                key=lambda item: (item.execution_order, item.source_order, item.action_id),
            )
        )
        for action in ordered_actions:
            if action.action_type is BattleActionType.COMBO_ATTACK or "pursuit" in action.labels:
                return action
        return None

    @staticmethod
    def _select_counter_action(actor: BattleUnitState) -> CompiledBehaviorAction | None:
        ordered_actions = tuple(
            sorted(
                actor.behavior_template.actions,
                key=lambda item: (item.execution_order, item.source_order, item.action_id),
            )
        )
        for action in ordered_actions:
            if action.action_type is BattleActionType.COUNTER_ATTACK or "counter" in action.labels:
                return action
        return None

    @staticmethod
    def _can_trigger_counter(
        *,
        context: BattleRuntimeContext,
        actor: BattleUnitState,
        target: BattleUnitState,
    ) -> bool:
        chance_permille = actor.base_snapshot.counter_rate_permille
        if chance_permille <= 0:
            return False
        if chance_permille >= 1000:
            context.emit_event(
                phase=BattleEventPhase.REACTION,
                event_type="counter_check",
                actor_unit_id=actor.unit_id,
                target_unit_id=target.unit_id,
                detail_items=(("chance_permille", chance_permille), ("roll", 1000), ("success", True)),
            )
            return True
        roll = context.random_source.next_int(
            minimum=1,
            maximum=1000,
            purpose=f"counter:{context.round_index}:{actor.unit_id}:{target.unit_id}",
        )
        success = roll <= chance_permille
        context.emit_event(
            phase=BattleEventPhase.REACTION,
            event_type="counter_check",
            actor_unit_id=actor.unit_id,
            target_unit_id=target.unit_id,
            detail_items=(("chance_permille", chance_permille), ("roll", roll), ("success", success)),
        )
        return success

    @staticmethod
    def _resolve_outcome(*, context: BattleRuntimeContext) -> BattleOutcome | None:
        ally_alive = len(context.alive_units(side=BattleSide.ALLY))
        enemy_alive = len(context.alive_units(side=BattleSide.ENEMY))
        if ally_alive == 0 and enemy_alive == 0:
            return BattleOutcome.DRAW
        if enemy_alive == 0:
            return BattleOutcome.ALLY_VICTORY
        if ally_alive == 0:
            return BattleOutcome.ENEMY_VICTORY
        return None

    @staticmethod
    def _resolve_round_limit_outcome(*, context: BattleRuntimeContext) -> BattleOutcome:
        ally_alive = len(context.alive_units(side=BattleSide.ALLY))
        enemy_alive = len(context.alive_units(side=BattleSide.ENEMY))
        if ally_alive > enemy_alive:
            return BattleOutcome.ALLY_VICTORY
        if enemy_alive > ally_alive:
            return BattleOutcome.ENEMY_VICTORY
        ally_hp = sum(unit.current_hp for unit in context.alive_units(side=BattleSide.ALLY))
        enemy_hp = sum(unit.current_hp for unit in context.alive_units(side=BattleSide.ENEMY))
        if ally_hp > enemy_hp:
            return BattleOutcome.ALLY_VICTORY
        if enemy_hp > ally_hp:
            return BattleOutcome.ENEMY_VICTORY
        return BattleOutcome.DRAW


__all__ = ["BattleTurnEngine"]
