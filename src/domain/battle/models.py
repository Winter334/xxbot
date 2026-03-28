"""自动战斗核心领域模型。"""

from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from domain.battle.special_effects import BattleSpecialEffectState

_DEFAULT_ROUND_LIMIT = 30
_STANDARD_PERMILLE_MAX = 1000
_EXTENDED_PERMILLE_MAX = 5000


class BattleSide(StrEnum):
    """战斗单位所属阵营。"""

    ALLY = "ally"
    ENEMY = "enemy"


class BattleActionType(StrEnum):
    """行为模板内声明的动作类别。"""

    BASIC_ATTACK = "basic_attack"
    BURST_ATTACK = "burst_attack"
    FINISHER = "finisher"
    COMBO_ATTACK = "combo_attack"
    COUNTER_ATTACK = "counter_attack"
    SHIELD_SKILL = "shield_skill"
    HEAL_SKILL = "heal_skill"
    AREA_SPELL = "area_spell"
    CONTROL_SPELL = "control_spell"
    DEBUFF_SPELL = "debuff_spell"


class TargetSelectionStrategy(StrEnum):
    """动作目标选择策略。"""

    CURRENT_TARGET = "current_target"
    LOWEST_HP_PERCENT = "lowest_hp_percent"
    HIGHEST_ATTACK = "highest_attack"
    HIGHEST_GUARD = "highest_guard"
    ALL_ENEMIES = "all_enemies"
    ALLY_LOWEST_HP_PERCENT = "ally_lowest_hp_percent"
    SELF = "self"


class BattleResourcePolicy(StrEnum):
    """主模板的资源使用倾向。"""

    CONSERVE = "conserve"
    STEADY = "steady"
    BURST = "burst"


class BattleOutcome(StrEnum):
    """战斗结束结果。"""

    ALLY_VICTORY = "ally_victory"
    ENEMY_VICTORY = "enemy_victory"
    DRAW = "draw"


class BattleEventPhase(StrEnum):
    """结构化事件所属阶段。"""

    ROUND_START = "round_start"
    ACTION_QUEUE = "action_queue"
    TURN_START = "turn_start"
    ACTION_DECISION = "action_decision"
    SETTLEMENT = "settlement"
    REACTION = "reaction"
    TURN_END = "turn_end"
    ROUND_END = "round_end"
    BATTLE_END = "battle_end"


class BattleStatusCategory(StrEnum):
    """状态效果类别。"""

    HARD_CONTROL = "hard_control"
    DAMAGE_OVER_TIME = "damage_over_time"
    ATTRIBUTE_SUPPRESSION = "attribute_suppression"


class BattleReactionType(StrEnum):
    """即时反应类别。"""

    PURSUIT = "pursuit"
    COUNTER = "counter"


class ActionNumericField(StrEnum):
    """允许辅助功法修正的数值字段。"""

    PRIORITY = "priority"
    WEIGHT_PERMILLE = "weight_permille"
    RESOURCE_COST = "resource_cost"
    COOLDOWN_ROUNDS = "cooldown_rounds"
    DAMAGE_SCALE_PERMILLE = "damage_scale_permille"
    SHIELD_SCALE_PERMILLE = "shield_scale_permille"
    HEAL_SCALE_PERMILLE = "heal_scale_permille"
    CONTROL_CHANCE_PERMILLE = "control_chance_permille"


class ActionThresholdField(StrEnum):
    """允许辅助功法平移的阈值字段。"""

    SELF_HP_BELOW_PERMILLE = "self_hp_below_permille"
    TARGET_HP_BELOW_PERMILLE = "target_hp_below_permille"
    RESOURCE_ABOVE_PERMILLE = "resource_above_permille"
    ENEMY_COUNT_AT_LEAST = "enemy_count_at_least"


