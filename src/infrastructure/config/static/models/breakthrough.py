"""突破秘境配置模型。"""

from __future__ import annotations

from collections import Counter

from pydantic import Field, field_validator, model_validator

from infrastructure.config.static.errors import StaticConfigIssueCollector
from infrastructure.config.static.models.common import (
    DisplayName,
    LAUNCH_REALM_TRANSITIONS,
    NonNegativeInt,
    OrderedConfigItem,
    PositiveDecimal,
    PositiveInt,
    ShortText,
    StableId,
    StaticConfigModel,
    VersionedSectionConfig,
)

LAUNCH_TRIAL_GROUP_IDS: tuple[str, ...] = ("entry_trials", "mind_palace", "void_gate")
LAUNCH_BREAKTHROUGH_MAPPING_IDS: tuple[str, ...] = (
    "mortal_to_qi_refining",
    "qi_refining_to_foundation",
    "foundation_to_core",
    "core_to_nascent_soul",
    "nascent_soul_to_deity_transformation",
    "deity_transformation_to_void_refinement",
    "void_refinement_to_body_integration",
    "body_integration_to_great_vehicle",
    "great_vehicle_to_tribulation",
)
ALLOWED_REPEAT_REWARD_IDS: tuple[str, ...] = (
    "spirit_stone",
    "enhancement_material",
    "reforge_material",
    "comprehension_material",
    "artifact_material",
)
EXPECTED_MATERIAL_TARGET_VICTORY_COUNTS: tuple[int, ...] = (1, 2, 2, 3, 3, 4, 4, 5, 6)
ALLOWED_BREAKTHROUGH_ENVIRONMENT_STAT_FIELDS: tuple[str, ...] = (
    "max_hp",
    "attack_power",
    "guard_power",
    "speed",
    "crit_rate_permille",
    "crit_damage_bonus_permille",
    "hit_rate_permille",
    "dodge_rate_permille",
    "control_bonus_permille",
    "control_resist_permille",
    "healing_power_permille",
    "shield_power_permille",
    "damage_bonus_permille",
    "damage_reduction_permille",
    "counter_rate_permille",
)
ALLOWED_ACTION_NUMERIC_FIELD_IDS: tuple[str, ...] = (
    "priority",
    "weight_permille",
    "resource_cost",
    "cooldown_rounds",
    "damage_scale_permille",
    "shield_scale_permille",
    "heal_scale_permille",
    "control_chance_permille",
)
ALLOWED_ACTION_THRESHOLD_FIELD_IDS: tuple[str, ...] = (
    "self_hp_below_permille",
    "target_hp_below_permille",
    "resource_above_permille",
    "enemy_count_at_least",
)
ALLOWED_REPEAT_REWARD_CYCLE_TYPES: tuple[str, ...] = ("daily",)
ALLOWED_REPEAT_REWARD_RESOURCE_KINDS: tuple[str, ...] = ("currency", "material")
ALLOWED_FIRST_CLEAR_REWARD_KINDS: tuple[str, ...] = ("qualification", "currency", "material")


class BreakthroughMaterialRequirement(StaticConfigModel):
    """单个突破材料要求。"""

    item_type: StableId
    item_id: StableId
    quantity: PositiveInt


class TrialGroupDefinition(OrderedConfigItem):
    """突破秘境组定义。"""

    group_id: StableId
    theme_summary: ShortText
    reward_focus_summary: ShortText


class EnvironmentStatModifierDefinition(StaticConfigModel):
    """环境规则对战斗单位静态属性的修正。"""

    stat_field: StableId
    delta: int | None = None
    multiplier_permille: PositiveInt | None = None

    @field_validator("stat_field")
    @classmethod
    def validate_stat_field(cls, value: str) -> str:
        """限制环境规则只能修正约定的战斗属性。"""
        if value not in ALLOWED_BREAKTHROUGH_ENVIRONMENT_STAT_FIELDS:
            raise ValueError("环境属性修正字段越界")
        return value

    @model_validator(mode="after")
    def validate_payload(self) -> "EnvironmentStatModifierDefinition":
        """禁止写入无实际效果或混合语义的属性修正。"""
        has_delta = self.delta is not None
        has_multiplier = self.multiplier_permille is not None
        if has_delta == has_multiplier:
            raise ValueError("环境属性修正必须且只能声明 delta 或 multiplier_permille 其中一种")
        if has_delta and self.delta == 0:
            raise ValueError("环境属性修正不能为 0")
        return self


