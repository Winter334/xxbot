"""战斗特殊效果运行态与事件分发。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol

from domain.battle.models import (
    BattleActionType,
    BattleEventPhase,
    BattleStatusCategory,
    BattleStatusEffect,
    CompiledBehaviorAction,
    TargetSelectionStrategy,
)

if TYPE_CHECKING:
    from domain.battle.models import BattleActionDecision, BattleUnitSnapshot, BattleUnitState


class BattleSpecialEffectHook(StrEnum):
    """特殊效果可挂接的运行期事件。"""

    BATTLE_START = "battle_start"
    ROUND_START = "round_start"
    TURN_START = "turn_start"
    BEFORE_ACTION = "before_action"
    AFTER_ACTION = "after_action"
    DAMAGE_RESOLVED = "damage_resolved"
    DAMAGE_TAKEN = "damage_taken"
    TURN_END = "turn_end"
    ROUND_END = "round_end"
    BATTLE_END = "battle_end"


@dataclass(slots=True)
class BattleSpecialEffectState:
    """单个特殊效果的战斗运行态。"""

    effect_id: str
    effect_name: str
    effect_type: str
    trigger_event: str
    owner_unit_id: str
    source_affix_id: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    cooldown_remaining: int = 0
    stack_count: int = 1
    max_stacks: int = 1
    triggers_used_this_round: int = 0
    triggers_used_this_battle: int = 0
    internal_counters: dict[str, int] = field(default_factory=dict)
    disabled: bool = False

    def __post_init__(self) -> None:
        _require_non_blank(self.effect_id, field_name="effect_id")
        _require_non_blank(self.effect_name, field_name="effect_name")
        _require_non_blank(self.effect_type, field_name="effect_type")
        _require_non_blank(self.trigger_event, field_name="trigger_event")
        _require_non_blank(self.owner_unit_id, field_name="owner_unit_id")
        self.payload = dict(self.payload)
        self.internal_counters = dict(self.internal_counters)
        self.cooldown_remaining = max(0, self.cooldown_remaining)
        self.stack_count = max(1, self.stack_count)
        self.max_stacks = max(1, self.max_stacks)
        if self.stack_count > self.max_stacks:
            self.stack_count = self.max_stacks
        self.triggers_used_this_round = max(0, self.triggers_used_this_round)
        self.triggers_used_this_battle = max(0, self.triggers_used_this_battle)

    @property
    def configured_cooldown_rounds(self) -> int:
        """返回效果默认冷却回合数。"""
        return _coerce_non_negative_int(self.payload.get("cooldown_rounds"), default=0)

    @property
    def trigger_limit_per_round(self) -> int | None:
        """返回每回合触发上限。"""
        value = self.payload.get("max_triggers_per_round")
        if value is None:
            return None
        return max(0, _coerce_non_negative_int(value, default=0))

    @property
    def trigger_limit_per_battle(self) -> int | None:
        """返回每战触发上限。"""
        value = self.payload.get("max_triggers_per_battle")
        if value is None:
            return None
        return max(0, _coerce_non_negative_int(value, default=0))

    def matches_hook(self, hook: BattleSpecialEffectHook) -> bool:
        """判断当前效果是否订阅指定钩子。"""
        return self.trigger_event in {hook.value, "any"}

    def can_trigger(self) -> bool:
        """判断当前效果是否满足触发条件。"""
        if self.disabled or self.cooldown_remaining > 0:
            return False
        if self.trigger_limit_per_round is not None and self.triggers_used_this_round >= self.trigger_limit_per_round:
            return False
        if self.trigger_limit_per_battle is not None and self.triggers_used_this_battle >= self.trigger_limit_per_battle:
            return False
        return True

    def begin_round(self) -> None:
        """处理回合开始时的公共状态递进。"""
        self.triggers_used_this_round = 0
        if self.cooldown_remaining > 0:
            self.cooldown_remaining -= 1

    def consume_trigger(self, *, cooldown_rounds: int | None = None) -> None:
        """记录一次触发并进入冷却。"""
        self.triggers_used_this_round += 1
        self.triggers_used_this_battle += 1
        next_cooldown = self.configured_cooldown_rounds if cooldown_rounds is None else max(0, cooldown_rounds)
        self.cooldown_remaining = next_cooldown

    def add_stacks(self, delta: int, *, maximum: int | None = None) -> None:
        """调整层数。"""
        upper_bound = self.max_stacks if maximum is None else max(1, maximum)
        self.max_stacks = upper_bound
        self.stack_count = max(1, min(upper_bound, self.stack_count + delta))

    def set_counter(self, name: str, value: int) -> None:
        """写入内部计数器。"""
        _require_non_blank(name, field_name="counter_name")
        self.internal_counters[name] = value

    def increment_counter(self, name: str, delta: int = 1) -> int:
        """累加内部计数器并返回新值。"""
        _require_non_blank(name, field_name="counter_name")
        current_value = self.internal_counters.get(name, 0)
        next_value = current_value + delta
        self.internal_counters[name] = next_value
        return next_value


@dataclass(frozen=True, slots=True)
class BattleSpecialEffectTriggerContext:
    """传递给特殊效果处理器的触发上下文。"""

    hook: BattleSpecialEffectHook
    runtime_context: Any
    owner_unit_id: str
    actor_unit_id: str | None = None
    target_unit_id: str | None = None
    action_id: str | None = None
    decision: BattleActionDecision | None = None
    resolved_value: int | None = None
    extra_payload: dict[str, Any] = field(default_factory=dict)


class BattleSpecialEffectHandler(Protocol):
    """特殊效果处理器协议。"""

    def handle(
        self,
        *,
        effect_state: BattleSpecialEffectState,
        trigger_context: BattleSpecialEffectTriggerContext,
    ) -> None:
        """处理一次特殊效果事件。"""


class BattleSpecialEffectRegistry:
    """管理特殊效果处理器并负责统一分发。"""

    def __init__(self, handlers: dict[str, BattleSpecialEffectHandler] | None = None) -> None:
        self._handlers: dict[str, BattleSpecialEffectHandler] = _build_default_handlers()
        if handlers is not None:
            self._handlers.update(dict(handlers))

    def register(self, *, effect_type: str, handler: BattleSpecialEffectHandler) -> None:
        """注册单个特殊效果处理器。"""
        _require_non_blank(effect_type, field_name="effect_type")
        self._handlers[effect_type] = handler

    def build_states(self, *, unit_snapshot: BattleUnitSnapshot) -> tuple[BattleSpecialEffectState, ...]:
        """根据单位快照中的效果载荷构造运行态。"""
        states: list[BattleSpecialEffectState] = []
        for payload in unit_snapshot.special_effect_payloads:
            if not isinstance(payload, dict):
                continue
            effect_id = str(payload.get("effect_id") or "").strip()
            if not effect_id:
                continue
            effect_payload = payload.get("payload")
            normalized_payload = dict(effect_payload) if isinstance(effect_payload, dict) else {}
            for key in (
                "cooldown_rounds",
                "max_stacks",
                "initial_stacks",
                "initial_cooldown",
                "max_triggers_per_round",
                "max_triggers_per_battle",
            ):
                if key in payload:
                    normalized_payload[key] = payload[key]
            initial_stacks = max(
                1,
                _coerce_non_negative_int(
                    payload.get("initial_stacks", normalized_payload.get("initial_stacks")),
                    default=1,
                ),
            )
            max_stacks = max(
                1,
                _coerce_non_negative_int(
                    payload.get("max_stacks", normalized_payload.get("max_stacks")),
                    default=initial_stacks,
                ),
            )
            states.append(
                BattleSpecialEffectState(
                    effect_id=effect_id,
                    effect_name=str(payload.get("effect_name") or effect_id),
                    effect_type=str(payload.get("effect_type") or "unknown"),
                    trigger_event=str(payload.get("trigger_event") or BattleSpecialEffectHook.TURN_START.value),
                    owner_unit_id=unit_snapshot.unit_id,
                    source_affix_id=str(payload.get("affix_id") or ""),
                    payload=normalized_payload,
                    cooldown_remaining=_coerce_non_negative_int(
                        payload.get("initial_cooldown", normalized_payload.get("initial_cooldown")),
                        default=0,
                    ),
                    stack_count=min(initial_stacks, max_stacks),
                    max_stacks=max_stacks,
                )
            )
        return tuple(states)

    def dispatch(
        self,
        *,
        hook: BattleSpecialEffectHook,
        runtime_context: Any,
        owner_unit_id: str | None = None,
        actor_unit_id: str | None = None,
        target_unit_id: str | None = None,
        action_id: str | None = None,
        decision: BattleActionDecision | None = None,
        resolved_value: int | None = None,
        extra_payload: dict[str, Any] | None = None,
    ) -> None:
        """把指定钩子广播给命中的特殊效果实例。"""
        units = (
            (runtime_context.get_unit(owner_unit_id),)
            if owner_unit_id is not None
            else runtime_context.ordered_units()
        )
        for unit in units:
            if hook is BattleSpecialEffectHook.ROUND_START:
                for effect_state in unit.special_effect_states:
                    effect_state.begin_round()
            for effect_state in unit.ordered_special_effects():
                if not effect_state.matches_hook(hook):
                    continue
                if not effect_state.can_trigger():
                    continue
                handler = self._handlers.get(effect_state.effect_type)
                if handler is None:
                    continue
                before_trigger_count = effect_state.triggers_used_this_battle
                handler.handle(
                    effect_state=effect_state,
                    trigger_context=BattleSpecialEffectTriggerContext(
                        hook=hook,
                        runtime_context=runtime_context,
                        owner_unit_id=unit.unit_id,
                        actor_unit_id=actor_unit_id,
                        target_unit_id=target_unit_id,
                        action_id=action_id,
                        decision=decision,
                        resolved_value=resolved_value,
                        extra_payload={} if extra_payload is None else dict(extra_payload),
                    ),
                )
                trigger_delta = effect_state.triggers_used_this_battle - before_trigger_count
                if trigger_delta <= 0:
                    continue
                runtime_context.add_stat(
                    unit_id=unit.unit_id,
                    field_name="special_effects_triggered",
                    delta=trigger_delta,
                )
                runtime_context.emit_event(
                    phase=_resolve_phase_for_hook(hook),
                    event_type="special_effect_triggered",
                    actor_unit_id=actor_unit_id or unit.unit_id,
                    target_unit_id=target_unit_id,
                    action_id=action_id,
                    detail_items=(
                        ("effect_id", effect_state.effect_id),
                        ("effect_type", effect_state.effect_type),
                        ("owner_unit_id", effect_state.owner_unit_id),
                        ("hook", hook.value),
                        ("stack_count", effect_state.stack_count),
                        ("cooldown_remaining", effect_state.cooldown_remaining),
                    ),
                )


class _BaseConfiguredEffectHandler:
    """首发特殊词条处理器的通用基类。"""

    def _should_trigger(
        self,
        *,
        effect_state: BattleSpecialEffectState,
        trigger_context: BattleSpecialEffectTriggerContext,
    ) -> bool:
        chance_permille = _read_rate_payload(effect_state.payload.get("trigger_rate_permille"), default=1000)
        if chance_permille <= 0:
            return False
        if chance_permille >= 1000:
            return True
        runtime_context = trigger_context.runtime_context
        action_marker = trigger_context.action_id or effect_state.effect_id
        roll = runtime_context.random_source.next_int(
            minimum=1,
            maximum=1000,
            purpose=(
                f"special_effect:{effect_state.effect_id}:{trigger_context.hook.value}:"
                f"{runtime_context.round_index}:{effect_state.owner_unit_id}:{action_marker}"
            ),
        )
        runtime_context.emit_event(
            phase=_resolve_phase_for_hook(trigger_context.hook),
            event_type="special_effect_roll",
            actor_unit_id=effect_state.owner_unit_id,
            target_unit_id=trigger_context.target_unit_id,
            action_id=trigger_context.action_id,
            detail_items=(
                ("effect_id", effect_state.effect_id),
                ("chance_permille", chance_permille),
                ("roll", roll),
                ("success", roll <= chance_permille),
            ),
        )
        return roll <= chance_permille


class _AttributeSuppressionHandler(_BaseConfiguredEffectHandler):
    """处理属性压制类特殊词条。"""

    def __init__(self, *, target_mode: str) -> None:
        self._target_mode = target_mode

    def handle(
        self,
        *,
        effect_state: BattleSpecialEffectState,
        trigger_context: BattleSpecialEffectTriggerContext,
    ) -> None:
        target = _resolve_target_unit(trigger_context=trigger_context, target_mode=self._target_mode)
        if target is None or not target.is_alive:
            return
        if not self._should_trigger(effect_state=effect_state, trigger_context=trigger_context):
            return
        suppression_permille = _read_rate_payload(effect_state.payload.get("suppression_permille"), default=0)
        duration_rounds = _read_positive_int_payload(effect_state.payload.get("duration_rounds"), default=2)
        if suppression_permille <= 0:
            return
        status = BattleStatusEffect(
            status_id="attribute_suppression",
            status_name="属性压制",
            category=BattleStatusCategory.ATTRIBUTE_SUPPRESSION,
            holder_unit_id=target.unit_id,
            source_unit_id=effect_state.owner_unit_id,
            source_action_id=_resolve_effect_action_id(effect_state=effect_state, trigger_context=trigger_context),
            intensity_permille=suppression_permille,
            duration_rounds=duration_rounds,
            stack_count=1,
            max_stacks=1,
            base_value=0,
            applied_round=max(0, trigger_context.runtime_context.round_index),
        )
        _merge_status_effect(runtime_context=trigger_context.runtime_context, target=target, status=status)
        effect_state.consume_trigger()


class _DamageOverTimeHandler(_BaseConfiguredEffectHandler):
    """处理持续伤害类特殊词条。"""

    def __init__(self, *, target_mode: str) -> None:
        self._target_mode = target_mode

    def handle(
        self,
        *,
        effect_state: BattleSpecialEffectState,
        trigger_context: BattleSpecialEffectTriggerContext,
    ) -> None:
        target = _resolve_target_unit(trigger_context=trigger_context, target_mode=self._target_mode)
        if target is None or not target.is_alive:
            return
        resolved_value = max(0, 0 if trigger_context.resolved_value is None else trigger_context.resolved_value)
        if resolved_value <= 0:
            return
        if not self._should_trigger(effect_state=effect_state, trigger_context=trigger_context):
            return
        dot_ratio_permille = _read_rate_payload(effect_state.payload.get("dot_ratio_permille"), default=0)
        duration_rounds = _read_positive_int_payload(effect_state.payload.get("duration_rounds"), default=2)
        max_stacks = _read_positive_int_payload(effect_state.payload.get("max_stacks"), default=3)
        if dot_ratio_permille <= 0:
            return
        base_value = max(1, resolved_value * dot_ratio_permille // 1000)
        status = BattleStatusEffect(
            status_id="damage_over_time",
            status_name="持续伤害",
            category=BattleStatusCategory.DAMAGE_OVER_TIME,
            holder_unit_id=target.unit_id,
            source_unit_id=effect_state.owner_unit_id,
            source_action_id=_resolve_effect_action_id(effect_state=effect_state, trigger_context=trigger_context),
            intensity_permille=1000,
            duration_rounds=duration_rounds,
            stack_count=1,
            max_stacks=max_stacks,
            base_value=base_value,
            applied_round=max(0, trigger_context.runtime_context.round_index),
        )
        _merge_status_effect(runtime_context=trigger_context.runtime_context, target=target, status=status)
        effect_state.consume_trigger()


class _GuardBarrierHandler(_BaseConfiguredEffectHandler):
    """处理基于护体的护盾类特殊词条。"""

    def __init__(self, *, require_empty_shield: bool = False) -> None:
        self._require_empty_shield = require_empty_shield

    def handle(
        self,
        *,
        effect_state: BattleSpecialEffectState,
        trigger_context: BattleSpecialEffectTriggerContext,
    ) -> None:
        owner = trigger_context.runtime_context.get_unit(effect_state.owner_unit_id)
        if not owner.is_alive:
            return
        if self._require_empty_shield and owner.current_shield > 0:
            return
        if not self._should_trigger(effect_state=effect_state, trigger_context=trigger_context):
            return
        guard_ratio_permille = _read_rate_payload(effect_state.payload.get("guard_ratio_permille"), default=0)
        if guard_ratio_permille <= 0:
            return
        effect_state.consume_trigger()
        _settle_shield_with_action(
            runtime_context=trigger_context.runtime_context,
            effect_state=effect_state,
            trigger_context=trigger_context,
            actor=owner,
            target=owner,
            shield_scale_permille=guard_ratio_permille,
        )


class _ResolvedDamageBarrierHandler(_BaseConfiguredEffectHandler):
    """处理基于伤害值转护盾的特殊词条。"""

    def handle(
        self,
        *,
        effect_state: BattleSpecialEffectState,
        trigger_context: BattleSpecialEffectTriggerContext,
    ) -> None:
        owner = trigger_context.runtime_context.get_unit(effect_state.owner_unit_id)
        if not owner.is_alive:
            return
        resolved_value = max(0, 0 if trigger_context.resolved_value is None else trigger_context.resolved_value)
        if resolved_value <= 0:
            return
        if not self._should_trigger(effect_state=effect_state, trigger_context=trigger_context):
            return
        ratio_permille = _read_rate_payload(effect_state.payload.get("damage_ratio_permille"), default=0)
        if ratio_permille <= 0:
            return
        base_shield = max(1, resolved_value * ratio_permille // 1000)
        effect_state.consume_trigger()
        _apply_flat_shield(
            runtime_context=trigger_context.runtime_context,
            effect_state=effect_state,
            trigger_context=trigger_context,
            actor=owner,
            target=owner,
            base_shield=base_shield,
        )


class _AttackHealHandler(_BaseConfiguredEffectHandler):
    """处理基于攻力回复气血的特殊词条。"""

    def __init__(self, *, hp_threshold_permille: int | None = None, require_damage_action: bool = False) -> None:
        self._hp_threshold_permille = hp_threshold_permille
        self._require_damage_action = require_damage_action

    def handle(
        self,
        *,
        effect_state: BattleSpecialEffectState,
        trigger_context: BattleSpecialEffectTriggerContext,
    ) -> None:
        owner = trigger_context.runtime_context.get_unit(effect_state.owner_unit_id)
        if not owner.is_alive:
            return
        if self._hp_threshold_permille is not None:
            if owner.current_hp * 1000 > owner.base_snapshot.max_hp * self._hp_threshold_permille:
                return
        if self._require_damage_action and not _action_has_damage_resolved(
            runtime_context=trigger_context.runtime_context,
            owner_unit_id=effect_state.owner_unit_id,
            action_id=trigger_context.action_id,
        ):
            return
        if not self._should_trigger(effect_state=effect_state, trigger_context=trigger_context):
            return
        attack_ratio_permille = _read_rate_payload(effect_state.payload.get("attack_ratio_permille"), default=0)
        if attack_ratio_permille <= 0:
            return
        effect_state.consume_trigger()
        _settle_heal_with_action(
            runtime_context=trigger_context.runtime_context,
            effect_state=effect_state,
            trigger_context=trigger_context,
            actor=owner,
            target=owner,
            heal_scale_permille=attack_ratio_permille,
        )


def _build_default_handlers() -> dict[str, BattleSpecialEffectHandler]:
    """返回首发特殊词条默认处理器。"""
    return {
        "sunder_on_hit": _AttributeSuppressionHandler(target_mode="target"),
        "dot_on_hit": _DamageOverTimeHandler(target_mode="target"),
        "battle_start_barrier": _GuardBarrierHandler(),
        "barrier_on_damage_taken": _ResolvedDamageBarrierHandler(),
        "low_hp_regen": _AttackHealHandler(hp_threshold_permille=500),
        "heal_after_attack": _AttackHealHandler(require_damage_action=True),
        "round_end_barrier_if_empty": _GuardBarrierHandler(require_empty_shield=True),
        "counter_sunder": _AttributeSuppressionHandler(target_mode="source"),
        "damage_to_barrier": _ResolvedDamageBarrierHandler(),
        "counter_dot": _DamageOverTimeHandler(target_mode="source"),
    }


def _resolve_target_unit(
    *,
    trigger_context: BattleSpecialEffectTriggerContext,
    target_mode: str,
) -> BattleUnitState | None:
    runtime_context = trigger_context.runtime_context
    if target_mode == "target":
        unit_id = trigger_context.target_unit_id
    elif target_mode == "source":
        unit_id = trigger_context.actor_unit_id
    elif target_mode == "owner":
        unit_id = trigger_context.owner_unit_id
    else:
        raise ValueError(f"未支持的特殊效果目标模式：{target_mode}")
    if unit_id is None:
        return None
    return runtime_context.get_unit(unit_id)


def _resolve_effect_action_id(
    *,
    effect_state: BattleSpecialEffectState,
    trigger_context: BattleSpecialEffectTriggerContext,
) -> str:
    """返回特殊效果结算所使用的动作标识。"""
    action_id = trigger_context.action_id
    if action_id is not None and action_id.strip():
        return action_id
    return f"special_effect:{effect_state.effect_id}"


def _build_special_action(
    *,
    effect_state: BattleSpecialEffectState,
    trigger_context: BattleSpecialEffectTriggerContext,
    action_type: BattleActionType,
    shield_scale_permille: int = 0,
    heal_scale_permille: int = 0,
) -> CompiledBehaviorAction:
    """构造复用现有结算逻辑的临时动作描述。"""
    return CompiledBehaviorAction(
        action_id=_resolve_effect_action_id(effect_state=effect_state, trigger_context=trigger_context),
        name=effect_state.effect_name,
        source_order=1,
        execution_order=1,
        action_type=action_type,
        target_strategy=TargetSelectionStrategy.SELF,
        priority=0,
        weight_permille=0,
        cooldown_rounds=0,
        resource_cost=0,
        damage_scale_permille=0,
        shield_scale_permille=max(0, shield_scale_permille),
        heal_scale_permille=max(0, heal_scale_permille),
        control_chance_permille=0,
        max_triggers=1,
        labels=("special_effect", effect_state.effect_type),
    )


def _merge_status_effect(
    *,
    runtime_context: Any,
    target: BattleUnitState,
    status: BattleStatusEffect,
) -> None:
    """复用统一结算引擎的状态合并逻辑。"""
    from domain.battle.settlement import BattleSettlementEngine

    BattleSettlementEngine()._merge_status_effect(
        context=runtime_context,
        target=target,
        status=status,
    )


def _settle_heal_with_action(
    *,
    runtime_context: Any,
    effect_state: BattleSpecialEffectState,
    trigger_context: BattleSpecialEffectTriggerContext,
    actor: BattleUnitState,
    target: BattleUnitState,
    heal_scale_permille: int,
) -> None:
    """复用统一结算引擎的治疗语义。"""
    from domain.battle.settlement import BattleSettlementEngine

    action = _build_special_action(
        effect_state=effect_state,
        trigger_context=trigger_context,
        action_type=BattleActionType.HEAL_SKILL,
        heal_scale_permille=heal_scale_permille,
    )
    BattleSettlementEngine()._settle_heal_target(
        context=runtime_context,
        actor=actor,
        target=target,
        action=action,
    )


def _settle_shield_with_action(
    *,
    runtime_context: Any,
    effect_state: BattleSpecialEffectState,
    trigger_context: BattleSpecialEffectTriggerContext,
    actor: BattleUnitState,
    target: BattleUnitState,
    shield_scale_permille: int,
) -> None:
    """复用统一结算引擎的护盾语义。"""
    from domain.battle.settlement import BattleSettlementEngine

    action = _build_special_action(
        effect_state=effect_state,
        trigger_context=trigger_context,
        action_type=BattleActionType.SHIELD_SKILL,
        shield_scale_permille=shield_scale_permille,
    )
    BattleSettlementEngine()._settle_shield_target(
        context=runtime_context,
        actor=actor,
        target=target,
        action=action,
    )


def _apply_flat_shield(
    *,
    runtime_context: Any,
    effect_state: BattleSpecialEffectState,
    trigger_context: BattleSpecialEffectTriggerContext,
    actor: BattleUnitState,
    target: BattleUnitState,
    base_shield: int,
) -> None:
    """按现有护盾乘区语义直接施加护盾。"""
    if not target.is_alive:
        return
    final_shield = max(0, base_shield) * (1000 + actor.base_snapshot.shield_power_permille) // 1000
    if final_shield <= 0:
        return
    target.current_shield += final_shield
    runtime_context.add_stat(unit_id=target.unit_id, field_name="shield_gained", delta=final_shield)
    runtime_context.emit_event(
        phase=BattleEventPhase.SETTLEMENT,
        event_type="shield_applied",
        actor_unit_id=actor.unit_id,
        target_unit_id=target.unit_id,
        action_id=_resolve_effect_action_id(effect_state=effect_state, trigger_context=trigger_context),
        detail_items=(("gained_shield", final_shield), ("current_shield", target.current_shield)),
    )


def _action_has_damage_resolved(*, runtime_context: Any, owner_unit_id: str, action_id: str | None) -> bool:
    """判断给定动作是否已经完成过伤害结算。"""
    if action_id is None or not action_id.strip():
        return False
    for event in reversed(runtime_context.events):
        if event.action_id != action_id:
            continue
        if event.event_type != "damage_resolved":
            continue
        if event.actor_unit_id != owner_unit_id:
            continue
        return True
    return False


def _resolve_phase_for_hook(hook: BattleSpecialEffectHook):
    phase_map = {
        BattleSpecialEffectHook.BATTLE_START: BattleEventPhase.ROUND_START,
        BattleSpecialEffectHook.ROUND_START: BattleEventPhase.ROUND_START,
        BattleSpecialEffectHook.TURN_START: BattleEventPhase.TURN_START,
        BattleSpecialEffectHook.BEFORE_ACTION: BattleEventPhase.ACTION_DECISION,
        BattleSpecialEffectHook.AFTER_ACTION: BattleEventPhase.SETTLEMENT,
        BattleSpecialEffectHook.DAMAGE_RESOLVED: BattleEventPhase.SETTLEMENT,
        BattleSpecialEffectHook.DAMAGE_TAKEN: BattleEventPhase.SETTLEMENT,
        BattleSpecialEffectHook.TURN_END: BattleEventPhase.TURN_END,
        BattleSpecialEffectHook.ROUND_END: BattleEventPhase.ROUND_END,
        BattleSpecialEffectHook.BATTLE_END: BattleEventPhase.BATTLE_END,
    }
    return phase_map[hook]


def _require_non_blank(value: str, *, field_name: str) -> None:
    if not value or not value.strip():
        raise ValueError(f"{field_name} 不能为空")


def _coerce_non_negative_int(value: Any, *, default: int) -> int:
    if value is None:
        return default
    try:
        resolved = int(value)
    except (TypeError, ValueError):
        return default
    return max(0, resolved)


def _read_positive_int_payload(value: Any, *, default: int) -> int:
    """读取正整数载荷。"""
    return max(1, _coerce_non_negative_int(value, default=default))


def _read_rate_payload(value: Any, *, default: int) -> int:
    """读取千分比载荷。"""
    return max(0, min(1000, _coerce_non_negative_int(value, default=default)))


__all__ = [
    "BattleSpecialEffectHandler",
    "BattleSpecialEffectHook",
    "BattleSpecialEffectRegistry",
    "BattleSpecialEffectState",
    "BattleSpecialEffectTriggerContext",
]