@dataclass(frozen=True, slots=True)
class BattleUnitSnapshot:
    """参与战斗的单体快照。"""

    unit_id: str
    unit_name: str
    side: BattleSide
    behavior_template_id: str
    realm_id: str
    stage_id: str
    max_hp: int
    current_hp: int
    current_shield: int
    max_resource: int
    current_resource: int
    attack_power: int
    guard_power: int
    speed: int
    crit_rate_permille: int = 0
    crit_damage_bonus_permille: int = 0
    hit_rate_permille: int = 0
    dodge_rate_permille: int = 0
    control_bonus_permille: int = 0
    control_resist_permille: int = 0
    healing_power_permille: int = 0
    shield_power_permille: int = 0
    damage_bonus_permille: int = 0
    damage_reduction_permille: int = 0
    counter_rate_permille: int = 0
    special_effect_payloads: tuple[dict[str, object], ...] = ()

    def __post_init__(self) -> None:
        _require_non_blank(self.unit_id, field_name="unit_id")
        _require_non_blank(self.unit_name, field_name="unit_name")
        _require_non_blank(self.behavior_template_id, field_name="behavior_template_id")
        _require_non_blank(self.realm_id, field_name="realm_id")
        _require_non_blank(self.stage_id, field_name="stage_id")
        _require_positive_int(self.max_hp, field_name="max_hp")
        _require_range(self.current_hp, field_name="current_hp", minimum=0, maximum=self.max_hp)
        _require_non_negative_int(self.current_shield, field_name="current_shield")
        _require_non_negative_int(self.max_resource, field_name="max_resource")
        _require_range(
            self.current_resource,
            field_name="current_resource",
            minimum=0,
            maximum=self.max_resource,
        )
        _require_positive_int(self.attack_power, field_name="attack_power")
        _require_non_negative_int(self.guard_power, field_name="guard_power")
        _require_positive_int(self.speed, field_name="speed")
        _require_rate(self.crit_rate_permille, field_name="crit_rate_permille")
        _require_rate(
            self.crit_damage_bonus_permille,
            field_name="crit_damage_bonus_permille",
            maximum=_EXTENDED_PERMILLE_MAX,
        )
        _require_rate(self.hit_rate_permille, field_name="hit_rate_permille")
        _require_rate(self.dodge_rate_permille, field_name="dodge_rate_permille")
        _require_rate(self.control_bonus_permille, field_name="control_bonus_permille")
        _require_rate(self.control_resist_permille, field_name="control_resist_permille")
        _require_rate(self.healing_power_permille, field_name="healing_power_permille")
        _require_rate(self.shield_power_permille, field_name="shield_power_permille")
        _require_rate(
            self.damage_bonus_permille,
            field_name="damage_bonus_permille",
            maximum=_EXTENDED_PERMILLE_MAX,
        )
        _require_rate(
            self.damage_reduction_permille,
            field_name="damage_reduction_permille",
            maximum=_STANDARD_PERMILLE_MAX,
        )
        _require_rate(self.counter_rate_permille, field_name="counter_rate_permille")


@dataclass(frozen=True, slots=True)
class BattleSnapshot:
    """单场战斗的输入快照。"""

    seed: int
    allies: tuple[BattleUnitSnapshot, ...]
    enemies: tuple[BattleUnitSnapshot, ...]
    round_limit: int = _DEFAULT_ROUND_LIMIT
    environment_tags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.allies:
            raise ValueError("战斗快照至少需要 1 个友方单位")
        if not self.enemies:
            raise ValueError("战斗快照至少需要 1 个敌方单位")
        _require_positive_int(self.round_limit, field_name="round_limit")
        ally_ids = tuple(unit.unit_id for unit in self.allies)
        enemy_ids = tuple(unit.unit_id for unit in self.enemies)
        _require_unique_values(ally_ids, field_name="allies.unit_id")
        _require_unique_values(enemy_ids, field_name="enemies.unit_id")
        duplicated_ids = set(ally_ids).intersection(enemy_ids)
        if duplicated_ids:
            duplicated_text = ", ".join(sorted(duplicated_ids))
            raise ValueError(f"战斗快照中的单位标识跨阵营重复：{duplicated_text}")
        for unit in self.allies:
            if unit.side is not BattleSide.ALLY:
                raise ValueError(f"友方快照存在错误阵营：{unit.unit_id}")
        for unit in self.enemies:
            if unit.side is not BattleSide.ENEMY:
                raise ValueError(f"敌方快照存在错误阵营：{unit.unit_id}")
        for tag in self.environment_tags:
            _require_non_blank(tag, field_name="environment_tags")

    @property
    def all_units(self) -> tuple[BattleUnitSnapshot, ...]:
        """按友方在前、敌方在后的顺序返回完整单位序列。"""
        return self.allies + self.enemies