class ActionPatchSelectorDefinition(StaticConfigModel):
    """环境模板补丁的动作选择器。"""

    action_ids: tuple[StableId, ...] = ()
    required_labels: tuple[StableId, ...] = ()

    @model_validator(mode="after")
    def validate_unique_values(self) -> "ActionPatchSelectorDefinition":
        """选择器内的动作与标签不得重复。"""
        if len(self.action_ids) != len(set(self.action_ids)):
            raise ValueError("action_ids 存在重复值")
        if len(self.required_labels) != len(set(self.required_labels)):
            raise ValueError("required_labels 存在重复值")
        return self


class TemplatePatchNumericBonusDefinition(StaticConfigModel):
    """环境模板补丁中的数值加成。"""

    field: StableId
    delta: int
    selector: ActionPatchSelectorDefinition = Field(default_factory=ActionPatchSelectorDefinition)

    @field_validator("field")
    @classmethod
    def validate_field(cls, value: str) -> str:
        """数值加成只能作用于允许的动作字段。"""
        if value not in ALLOWED_ACTION_NUMERIC_FIELD_IDS:
            raise ValueError("动作数值补丁字段越界")
        return value

    @model_validator(mode="after")
    def validate_delta(self) -> "TemplatePatchNumericBonusDefinition":
        """禁止声明无效的零值补丁。"""
        if self.delta == 0:
            raise ValueError("动作数值补丁增量不能为 0")
        return self


class TemplatePatchMultiplierDefinition(StaticConfigModel):
    """环境模板补丁中的乘区修正。"""

    field: StableId
    multiplier_permille: PositiveInt
    selector: ActionPatchSelectorDefinition = Field(default_factory=ActionPatchSelectorDefinition)

    @field_validator("field")
    @classmethod
    def validate_field(cls, value: str) -> str:
        """乘区修正只能作用于允许的动作字段。"""
        if value not in ALLOWED_ACTION_NUMERIC_FIELD_IDS:
            raise ValueError("动作乘区补丁字段越界")
        return value


class TemplatePatchThresholdShiftDefinition(StaticConfigModel):
    """环境模板补丁中的阈值平移。"""

    field: StableId
    delta: int
    selector: ActionPatchSelectorDefinition = Field(default_factory=ActionPatchSelectorDefinition)

    @field_validator("field")
    @classmethod
    def validate_field(cls, value: str) -> str:
        """阈值平移只能作用于允许的阈值字段。"""
        if value not in ALLOWED_ACTION_THRESHOLD_FIELD_IDS:
            raise ValueError("动作阈值补丁字段越界")
        return value

    @model_validator(mode="after")
    def validate_delta(self) -> "TemplatePatchThresholdShiftDefinition":
        """禁止声明无效的零值平移。"""
        if self.delta == 0:
            raise ValueError("动作阈值补丁增量不能为 0")
        return self


class TemplatePatchTriggerCapAdjustmentDefinition(StaticConfigModel):
    """环境模板补丁中的触发上限修正。"""

    delta: int
    selector: ActionPatchSelectorDefinition = Field(default_factory=ActionPatchSelectorDefinition)

    @model_validator(mode="after")
    def validate_delta(self) -> "TemplatePatchTriggerCapAdjustmentDefinition":
        """禁止声明无效的零值修正。"""
        if self.delta == 0:
            raise ValueError("触发上限补丁增量不能为 0")
        return self


class EnvironmentTemplatePatchDefinition(StaticConfigModel):
    """单条环境模板补丁定义。"""

    patch_id: StableId
    patch_name: ShortText
    numeric_bonuses: tuple[TemplatePatchNumericBonusDefinition, ...] = ()
    multipliers: tuple[TemplatePatchMultiplierDefinition, ...] = ()
    threshold_shifts: tuple[TemplatePatchThresholdShiftDefinition, ...] = ()
    trigger_cap_adjustments: tuple[TemplatePatchTriggerCapAdjustmentDefinition, ...] = ()

    @model_validator(mode="after")
    def validate_non_empty(self) -> "EnvironmentTemplatePatchDefinition":
        """环境模板补丁至少需要一类实际修正。"""
        if not any(
            (
                self.numeric_bonuses,
                self.multipliers,
                self.threshold_shifts,
                self.trigger_cap_adjustments,
            )
        ):
            raise ValueError("环境模板补丁至少需要包含 1 类修正")
        return self


