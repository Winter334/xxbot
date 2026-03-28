"""战斗行为模板静态配置模型。"""

from __future__ import annotations

from collections import Counter

from infrastructure.config.static.errors import StaticConfigIssueCollector
from infrastructure.config.static.models.common import (
    NonNegativeInt,
    OrderedConfigItem,
    PositiveInt,
    ShortText,
    StableId,
    VersionedSectionConfig,
)
from infrastructure.config.static.models.skill import LAUNCH_SKILL_AXIS_IDS, LAUNCH_SKILL_PATH_IDS

ALLOWED_BATTLE_ACTION_TYPE_IDS: tuple[str, ...] = (
    "basic_attack",
    "burst_attack",
    "finisher",
    "combo_attack",
    "counter_attack",
    "shield_skill",
    "heal_skill",
    "area_spell",
    "control_spell",
    "debuff_spell",
)
ALLOWED_TARGET_SELECTION_IDS: tuple[str, ...] = (
    "current_target",
    "lowest_hp_percent",
    "highest_attack",
    "highest_guard",
    "all_enemies",
    "ally_lowest_hp_percent",
    "self",
)
ALLOWED_RESOURCE_POLICY_IDS: tuple[str, ...] = ("conserve", "steady", "burst")


class BattleTemplateActionDefinition(OrderedConfigItem):
    """单个行为模板动作定义。"""

    action_id: StableId
    action_type: StableId
    target_strategy: StableId
    priority: NonNegativeInt
    weight_permille: PositiveInt
    cooldown_rounds: NonNegativeInt
    resource_cost: NonNegativeInt
    damage_scale_permille: NonNegativeInt
    shield_scale_permille: NonNegativeInt
    heal_scale_permille: NonNegativeInt
    control_chance_permille: NonNegativeInt
    max_triggers: PositiveInt
    labels: tuple[StableId, ...]
    self_hp_below_permille: NonNegativeInt | None = None
    target_hp_below_permille: NonNegativeInt | None = None
    resource_above_permille: NonNegativeInt | None = None
    enemy_count_at_least: PositiveInt | None = None


class BattleTemplateDefinition(OrderedConfigItem):
    """单个主修路径的基础行为模板。"""

    template_id: StableId
    path_id: StableId
    axis_id: StableId
    default_target_strategy: StableId
    resource_policy: StableId
    template_tags: tuple[StableId, ...]
    actions: tuple[BattleTemplateActionDefinition, ...]