@dataclass(frozen=True, slots=True)
class BehaviorActionTemplate:
    """主修路径定义的单个动作模板。"""

    action_id: str
    name: str
    order: int
    action_type: BattleActionType
    target_strategy: TargetSelectionStrategy
    priority: int
    weight_permille: int
    cooldown_rounds: int
    resource_cost: int
    damage_scale_permille: int
    shield_scale_permille: int
    heal_scale_permille: int
    control_chance_permille: int
    max_triggers: int
    labels: tuple[str, ...]
    self_hp_below_permille: int | None = None
    target_hp_below_permille: int | None = None
    resource_above_permille: int | None = None
    enemy_count_at_least: int | None = None

    def __post_init__(self) -> None:
        _validate_action_fields(
            action_id=self.action_id,
            name=self.name,
            order=self.order,
            priority=self.priority,
            weight_permille=self.weight_permille,
            cooldown_rounds=self.cooldown_rounds,
            resource_cost=self.resource_cost,
            damage_scale_permille=self.damage_scale_permille,
            shield_scale_permille=self.shield_scale_permille,
            heal_scale_permille=self.heal_scale_permille,
            control_chance_permille=self.control_chance_permille,
            max_triggers=self.max_triggers,
            labels=self.labels,
            self_hp_below_permille=self.self_hp_below_permille,
            target_hp_below_permille=self.target_hp_below_permille,
            resource_above_permille=self.resource_above_permille,
            enemy_count_at_least=self.enemy_count_at_least,
        )


@dataclass(frozen=True, slots=True)
class BehaviorTemplate:
    """主修路径对应的基础行为模板。"""

    template_id: str
    path_id: str
    axis_id: str
    name: str
    default_target_strategy: TargetSelectionStrategy
    resource_policy: BattleResourcePolicy
    template_tags: tuple[str, ...]
    actions: tuple[BehaviorActionTemplate, ...]

    def __post_init__(self) -> None:
        _require_non_blank(self.template_id, field_name="template_id")
        _require_non_blank(self.path_id, field_name="path_id")
        _require_non_blank(self.axis_id, field_name="axis_id")
        _require_non_blank(self.name, field_name="name")
        if not self.template_tags:
            raise ValueError("行为模板至少需要 1 个模板标签")
        if not self.actions:
            raise ValueError("行为模板至少需要 1 个动作")
        for tag in self.template_tags:
            _require_non_blank(tag, field_name="template_tags")
        _require_unique_values((action.action_id for action in self.actions), field_name="actions.action_id")
        _require_unique_values((action.order for action in self.actions), field_name="actions.order")


@dataclass(frozen=True, slots=True)
class CompiledBehaviorAction:
    """应用辅助补丁后的运行期动作模板。"""

    action_id: str
    name: str
    source_order: int
    execution_order: int
    action_type: BattleActionType
    target_strategy: TargetSelectionStrategy
    priority: int
    weight_permille: int
    cooldown_rounds: int
    resource_cost: int
    damage_scale_permille: int
    shield_scale_permille: int
    heal_scale_permille: int
    control_chance_permille: int
    max_triggers: int
    labels: tuple[str, ...]
    self_hp_below_permille: int | None = None
    target_hp_below_permille: int | None = None
    resource_above_permille: int | None = None
    enemy_count_at_least: int | None = None

    def __post_init__(self) -> None:
        _validate_action_fields(
            action_id=self.action_id,
            name=self.name,
            order=self.source_order,
            priority=self.priority,
            weight_permille=self.weight_permille,
            cooldown_rounds=self.cooldown_rounds,
            resource_cost=self.resource_cost,
            damage_scale_permille=self.damage_scale_permille,
            shield_scale_permille=self.shield_scale_permille,
            heal_scale_permille=self.heal_scale_permille,
            control_chance_permille=self.control_chance_permille,
            max_triggers=self.max_triggers,
            labels=self.labels,
            self_hp_below_permille=self.self_hp_below_permille,
            target_hp_below_permille=self.target_hp_below_permille,
            resource_above_permille=self.resource_above_permille,
            enemy_count_at_least=self.enemy_count_at_least,
        )
        _require_positive_int(self.execution_order, field_name="execution_order")