class EnvironmentRuleDefinition(OrderedConfigItem):
    """可执行的突破秘境环境规则。"""

    rule_id: StableId
    summary: ShortText
    environment_tags: tuple[StableId, ...] = ()
    ally_stat_modifiers: tuple[EnvironmentStatModifierDefinition, ...] = ()
    enemy_stat_modifiers: tuple[EnvironmentStatModifierDefinition, ...] = ()
    ally_template_patches: tuple[EnvironmentTemplatePatchDefinition, ...] = ()
    enemy_template_patches: tuple[EnvironmentTemplatePatchDefinition, ...] = ()

    @model_validator(mode="after")
    def validate_rule_payload(self) -> "EnvironmentRuleDefinition":
        """环境规则必须包含可执行效果，标签不得重复。"""
        if len(self.environment_tags) != len(set(self.environment_tags)):
            raise ValueError("environment_tags 存在重复值")
        if not any(
            (
                self.ally_stat_modifiers,
                self.enemy_stat_modifiers,
                self.ally_template_patches,
                self.enemy_template_patches,
            )
        ):
            raise ValueError("环境规则至少需要声明 1 类可执行效果")
        return self


class RepeatRewardResourceDefinition(StaticConfigModel):
    """重复挑战资源包内的单条资源。"""

    resource_kind: StableId
    resource_id: StableId
    quantity: PositiveInt
    bound: bool = True

    @field_validator("resource_kind")
    @classmethod
    def validate_resource_kind(cls, value: str) -> str:
        """重复奖励只允许货币或材料。"""
        if value not in ALLOWED_REPEAT_REWARD_RESOURCE_KINDS:
            raise ValueError("重复奖励资源类型越界")
        return value

    @model_validator(mode="after")
    def validate_binding_boundary(self) -> "RepeatRewardResourceDefinition":
        """材料奖励必须显式标记为绑定。"""
        if self.resource_kind == "material" and not self.bound:
            raise ValueError("material 类型奖励必须固定声明 bound = true")
        return self


class RepeatRewardPoolDefinition(OrderedConfigItem):
    """重复挑战奖励池与软限制配置。"""

    pool_id: StableId
    reward_direction: StableId
    cycle_type: StableId
    high_yield_limit: PositiveInt
    high_yield_ratio: PositiveDecimal
    reduced_yield_ratio: PositiveDecimal
    resource_whitelist: tuple[StableId, ...]
    resources: tuple[RepeatRewardResourceDefinition, ...]

    @field_validator("reward_direction")
    @classmethod
    def validate_reward_direction(cls, value: str) -> str:
        """重复奖励方向必须落在首发白名单内。"""
        if value not in ALLOWED_REPEAT_REWARD_IDS:
            raise ValueError("重复挑战资源方向越界")
        return value

    @field_validator("cycle_type")
    @classmethod
    def validate_cycle_type(cls, value: str) -> str:
        """首发软限制周期只允许固定预设。"""
        if value not in ALLOWED_REPEAT_REWARD_CYCLE_TYPES:
            raise ValueError("重复奖励周期类型越界")
        return value

    @model_validator(mode="after")
    def validate_pool_boundary(self) -> "RepeatRewardPoolDefinition":
        """奖励池需要约束倍率边界、白名单与资源唯一性。"""
        if not self.resource_whitelist:
            raise ValueError("重复奖励池必须声明奖励白名单")
        if not self.resources:
            raise ValueError("重复奖励池至少需要声明 1 条资源")
        if len(self.resource_whitelist) != len(set(self.resource_whitelist)):
            raise ValueError("奖励白名单存在重复资源标识")
        resource_ids = tuple(resource.resource_id for resource in self.resources)
        if len(resource_ids) != len(set(resource_ids)):
            raise ValueError("重复奖励池存在重复资源标识")
        if not set(resource_ids).issubset(set(self.resource_whitelist)):
            raise ValueError("重复奖励池资源必须全部落在奖励白名单内")
        if self.high_yield_ratio < self.reduced_yield_ratio:
            raise ValueError("高收益倍率必须大于等于衰减倍率")
        return self