class BattleTemplateConfig(VersionedSectionConfig):
    """六条主修子方向的行为模板配置。"""

    templates: tuple[BattleTemplateDefinition, ...]

    @property
    def ordered_templates(self) -> tuple[BattleTemplateDefinition, ...]:
        """按顺序返回全部行为模板。"""
        return tuple(sorted(self.templates, key=lambda item: item.order))

    def get_template_by_path_id(self, path_id: str) -> BattleTemplateDefinition | None:
        """按主修路径标识读取行为模板。"""
        for template in self.ordered_templates:
            if template.path_id == path_id:
                return template
        return None

    def collect_issues(self, *, filename: str, collector: StaticConfigIssueCollector) -> None:
        """收集当前配置节的结构与边界错误。"""
        ordered_templates = self.ordered_templates
        path_ids = tuple(template.path_id for template in ordered_templates)
        order_counter = Counter(template.order for template in self.templates)
        path_id_counter = Counter(template.path_id for template in self.templates)
        template_id_counter = Counter(template.template_id for template in self.templates)

        if path_ids != LAUNCH_SKILL_PATH_IDS:
            collector.add(
                filename=filename,
                config_path="templates",
                identifier="path_sequence",
                reason="战斗行为模板必须完整覆盖首发六条子方向，且顺序不可变",
            )

        for template in self.templates:
            if order_counter[template.order] > 1:
                collector.add(
                    filename=filename,
                    config_path="templates[].order",
                    identifier=template.path_id,
                    reason=f"行为模板顺序值 {template.order} 重复",
                )
            if path_id_counter[template.path_id] > 1:
                collector.add(
                    filename=filename,
                    config_path="templates[].path_id",
                    identifier=template.path_id,
                    reason="主修路径标识重复",
                )
            if template_id_counter[template.template_id] > 1:
                collector.add(
                    filename=filename,
                    config_path="templates[].template_id",
                    identifier=template.template_id,
                    reason="行为模板标识重复",
                )
            if template.template_id != template.path_id:
                collector.add(
                    filename=filename,
                    config_path="templates[].template_id",
                    identifier=template.path_id,
                    reason="首发阶段行为模板标识必须与主修路径标识一致",
                )
            if template.axis_id not in LAUNCH_SKILL_AXIS_IDS:
                collector.add(
                    filename=filename,
                    config_path="templates[].axis_id",
                    identifier=template.path_id,
                    reason=f"行为模板引用了未定义主轴 {template.axis_id}",
                )
            if template.default_target_strategy not in ALLOWED_TARGET_SELECTION_IDS:
                collector.add(
                    filename=filename,
                    config_path="templates[].default_target_strategy",
                    identifier=template.path_id,
                    reason=f"默认目标策略 {template.default_target_strategy} 不受支持",
                )
            if template.resource_policy not in ALLOWED_RESOURCE_POLICY_IDS:
                collector.add(
                    filename=filename,
                    config_path="templates[].resource_policy",
                    identifier=template.path_id,
                    reason=f"资源倾向 {template.resource_policy} 不受支持",
                )
            if not template.template_tags:
                collector.add(
                    filename=filename,
                    config_path="templates[].template_tags",
                    identifier=template.path_id,
                    reason="行为模板至少需要 1 个模板标签",
                )
            elif len(set(template.template_tags)) != len(template.template_tags):
                collector.add(
                    filename=filename,
                    config_path="templates[].template_tags",
                    identifier=template.path_id,
                    reason="行为模板标签存在重复值",
                )
            if not template.actions:
                collector.add(
                    filename=filename,
                    config_path="templates[].actions",
                    identifier=template.path_id,
                    reason="行为模板至少需要 1 个动作",
                )
                continue
            self._collect_action_issues(filename=filename, collector=collector, template=template)

    def _collect_action_issues(
        self,
        *,
        filename: str,
        collector: StaticConfigIssueCollector,
        template: BattleTemplateDefinition,
    ) -> None:
        action_order_counter = Counter(action.order for action in template.actions)
        action_id_counter = Counter(action.action_id for action in template.actions)

        for action in template.actions:
            if action_order_counter[action.order] > 1:
                collector.add(
                    filename=filename,
                    config_path="templates[].actions[].order",
                    identifier=action.action_id,
                    reason=f"动作顺序值 {action.order} 重复",
                )
            if action_id_counter[action.action_id] > 1:
                collector.add(
                    filename=filename,
                    config_path="templates[].actions[].action_id",
                    identifier=action.action_id,
                    reason="动作标识重复",
                )
            if action.action_type not in ALLOWED_BATTLE_ACTION_TYPE_IDS:
                collector.add(
                    filename=filename,
                    config_path="templates[].actions[].action_type",
                    identifier=action.action_id,
                    reason=f"动作类别 {action.action_type} 不受支持",
                )
            if action.target_strategy not in ALLOWED_TARGET_SELECTION_IDS:
                collector.add(
                    filename=filename,
                    config_path="templates[].actions[].target_strategy",
                    identifier=action.action_id,
                    reason=f"目标策略 {action.target_strategy} 不受支持",
                )
            if not action.labels:
                collector.add(
                    filename=filename,
                    config_path="templates[].actions[].labels",
                    identifier=action.action_id,
                    reason="动作至少需要 1 个标签",
                )
            elif len(set(action.labels)) != len(action.labels):
                collector.add(
                    filename=filename,
                    config_path="templates[].actions[].labels",
                    identifier=action.action_id,
                    reason="动作标签存在重复值",
                )
            if not any(
                (
                    action.damage_scale_permille > 0,
                    action.shield_scale_permille > 0,
                    action.heal_scale_permille > 0,
                    action.control_chance_permille > 0,
                )
            ):
                collector.add(
                    filename=filename,
                    config_path="templates[].actions[]",
                    identifier=action.action_id,
                    reason="动作至少需要声明 1 类效果强度",
                )
            self._check_permille_cap(
                filename=filename,
                collector=collector,
                config_path="templates[].actions[].control_chance_permille",
                identifier=action.action_id,
                value=action.control_chance_permille,
            )
            self._check_optional_permille_cap(
                filename=filename,
                collector=collector,
                config_path="templates[].actions[].self_hp_below_permille",
                identifier=action.action_id,
                value=action.self_hp_below_permille,
            )
            self._check_optional_permille_cap(
                filename=filename,
                collector=collector,
                config_path="templates[].actions[].target_hp_below_permille",
                identifier=action.action_id,
                value=action.target_hp_below_permille,
            )
            self._check_optional_permille_cap(
                filename=filename,
                collector=collector,
                config_path="templates[].actions[].resource_above_permille",
                identifier=action.action_id,
                value=action.resource_above_permille,
            )

    @staticmethod
    def _check_permille_cap(
        *,
        filename: str,
        collector: StaticConfigIssueCollector,
        config_path: str,
        identifier: str,
        value: int,
    ) -> None:
        if value > 1000:
            collector.add(
                filename=filename,
                config_path=config_path,
                identifier=identifier,
                reason="千分比字段不能大于 1000",
            )

    @classmethod
    def _check_optional_permille_cap(
        cls,
        *,
        filename: str,
        collector: StaticConfigIssueCollector,
        config_path: str,
        identifier: str,
        value: int | None,
    ) -> None:
        if value is None:
            return
        cls._check_permille_cap(
            filename=filename,
            collector=collector,
            config_path=config_path,
            identifier=identifier,
            value=value,
        )


__all__ = [
    "ALLOWED_BATTLE_ACTION_TYPE_IDS",
    "ALLOWED_RESOURCE_POLICY_IDS",
    "ALLOWED_TARGET_SELECTION_IDS",
    "BattleTemplateActionDefinition",
    "BattleTemplateConfig",
    "BattleTemplateDefinition",
]