@dataclass(frozen=True, slots=True)
class CompiledBehaviorTemplate:
    """可直接供回合引擎读取的运行期行为模板。"""

    template_id: str
    path_id: str
    axis_id: str
    name: str
    default_target_strategy: TargetSelectionStrategy
    resource_policy: BattleResourcePolicy
    template_tags: tuple[str, ...]
    actions: tuple[CompiledBehaviorAction, ...]
    applied_patch_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_non_blank(self.template_id, field_name="template_id")
        _require_non_blank(self.path_id, field_name="path_id")
        _require_non_blank(self.axis_id, field_name="axis_id")
        _require_non_blank(self.name, field_name="name")
        if not self.template_tags:
            raise ValueError("运行期行为模板至少需要 1 个模板标签")
        if not self.actions:
            raise ValueError("运行期行为模板至少需要 1 个动作")
        for tag in self.template_tags:
            _require_non_blank(tag, field_name="template_tags")
        for patch_id in self.applied_patch_ids:
            _require_non_blank(patch_id, field_name="applied_patch_ids")
        _require_unique_values((action.action_id for action in self.actions), field_name="actions.action_id")
        _require_unique_values(
            (action.execution_order for action in self.actions),
            field_name="actions.execution_order",
        )


@dataclass(frozen=True, slots=True)
class BattleRandomCall:
    """单次随机调用记录。"""

    sequence: int
    purpose: str
    minimum: int
    maximum: int
    result: int

    def __post_init__(self) -> None:
        _require_positive_int(self.sequence, field_name="sequence")
        _require_non_blank(self.purpose, field_name="purpose")
        if self.minimum > self.maximum:
            raise ValueError("minimum 不能大于 maximum")
        _require_range(
            self.result,
            field_name="result",
            minimum=self.minimum,
            maximum=self.maximum,
        )


class BattleRandomSource(Protocol):
    """战斗随机源接口。"""

    def next_int(self, *, minimum: int, maximum: int, purpose: str) -> int:
        """返回闭区间随机整数，并记录用途标签。"""

    def export_calls(self) -> tuple[BattleRandomCall, ...]:
        """导出已记录的随机调用序列。"""


@dataclass(frozen=True, slots=True)
class BattleEvent:
    """结构化战斗事件。"""

    sequence: int
    round_index: int
    phase: BattleEventPhase
    event_type: str
    actor_unit_id: str | None = None
    target_unit_id: str | None = None
    action_id: str | None = None
    detail_items: tuple[tuple[str, str | int | bool | None], ...] = ()

    def __post_init__(self) -> None:
        _require_positive_int(self.sequence, field_name="sequence")
        _require_non_negative_int(self.round_index, field_name="round_index")
        _require_non_blank(self.event_type, field_name="event_type")
        if self.actor_unit_id is not None:
            _require_non_blank(self.actor_unit_id, field_name="actor_unit_id")
        if self.target_unit_id is not None:
            _require_non_blank(self.target_unit_id, field_name="target_unit_id")
        if self.action_id is not None:
            _require_non_blank(self.action_id, field_name="action_id")
        for key, _ in self.detail_items:
            _require_non_blank(key, field_name="detail_items.key")


@dataclass(frozen=True, slots=True)
class BattleStatusEffect:
    """运行态状态效果。"""

    status_id: str
    status_name: str
    category: BattleStatusCategory
    holder_unit_id: str
    source_unit_id: str
    source_action_id: str
    intensity_permille: int
    duration_rounds: int
    stack_count: int = 1
    max_stacks: int = 1
    base_value: int = 0
    applied_round: int = 0

    def __post_init__(self) -> None:
        _require_non_blank(self.status_id, field_name="status_id")
        _require_non_blank(self.status_name, field_name="status_name")
        _require_non_blank(self.holder_unit_id, field_name="holder_unit_id")
        _require_non_blank(self.source_unit_id, field_name="source_unit_id")
        _require_non_blank(self.source_action_id, field_name="source_action_id")
        _require_rate(
            self.intensity_permille,
            field_name="intensity_permille",
            maximum=_STANDARD_PERMILLE_MAX,
        )
        _require_positive_int(self.duration_rounds, field_name="duration_rounds")
        _require_positive_int(self.stack_count, field_name="stack_count")
        _require_positive_int(self.max_stacks, field_name="max_stacks")
        if self.stack_count > self.max_stacks:
            raise ValueError("stack_count 不能大于 max_stacks")
        _require_non_negative_int(self.base_value, field_name="base_value")
        _require_non_negative_int(self.applied_round, field_name="applied_round")