class FirstClearRewardDefinition(StaticConfigModel):
    """首通奖励定义。"""

    reward_kind: StableId
    resource_id: StableId | None = None
    quantity: PositiveInt | None = None
    bound: bool = True

    @field_validator("reward_kind")
    @classmethod
    def validate_reward_kind(cls, value: str) -> str:
        """首通奖励只允许资格、货币或材料。"""
        if value not in ALLOWED_FIRST_CLEAR_REWARD_KINDS:
            raise ValueError("首通奖励类型越界")
        return value

    @model_validator(mode="after")
    def validate_payload(self) -> "FirstClearRewardDefinition":
        """资格奖励与资源奖励的字段必须分开声明。"""
        if self.reward_kind == "qualification":
            if self.resource_id is not None or self.quantity is not None:
                raise ValueError("qualification 奖励不能声明 resource_id 或 quantity")
            return self

        if self.resource_id is None or self.quantity is None:
            raise ValueError("资源类首通奖励必须声明 resource_id 与 quantity")
        if self.reward_kind == "material" and not self.bound:
            raise ValueError("material 类型首通奖励必须固定声明 bound = true")
        return self


class BreakthroughTrialDefinition(OrderedConfigItem):
    """单个突破映射定义。"""

    mapping_id: StableId
    group_id: StableId
    from_realm_id: StableId
    to_realm_id: StableId
    boss_template_id: StableId
    boss_stage_id: StableId
    boss_scale_permille: PositiveInt
    environment_rule: ShortText
    environment_rule_id: StableId
    repeat_reward_direction: StableId
    repeat_reward_pool_id: StableId
    first_clear_grants_qualification: bool
    first_clear_rewards: tuple[FirstClearRewardDefinition, ...]
    required_comprehension_value: NonNegativeInt
    required_items: tuple[BreakthroughMaterialRequirement, ...]
    material_trial_name: DisplayName
    material_atmosphere_text: ShortText
    material_boss_scale_permille: PositiveInt
    material_target_victory_count: PositiveInt


