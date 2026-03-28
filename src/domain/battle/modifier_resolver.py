"""辅助功法参数补丁合并器。"""

from __future__ import annotations

from collections.abc import Sequence

from domain.battle.models import (
    ActionNumericField,
    ActionThresholdField,
    AuxiliarySkillParameterPatch,
    BehaviorActionTemplate,
    BehaviorTemplate,
    CompiledBehaviorAction,
    CompiledBehaviorTemplate,
)

_NUMERIC_FIELD_BOUNDS: dict[ActionNumericField, tuple[int, int]] = {
    ActionNumericField.PRIORITY: (0, 999),
    ActionNumericField.WEIGHT_PERMILLE: (0, 5000),
    ActionNumericField.RESOURCE_COST: (0, 10000),
    ActionNumericField.COOLDOWN_ROUNDS: (0, 30),
    ActionNumericField.DAMAGE_SCALE_PERMILLE: (0, 5000),
    ActionNumericField.SHIELD_SCALE_PERMILLE: (0, 5000),
    ActionNumericField.HEAL_SCALE_PERMILLE: (0, 5000),
    ActionNumericField.CONTROL_CHANCE_PERMILLE: (0, 1000),
}
_THRESHOLD_FIELD_BOUNDS: dict[ActionThresholdField, tuple[int, int]] = {
    ActionThresholdField.SELF_HP_BELOW_PERMILLE: (0, 1000),
    ActionThresholdField.TARGET_HP_BELOW_PERMILLE: (0, 1000),
    ActionThresholdField.RESOURCE_ABOVE_PERMILLE: (0, 1000),
    ActionThresholdField.ENEMY_COUNT_AT_LEAST: (1, 16),
}
_TRIGGER_CAP_BOUNDS = (1, 10)