@dataclass(frozen=True, slots=True)
class BattleActionQueueEntry:
    """回合内的稳定行动队列项。"""

    actor_unit_id: str
    effective_speed: int
    stable_first_strike_key: int
    side_id: int
    stable_order: int

    def __post_init__(self) -> None:
        _require_non_blank(self.actor_unit_id, field_name="actor_unit_id")
        _require_non_negative_int(self.effective_speed, field_name="effective_speed")
        _require_non_negative_int(self.stable_first_strike_key, field_name="stable_first_strike_key")
        _require_non_negative_int(self.side_id, field_name="side_id")
        _require_positive_int(self.stable_order, field_name="stable_order")


@dataclass(frozen=True, slots=True)
class BattleActionDecision:
    """一次行为决策结果。"""

    actor_unit_id: str
    action: CompiledBehaviorAction
    target_unit_ids: tuple[str, ...]
    is_fallback: bool = False
    reaction_type: BattleReactionType | None = None
    reaction_depth: int = 0
    origin_action_id: str | None = None
    consume_cost: bool = True
    can_trigger_counter: bool = True

    def __post_init__(self) -> None:
        _require_non_blank(self.actor_unit_id, field_name="actor_unit_id")
        if not self.target_unit_ids:
            raise ValueError("target_unit_ids 至少需要 1 个目标")
        for target_unit_id in self.target_unit_ids:
            _require_non_blank(target_unit_id, field_name="target_unit_ids")
        _require_unique_values(self.target_unit_ids, field_name="target_unit_ids")
        _require_non_negative_int(self.reaction_depth, field_name="reaction_depth")
        if self.origin_action_id is not None:
            _require_non_blank(self.origin_action_id, field_name="origin_action_id")