class BreakthroughTrialConfig(VersionedSectionConfig):
    """突破秘境组、环境规则、奖励池与九次突破映射配置。"""

    trial_groups: tuple[TrialGroupDefinition, ...]
    environment_rules: tuple[EnvironmentRuleDefinition, ...]
    repeat_reward_pools: tuple[RepeatRewardPoolDefinition, ...]
    trials: tuple[BreakthroughTrialDefinition, ...]

    @property
    def ordered_trial_groups(self) -> tuple[TrialGroupDefinition, ...]:
        """按顺序返回全部秘境分组。"""
        return tuple(sorted(self.trial_groups, key=lambda item: item.order))

    @property
    def ordered_environment_rules(self) -> tuple[EnvironmentRuleDefinition, ...]:
        """按顺序返回全部环境规则。"""
        return tuple(sorted(self.environment_rules, key=lambda item: item.order))

    @property
    def ordered_repeat_reward_pools(self) -> tuple[RepeatRewardPoolDefinition, ...]:
        """按顺序返回全部重复奖励池。"""
        return tuple(sorted(self.repeat_reward_pools, key=lambda item: item.order))

    @property
    def ordered_trials(self) -> tuple[BreakthroughTrialDefinition, ...]:
        """按顺序返回全部突破映射。"""
        return tuple(sorted(self.trials, key=lambda item: item.order))

    def get_environment_rule(self, rule_id: str) -> EnvironmentRuleDefinition | None:
        """按标识读取环境规则。"""
        for rule in self.environment_rules:
            if rule.rule_id == rule_id:
                return rule
        return None

    def get_repeat_reward_pool(self, pool_id: str) -> RepeatRewardPoolDefinition | None:
        """按标识读取重复奖励池。"""
        for pool in self.repeat_reward_pools:
            if pool.pool_id == pool_id:
                return pool
        return None

    def get_trial(self, mapping_id: str) -> BreakthroughTrialDefinition | None:
        """按映射标识读取突破试炼。"""
        for trial in self.trials:
            if trial.mapping_id == mapping_id:
                return trial
        return None

    def get_trial_by_from_realm_id(self, realm_id: str) -> BreakthroughTrialDefinition | None:
        """按当前大境界标识读取下一次突破映射。"""
        for trial in self.ordered_trials:
            if trial.from_realm_id == realm_id:
                return trial
        return None

    def collect_issues(self, *, filename: str, collector: StaticConfigIssueCollector) -> None:
        """收集当前配置节的结构与边界错误。"""
        ordered_groups = self.ordered_trial_groups
        ordered_environment_rules = self.ordered_environment_rules
        ordered_reward_pools = self.ordered_repeat_reward_pools
        ordered_trials = self.ordered_trials
        group_ids = tuple(group.group_id for group in ordered_groups)
        mapping_ids = tuple(trial.mapping_id for trial in ordered_trials)
        known_group_ids = {group.group_id for group in self.trial_groups}
        known_environment_rule_ids = {rule.rule_id for rule in self.environment_rules}
        known_reward_pool_ids = {pool.pool_id for pool in self.repeat_reward_pools}
        expected_transitions = {
            mapping_id: transition
            for mapping_id, transition in zip(
                LAUNCH_BREAKTHROUGH_MAPPING_IDS,
                LAUNCH_REALM_TRANSITIONS,
                strict=True,
            )
        }
        expected_material_target_wins = {
            mapping_id: target_wins
            for mapping_id, target_wins in zip(
                LAUNCH_BREAKTHROUGH_MAPPING_IDS,
                EXPECTED_MATERIAL_TARGET_VICTORY_COUNTS,
                strict=True,
            )
        }

        group_order_counter = Counter(group.order for group in self.trial_groups)
        group_id_counter = Counter(group.group_id for group in self.trial_groups)
        environment_rule_order_counter = Counter(rule.order for rule in self.environment_rules)
        environment_rule_id_counter = Counter(rule.rule_id for rule in self.environment_rules)
        reward_pool_order_counter = Counter(pool.order for pool in self.repeat_reward_pools)
        reward_pool_id_counter = Counter(pool.pool_id for pool in self.repeat_reward_pools)
        trial_id_counter = Counter(trial.mapping_id for trial in self.trials)

        if group_ids != LAUNCH_TRIAL_GROUP_IDS:
            collector.add(
                filename=filename,
                config_path="trial_groups",
                identifier="trial_group_sequence",
                reason="突破秘境组必须固定为 entry_trials、mind_palace、void_gate",
            )
        if mapping_ids != LAUNCH_BREAKTHROUGH_MAPPING_IDS:
            collector.add(
                filename=filename,
                config_path="trials",
                identifier="mapping_sequence",
                reason="九次突破映射必须完整覆盖凡人到渡劫前全部大境界突破",
            )

        for group in self.trial_groups:
            if group_order_counter[group.order] > 1:
                collector.add(
                    filename=filename,
                    config_path="trial_groups[].order",
                    identifier=group.group_id,
                    reason=f"突破秘境组顺序值 {group.order} 重复",
                )
            if group_id_counter[group.group_id] > 1:
                collector.add(
                    filename=filename,
                    config_path="trial_groups[].group_id",
                    identifier=group.group_id,
                    reason="突破秘境组标识重复",
                )

        for rule in self.environment_rules:
            if environment_rule_order_counter[rule.order] > 1:
                collector.add(
                    filename=filename,
                    config_path="environment_rules[].order",
                    identifier=rule.rule_id,
                    reason=f"环境规则顺序值 {rule.order} 重复",
                )
            if environment_rule_id_counter[rule.rule_id] > 1:
                collector.add(
                    filename=filename,
                    config_path="environment_rules[].rule_id",
                    identifier=rule.rule_id,
                    reason="环境规则标识重复",
                )

        for pool in self.repeat_reward_pools:
            if reward_pool_order_counter[pool.order] > 1:
                collector.add(
                    filename=filename,
                    config_path="repeat_reward_pools[].order",
                    identifier=pool.pool_id,
                    reason=f"重复奖励池顺序值 {pool.order} 重复",
                )
            if reward_pool_id_counter[pool.pool_id] > 1:
                collector.add(
                    filename=filename,
                    config_path="repeat_reward_pools[].pool_id",
                    identifier=pool.pool_id,
                    reason="重复奖励池标识重复",
                )

        for trial in ordered_trials:
            if trial_id_counter[trial.mapping_id] > 1:
                collector.add(
                    filename=filename,
                    config_path="trials[].mapping_id",
                    identifier=trial.mapping_id,
                    reason="突破映射标识重复",
                )
            if trial.group_id not in known_group_ids:
                collector.add(
                    filename=filename,
                    config_path="trials[].group_id",
                    identifier=trial.mapping_id,
                    reason=f"引用了未定义秘境组 {trial.group_id}",
                )
            if trial.environment_rule_id not in known_environment_rule_ids:
                collector.add(
                    filename=filename,
                    config_path="trials[].environment_rule_id",
                    identifier=trial.mapping_id,
                    reason=f"引用了未定义环境规则 {trial.environment_rule_id}",
                )
            if trial.repeat_reward_pool_id not in known_reward_pool_ids:
                collector.add(
                    filename=filename,
                    config_path="trials[].repeat_reward_pool_id",
                    identifier=trial.mapping_id,
                    reason=f"引用了未定义重复奖励池 {trial.repeat_reward_pool_id}",
                )

            reward_pool = self.get_repeat_reward_pool(trial.repeat_reward_pool_id)
            if reward_pool is not None and reward_pool.reward_direction != trial.repeat_reward_direction:
                collector.add(
                    filename=filename,
                    config_path="trials[].repeat_reward_direction",
                    identifier=trial.mapping_id,
                    reason="关卡重复奖励方向必须与绑定奖励池完全一致",
                )

            if trial.repeat_reward_direction not in ALLOWED_REPEAT_REWARD_IDS:
                collector.add(
                    filename=filename,
                    config_path="trials[].repeat_reward_direction",
                    identifier=trial.mapping_id,
                    reason="重复挑战资源方向越界，只允许基础资源补口类型",
                )
            if not trial.first_clear_grants_qualification:
                collector.add(
                    filename=filename,
                    config_path="trials[].first_clear_grants_qualification",
                    identifier=trial.mapping_id,
                    reason="首通突破秘境必须固定发放突破资格",
                )

            qualification_reward_count = sum(
                1
                for reward in trial.first_clear_rewards
                if reward.reward_kind == "qualification"
            )
            if qualification_reward_count == 0:
                collector.add(
                    filename=filename,
                    config_path="trials[].first_clear_rewards",
                    identifier=trial.mapping_id,
                    reason="首通奖励必须包含 qualification 项",
                )
            elif qualification_reward_count > 1:
                collector.add(
                    filename=filename,
                    config_path="trials[].first_clear_rewards",
                    identifier=trial.mapping_id,
                    reason="首通奖励中的 qualification 项只能声明一次",
                )

            first_clear_reward_counter = Counter(
                (reward.reward_kind, reward.resource_id or reward.reward_kind)
                for reward in trial.first_clear_rewards
            )
            for reward_key, count in first_clear_reward_counter.items():
                if count > 1:
                    collector.add(
                        filename=filename,
                        config_path="trials[].first_clear_rewards",
                        identifier=trial.mapping_id,
                        reason=f"首通奖励 {reward_key[0]}:{reward_key[1]} 重复声明",
                    )

            if trial.material_boss_scale_permille >= trial.boss_scale_permille:
                collector.add(
                    filename=filename,
                    config_path="trials[].material_boss_scale_permille",
                    identifier=trial.mapping_id,
                    reason="材料秘境基础敌人倍率必须严格低于突破秘境倍率",
                )
            expected_target_wins = expected_material_target_wins.get(trial.mapping_id)
            if expected_target_wins is not None and trial.material_target_victory_count != expected_target_wins:
                collector.add(
                    filename=filename,
                    config_path="trials[].material_target_victory_count",
                    identifier=trial.mapping_id,
                    reason=(
                        "材料秘境目标胜利次数必须符合首发曲线 "
                        "1,2,2,3,3,4,4,5,6"
                    ),
                )

            expected_transition = expected_transitions.get(trial.mapping_id)
            if expected_transition is not None and (
                trial.from_realm_id != expected_transition[0]
                or trial.to_realm_id != expected_transition[1]
            ):
                collector.add(
                    filename=filename,
                    config_path="trials[].from_realm_id",
                    identifier=trial.mapping_id,
                    reason=(
                        "突破映射的大境界前后关系不合法，"
                        f"期望 {expected_transition[0]} -> {expected_transition[1]}"
                    ),
                )

            material_key_counter = Counter(
                (item.item_type, item.item_id)
                for item in trial.required_items
            )
            for (item_type, item_id), count in material_key_counter.items():
                if count > 1:
                    collector.add(
                        filename=filename,
                        config_path="trials[].required_items",
                        identifier=trial.mapping_id,
                        reason=f"突破材料 {item_type}:{item_id} 重复声明",
                    )


__all__ = [
    "ActionPatchSelectorDefinition",
    "BreakthroughMaterialRequirement",
    "BreakthroughTrialConfig",
    "BreakthroughTrialDefinition",
    "EnvironmentRuleDefinition",
    "EnvironmentStatModifierDefinition",
    "EnvironmentTemplatePatchDefinition",
    "FirstClearRewardDefinition",
    "RepeatRewardPoolDefinition",
    "RepeatRewardResourceDefinition",
    "TemplatePatchMultiplierDefinition",
    "TemplatePatchNumericBonusDefinition",
    "TemplatePatchThresholdShiftDefinition",
    "TemplatePatchTriggerCapAdjustmentDefinition",
    "TrialGroupDefinition",
]