class AuxiliaryModifierResolver:
    """把辅助功法补丁合并到主修行为模板。"""

    def resolve(
        self,
        *,
        base_template: BehaviorTemplate,
        patches: Sequence[AuxiliarySkillParameterPatch] = (),
    ) -> CompiledBehaviorTemplate:
        """返回应用补丁后的运行期模板。

        合并顺序固定为：
        1. 数值加成
        2. 数值乘区
        3. 阈值平移
        4. 触发上限修正

        标签、动作类别、目标策略与模板标签全部沿用主模板，不允许通过补丁改写。
        阈值平移只会修正模板原本已声明的阈值，不会为无阈值动作新增分支。
        """
        ordered_base_actions = tuple(sorted(base_template.actions, key=lambda item: item.order))
        compiled_actions = tuple(
            self._compile_action(action=action, patches=patches)
            for action in ordered_base_actions
        )
        execution_order_map = self._build_execution_order_map(compiled_actions)
        ordered_actions = tuple(
            sorted(
                (
                    self._replace_execution_order(action=action, execution_order=execution_order_map[action.action_id])
                    for action in compiled_actions
                ),
                key=lambda item: item.execution_order,
            )
        )
        applied_patch_ids = _collect_patch_ids(patches)
        return CompiledBehaviorTemplate(
            template_id=base_template.template_id,
            path_id=base_template.path_id,
            axis_id=base_template.axis_id,
            name=base_template.name,
            default_target_strategy=base_template.default_target_strategy,
            resource_policy=base_template.resource_policy,
            template_tags=base_template.template_tags,
            actions=ordered_actions,
            applied_patch_ids=applied_patch_ids,
        )

    def _compile_action(
        self,
        *,
        action: BehaviorActionTemplate,
        patches: Sequence[AuxiliarySkillParameterPatch],
    ) -> CompiledBehaviorAction:
        numeric_values: dict[ActionNumericField, int] = {
            ActionNumericField.PRIORITY: action.priority,
            ActionNumericField.WEIGHT_PERMILLE: action.weight_permille,
            ActionNumericField.RESOURCE_COST: action.resource_cost,
            ActionNumericField.COOLDOWN_ROUNDS: action.cooldown_rounds,
            ActionNumericField.DAMAGE_SCALE_PERMILLE: action.damage_scale_permille,
            ActionNumericField.SHIELD_SCALE_PERMILLE: action.shield_scale_permille,
            ActionNumericField.HEAL_SCALE_PERMILLE: action.heal_scale_permille,
            ActionNumericField.CONTROL_CHANCE_PERMILLE: action.control_chance_permille,
        }
        threshold_values: dict[ActionThresholdField, int | None] = {
            ActionThresholdField.SELF_HP_BELOW_PERMILLE: action.self_hp_below_permille,
            ActionThresholdField.TARGET_HP_BELOW_PERMILLE: action.target_hp_below_permille,
            ActionThresholdField.RESOURCE_ABOVE_PERMILLE: action.resource_above_permille,
            ActionThresholdField.ENEMY_COUNT_AT_LEAST: action.enemy_count_at_least,
        }
        trigger_cap = action.max_triggers

        for patch in patches:
            for numeric_bonus in patch.numeric_bonuses:
                if numeric_bonus.selector.matches(action_id=action.action_id, labels=action.labels):
                    numeric_values[numeric_bonus.field] += numeric_bonus.delta

        for patch in patches:
            for multiplier in patch.multipliers:
                if multiplier.selector.matches(action_id=action.action_id, labels=action.labels):
                    numeric_values[multiplier.field] = _scale_value(
                        numeric_values[multiplier.field],
                        multiplier.multiplier_permille,
                    )

        for patch in patches:
            for threshold_shift in patch.threshold_shifts:
                if not threshold_shift.selector.matches(action_id=action.action_id, labels=action.labels):
                    continue
                current_value = threshold_values[threshold_shift.field]
                if current_value is None:
                    continue
                threshold_values[threshold_shift.field] = current_value + threshold_shift.delta

        for patch in patches:
            for adjustment in patch.trigger_cap_adjustments:
                if adjustment.selector.matches(action_id=action.action_id, labels=action.labels):
                    trigger_cap += adjustment.delta

        clamped_numeric_values = {
            field: _clamp(field_value, *_NUMERIC_FIELD_BOUNDS[field])
            for field, field_value in numeric_values.items()
        }
        clamped_threshold_values = {
            field: _clamp_optional(field_value, *_THRESHOLD_FIELD_BOUNDS[field])
            for field, field_value in threshold_values.items()
        }
        normalized_trigger_cap = _clamp(trigger_cap, *_TRIGGER_CAP_BOUNDS)

        return CompiledBehaviorAction(
            action_id=action.action_id,
            name=action.name,
            source_order=action.order,
            execution_order=action.order,
            action_type=action.action_type,
            target_strategy=action.target_strategy,
            priority=clamped_numeric_values[ActionNumericField.PRIORITY],
            weight_permille=clamped_numeric_values[ActionNumericField.WEIGHT_PERMILLE],
            cooldown_rounds=clamped_numeric_values[ActionNumericField.COOLDOWN_ROUNDS],
            resource_cost=clamped_numeric_values[ActionNumericField.RESOURCE_COST],
            damage_scale_permille=clamped_numeric_values[ActionNumericField.DAMAGE_SCALE_PERMILLE],
            shield_scale_permille=clamped_numeric_values[ActionNumericField.SHIELD_SCALE_PERMILLE],
            heal_scale_permille=clamped_numeric_values[ActionNumericField.HEAL_SCALE_PERMILLE],
            control_chance_permille=clamped_numeric_values[ActionNumericField.CONTROL_CHANCE_PERMILLE],
            max_triggers=normalized_trigger_cap,
            labels=action.labels,
            self_hp_below_permille=clamped_threshold_values[ActionThresholdField.SELF_HP_BELOW_PERMILLE],
            target_hp_below_permille=clamped_threshold_values[ActionThresholdField.TARGET_HP_BELOW_PERMILLE],
            resource_above_permille=clamped_threshold_values[ActionThresholdField.RESOURCE_ABOVE_PERMILLE],
            enemy_count_at_least=clamped_threshold_values[ActionThresholdField.ENEMY_COUNT_AT_LEAST],
        )

    @staticmethod
    def _build_execution_order_map(
        compiled_actions: Sequence[CompiledBehaviorAction],
    ) -> dict[str, int]:
        """按优先级生成稳定执行顺序。"""
        ordered_actions = tuple(
            sorted(
                compiled_actions,
                key=lambda item: (-item.priority, item.source_order, item.action_id),
            )
        )
        return {
            action.action_id: execution_order
            for execution_order, action in enumerate(ordered_actions, start=1)
        }

    @staticmethod
    def _replace_execution_order(
        *,
        action: CompiledBehaviorAction,
        execution_order: int,
    ) -> CompiledBehaviorAction:
        """生成写入新执行顺序后的动作对象。"""
        return CompiledBehaviorAction(
            action_id=action.action_id,
            name=action.name,
            source_order=action.source_order,
            execution_order=execution_order,
            action_type=action.action_type,
            target_strategy=action.target_strategy,
            priority=action.priority,
            weight_permille=action.weight_permille,
            cooldown_rounds=action.cooldown_rounds,
            resource_cost=action.resource_cost,
            damage_scale_permille=action.damage_scale_permille,
            shield_scale_permille=action.shield_scale_permille,
            heal_scale_permille=action.heal_scale_permille,
            control_chance_permille=action.control_chance_permille,
            max_triggers=action.max_triggers,
            labels=action.labels,
            self_hp_below_permille=action.self_hp_below_permille,
            target_hp_below_permille=action.target_hp_below_permille,
            resource_above_permille=action.resource_above_permille,
            enemy_count_at_least=action.enemy_count_at_least,
        )


def _scale_value(value: int, multiplier_permille: int) -> int:
    """按千分比乘区缩放整数值。"""
    return (value * multiplier_permille) // 1000


def _clamp(value: int, minimum: int, maximum: int) -> int:
    """把整数裁剪到固定区间。"""
    return max(minimum, min(maximum, value))


def _clamp_optional(value: int | None, minimum: int, maximum: int) -> int | None:
    """把可选整数裁剪到固定区间。"""
    if value is None:
        return None
    return _clamp(value, minimum, maximum)


def _collect_patch_ids(patches: Sequence[AuxiliarySkillParameterPatch]) -> tuple[str, ...]:
    """按传入顺序收集唯一补丁标识。"""
    ordered_patch_ids: list[str] = []
    seen_patch_ids: set[str] = set()
    for patch in patches:
        if patch.patch_id in seen_patch_ids:
            continue
        seen_patch_ids.add(patch.patch_id)
        ordered_patch_ids.append(patch.patch_id)
    return tuple(ordered_patch_ids)


__all__ = ["AuxiliaryModifierResolver"]