@dataclass(slots=True)
class BattleUnitState:
    """战斗中的单位运行态。"""

    base_snapshot: BattleUnitSnapshot
    behavior_template: CompiledBehaviorTemplate
    stable_order: int
    side_id: int
    stable_first_strike_key: int
    current_hp: int
    current_shield: int
    current_resource: int
    cooldowns: dict[str, int] = dataclass_field(default_factory=dict)
    statuses: list[BattleStatusEffect] = dataclass_field(default_factory=list)
    special_effect_states: list[BattleSpecialEffectState] = dataclass_field(default_factory=list)
    current_target_unit_id: str | None = None
    counter_used_this_round: bool = False
    turn_count: int = 0

    def __post_init__(self) -> None:
        _require_positive_int(self.stable_order, field_name="stable_order")
        _require_non_negative_int(self.side_id, field_name="side_id")
        _require_non_negative_int(self.stable_first_strike_key, field_name="stable_first_strike_key")
        _require_range(
            self.current_hp,
            field_name="current_hp",
            minimum=0,
            maximum=self.base_snapshot.max_hp,
        )
        _require_non_negative_int(self.current_shield, field_name="current_shield")
        _require_range(
            self.current_resource,
            field_name="current_resource",
            minimum=0,
            maximum=self.base_snapshot.max_resource,
        )
        if self.current_target_unit_id is not None:
            _require_non_blank(self.current_target_unit_id, field_name="current_target_unit_id")
        self.statuses = list(self.ordered_statuses())
        self.special_effect_states = list(self.ordered_special_effects())
        self.cooldowns = dict(sorted(self.cooldowns.items(), key=lambda item: item[0]))

    @property
    def unit_id(self) -> str:
        """返回单位标识。"""
        return self.base_snapshot.unit_id

    @property
    def unit_name(self) -> str:
        """返回单位名称。"""
        return self.base_snapshot.unit_name

    @property
    def side(self) -> BattleSide:
        """返回单位阵营。"""
        return self.base_snapshot.side

    @property
    def is_alive(self) -> bool:
        """返回单位是否存活。"""
        return self.current_hp > 0

    @property
    def hp_ratio_permille(self) -> int:
        """返回当前气血比例。"""
        return (self.current_hp * 1000) // self.base_snapshot.max_hp

    @property
    def resource_ratio_permille(self) -> int:
        """返回当前资源比例。"""
        if self.base_snapshot.max_resource == 0:
            return 0
        return (self.current_resource * 1000) // self.base_snapshot.max_resource

    @property
    def active_attribute_suppression_permille(self) -> int:
        """返回当前属性压制的有效强度。"""
        return max(
            (
                status.intensity_permille
                for status in self.statuses
                if status.category is BattleStatusCategory.ATTRIBUTE_SUPPRESSION
            ),
            default=0,
        )

    @property
    def effective_attack_power(self) -> int:
        """返回应用属性压制后的攻击。"""
        suppression_permille = self.active_attribute_suppression_permille // 2
        return max(
            1,
            self.base_snapshot.attack_power * (1000 - suppression_permille) // 1000,
        )

    @property
    def effective_guard_power(self) -> int:
        """返回应用属性压制后的防御。"""
        suppression_permille = self.active_attribute_suppression_permille
        return self.base_snapshot.guard_power * (1000 - suppression_permille) // 1000

    @property
    def effective_speed(self) -> int:
        """返回行动排序使用的有效速度。"""
        return self.base_snapshot.speed

    def ordered_statuses(self) -> tuple[BattleStatusEffect, ...]:
        """按稳定顺序返回状态列表。"""
        return tuple(
            sorted(
                self.statuses,
                key=lambda item: (
                    item.category.value,
                    item.status_id,
                    item.source_unit_id,
                    item.source_action_id,
                    item.applied_round,
                ),
            )
        )

    def ordered_special_effects(self) -> tuple[BattleSpecialEffectState, ...]:
        """按稳定顺序返回特殊效果运行态。"""
        return tuple(
            sorted(
                self.special_effect_states,
                key=lambda item: (
                    item.effect_id,
                    item.effect_type,
                    item.owner_unit_id,
                ),
            )
        )


@dataclass(frozen=True, slots=True)
class BattleUnitStatistics:
    """单个单位的结构化统计。"""

    unit_id: str
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

    def __post_init__(self) -> None:
        _require_non_blank(self.unit_id, field_name="unit_id")
        _require_non_negative_int(self.damage_dealt, field_name="damage_dealt")
        _require_non_negative_int(self.damage_taken, field_name="damage_taken")
        _require_non_negative_int(self.healing_done, field_name="healing_done")
        _require_non_negative_int(self.healing_received, field_name="healing_received")
        _require_non_negative_int(self.shield_gained, field_name="shield_gained")
        _require_non_negative_int(self.shield_absorbed, field_name="shield_absorbed")
        _require_non_negative_int(self.actions_executed, field_name="actions_executed")
        _require_non_negative_int(self.pursuits_triggered, field_name="pursuits_triggered")
        _require_non_negative_int(self.counters_triggered, field_name="counters_triggered")
        _require_non_negative_int(self.statuses_applied, field_name="statuses_applied")
        _require_non_negative_int(self.special_effects_triggered, field_name="special_effects_triggered")
        _require_non_negative_int(self.kills, field_name="kills")
        _require_non_negative_int(self.deaths, field_name="deaths")


@dataclass(frozen=True, slots=True)
class BattleStatistics:
    """整场战斗的结构化统计。"""

    unit_statistics: tuple[BattleUnitStatistics, ...]
    total_rounds: int
    total_events: int
    total_random_calls: int

    def __post_init__(self) -> None:
        _require_non_negative_int(self.total_rounds, field_name="total_rounds")
        _require_non_negative_int(self.total_events, field_name="total_events")
        _require_non_negative_int(self.total_random_calls, field_name="total_random_calls")
        _require_unique_values(
            (item.unit_id for item in self.unit_statistics),
            field_name="unit_statistics.unit_id",
        )


@dataclass(frozen=True, slots=True)
class BattleResult:
    """自动战斗的结构化结果。"""

    outcome: BattleOutcome
    completed_rounds: int
    final_units: tuple[BattleUnitState, ...]
    events: tuple[BattleEvent, ...]
    random_calls: tuple[BattleRandomCall, ...]
    statistics: BattleStatistics

    def __post_init__(self) -> None:
        _require_non_negative_int(self.completed_rounds, field_name="completed_rounds")
        _require_unique_values((unit.unit_id for unit in self.final_units), field_name="final_units.unit_id")


@dataclass(frozen=True, slots=True)
class ActionPatchSelector:
    """辅助功法补丁的动作选择器。"""

    action_ids: tuple[str, ...] = ()
    required_labels: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for action_id in self.action_ids:
            _require_non_blank(action_id, field_name="action_ids")
        for label in self.required_labels:
            _require_non_blank(label, field_name="required_labels")
        _require_unique_values(self.action_ids, field_name="action_ids")
        _require_unique_values(self.required_labels, field_name="required_labels")

    def matches(self, *, action_id: str, labels: tuple[str, ...]) -> bool:
        """判断当前选择器是否命中给定动作。"""
        if self.action_ids and action_id not in self.action_ids:
            return False
        if self.required_labels and not set(self.required_labels).issubset(labels):
            return False
        return True


@dataclass(frozen=True, slots=True)
class ActionNumericBonusPatch:
    """数值加成类补丁。"""

    field: ActionNumericField
    delta: int
    selector: ActionPatchSelector = dataclass_field(default_factory=ActionPatchSelector)


@dataclass(frozen=True, slots=True)
class ActionMultiplierPatch:
    """数值乘区类补丁。"""

    field: ActionNumericField
    multiplier_permille: int
    selector: ActionPatchSelector = dataclass_field(default_factory=ActionPatchSelector)

    def __post_init__(self) -> None:
        _require_positive_int(self.multiplier_permille, field_name="multiplier_permille")


@dataclass(frozen=True, slots=True)
class ActionThresholdShiftPatch:
    """阈值平移类补丁。"""

    field: ActionThresholdField
    delta: int
    selector: ActionPatchSelector = dataclass_field(default_factory=ActionPatchSelector)


@dataclass(frozen=True, slots=True)
class ActionTriggerCapAdjustment:
    """触发上限修正类补丁。"""

    delta: int
    selector: ActionPatchSelector = dataclass_field(default_factory=ActionPatchSelector)


@dataclass(frozen=True, slots=True)
class AuxiliarySkillParameterPatch:
    """辅助功法提供的参数补丁集合。"""

    patch_id: str
    patch_name: str
    numeric_bonuses: tuple[ActionNumericBonusPatch, ...] = ()
    multipliers: tuple[ActionMultiplierPatch, ...] = ()
    threshold_shifts: tuple[ActionThresholdShiftPatch, ...] = ()
    trigger_cap_adjustments: tuple[ActionTriggerCapAdjustment, ...] = ()

    def __post_init__(self) -> None:
        _require_non_blank(self.patch_id, field_name="patch_id")
        _require_non_blank(self.patch_name, field_name="patch_name")
        if not any(
            (
                self.numeric_bonuses,
                self.multipliers,
                self.threshold_shifts,
                self.trigger_cap_adjustments,
            )
        ):
            raise ValueError("辅助功法参数补丁至少需要包含 1 类修正")


def _validate_action_fields(
    *,
    action_id: str,
    name: str,
    order: int,
    priority: int,
    weight_permille: int,
    cooldown_rounds: int,
    resource_cost: int,
    damage_scale_permille: int,
    shield_scale_permille: int,
    heal_scale_permille: int,
    control_chance_permille: int,
    max_triggers: int,
    labels: tuple[str, ...],
    self_hp_below_permille: int | None,
    target_hp_below_permille: int | None,
    resource_above_permille: int | None,
    enemy_count_at_least: int | None,
) -> None:
    """校验动作模板的公共字段。"""
    _require_non_blank(action_id, field_name="action_id")
    _require_non_blank(name, field_name="name")
    _require_positive_int(order, field_name="order")
    _require_non_negative_int(priority, field_name="priority")
    _require_non_negative_int(weight_permille, field_name="weight_permille")
    _require_non_negative_int(cooldown_rounds, field_name="cooldown_rounds")
    _require_non_negative_int(resource_cost, field_name="resource_cost")
    _require_non_negative_int(damage_scale_permille, field_name="damage_scale_permille")
    _require_non_negative_int(shield_scale_permille, field_name="shield_scale_permille")
    _require_non_negative_int(heal_scale_permille, field_name="heal_scale_permille")
    _require_rate(
        control_chance_permille,
        field_name="control_chance_permille",
        maximum=_STANDARD_PERMILLE_MAX,
    )
    _require_positive_int(max_triggers, field_name="max_triggers")
    if not labels:
        raise ValueError(f"动作 {action_id} 至少需要 1 个标签")
    for label in labels:
        _require_non_blank(label, field_name="labels")
    _require_unique_values(labels, field_name=f"{action_id}.labels")
    if not any(
        (
            damage_scale_permille > 0,
            shield_scale_permille > 0,
            heal_scale_permille > 0,
            control_chance_permille > 0,
        )
    ):
        raise ValueError(f"动作 {action_id} 至少需要声明 1 类效果强度")
    if self_hp_below_permille is not None:
        _require_rate(
            self_hp_below_permille,
            field_name="self_hp_below_permille",
            maximum=_STANDARD_PERMILLE_MAX,
        )
    if target_hp_below_permille is not None:
        _require_rate(
            target_hp_below_permille,
            field_name="target_hp_below_permille",
            maximum=_STANDARD_PERMILLE_MAX,
        )
    if resource_above_permille is not None:
        _require_rate(
            resource_above_permille,
            field_name="resource_above_permille",
            maximum=_STANDARD_PERMILLE_MAX,
        )
    if enemy_count_at_least is not None:
        _require_positive_int(enemy_count_at_least, field_name="enemy_count_at_least")


def _require_non_blank(value: str, *, field_name: str) -> None:
    """校验字符串字段不能为空。"""
    if not value or not value.strip():
        raise ValueError(f"{field_name} 不能为空")


def _require_positive_int(value: int, *, field_name: str) -> None:
    """校验正整数。"""
    if value <= 0:
        raise ValueError(f"{field_name} 必须大于 0")


def _require_non_negative_int(value: int, *, field_name: str) -> None:
    """校验非负整数。"""
    if value < 0:
        raise ValueError(f"{field_name} 不能小于 0")


def _require_range(value: int, *, field_name: str, minimum: int, maximum: int) -> None:
    """校验闭区间整数范围。"""
    if value < minimum or value > maximum:
        raise ValueError(f"{field_name} 必须位于 {minimum} 到 {maximum} 之间")


def _require_rate(value: int, *, field_name: str, maximum: int = _EXTENDED_PERMILLE_MAX) -> None:
    """校验千分比或扩展千分比字段。"""
    _require_range(value, field_name=field_name, minimum=0, maximum=maximum)


def _require_unique_values(values: tuple[str, ...] | tuple[int, ...] | object, *, field_name: str) -> None:
    """校验序列值不重复。"""
    normalized_values = tuple(values)
    if len(normalized_values) != len(set(normalized_values)):
        raise ValueError(f"{field_name} 存在重复值")


__all__ = [
    "ActionNumericBonusPatch",
    "ActionNumericField",
    "ActionMultiplierPatch",
    "ActionPatchSelector",
    "ActionThresholdField",
    "ActionThresholdShiftPatch",
    "ActionTriggerCapAdjustment",
    "AuxiliarySkillParameterPatch",
    "BattleActionDecision",
    "BattleActionQueueEntry",
    "BattleActionType",
    "BattleEvent",
    "BattleEventPhase",
    "BattleOutcome",
    "BattleRandomCall",
    "BattleRandomSource",
    "BattleReactionType",
    "BattleResourcePolicy",
    "BattleResult",
    "BattleSide",
    "BattleSnapshot",
    "BattleStatistics",
    "BattleStatusCategory",
    "BattleStatusEffect",
    "BattleUnitSnapshot",
    "BattleUnitState",
    "BattleUnitStatistics",
    "BehaviorActionTemplate",
    "BehaviorTemplate",
    "CompiledBehaviorAction",
    "CompiledBehaviorTemplate",
    "TargetSelectionStrategy",
]
