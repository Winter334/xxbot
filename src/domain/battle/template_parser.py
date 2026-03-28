"""行为模板解析器。"""

from __future__ import annotations

from infrastructure.config.static.models.battle import BattleTemplateConfig, BattleTemplateDefinition
from infrastructure.config.static.models.skill import SkillPathConfig

from domain.battle.modifier_resolver import AuxiliaryModifierResolver
from domain.battle.models import (
    AuxiliarySkillParameterPatch,
    BattleActionType,
    BattleResourcePolicy,
    BehaviorActionTemplate,
    BehaviorTemplate,
    CompiledBehaviorTemplate,
    TargetSelectionStrategy,
)


class BattleTemplateNotFoundError(LookupError):
    """行为模板不存在。"""


class BattleTemplateParser:
    """把静态模板配置解析为运行期行为模板。"""

    def __init__(
        self,
        *,
        template_config: BattleTemplateConfig,
        skill_path_config: SkillPathConfig,
        modifier_resolver: AuxiliaryModifierResolver | None = None,
    ) -> None:
        self._template_config = template_config
        self._skill_path_axis_map = {
            path.path_id: path.axis_id
            for path in skill_path_config.paths
        }
        self._modifier_resolver = modifier_resolver or AuxiliaryModifierResolver()

    def parse_template(
        self,
        *,
        path_id: str,
        patches: tuple[AuxiliarySkillParameterPatch, ...] = (),
    ) -> CompiledBehaviorTemplate:
        """按主修路径解析运行期模板。

        解析过程只负责结构映射、补丁合并与参数裁剪。
        不在这里处理战斗流程分支，也不根据具体敌人或场景写特判。
        """
        template_definition = self._require_template_definition(path_id)
        base_template = self._build_base_template(template_definition)
        return self._modifier_resolver.resolve(base_template=base_template, patches=patches)

    def build_base_template(self, *, path_id: str) -> BehaviorTemplate:
        """仅构造未应用补丁的基础模板。"""
        template_definition = self._require_template_definition(path_id)
        return self._build_base_template(template_definition)

    def _require_template_definition(self, path_id: str) -> BattleTemplateDefinition:
        template_definition = self._template_config.get_template_by_path_id(path_id)
        if template_definition is None:
            raise BattleTemplateNotFoundError(f"未找到主修路径行为模板：{path_id}")
        expected_axis_id = self._skill_path_axis_map.get(path_id)
        if expected_axis_id is None:
            raise BattleTemplateNotFoundError(f"未找到主修路径配置：{path_id}")
        if template_definition.axis_id != expected_axis_id:
            raise BattleTemplateNotFoundError(
                f"主修路径 {path_id} 的行为模板主轴与功法配置不一致"
            )
        return template_definition

    @staticmethod
    def _build_base_template(template_definition: BattleTemplateDefinition) -> BehaviorTemplate:
        ordered_actions = tuple(sorted(template_definition.actions, key=lambda item: item.order))
        return BehaviorTemplate(
            template_id=template_definition.template_id,
            path_id=template_definition.path_id,
            axis_id=template_definition.axis_id,
            name=template_definition.name,
            default_target_strategy=TargetSelectionStrategy(template_definition.default_target_strategy),
            resource_policy=BattleResourcePolicy(template_definition.resource_policy),
            template_tags=template_definition.template_tags,
            actions=tuple(
                BehaviorActionTemplate(
                    action_id=action.action_id,
                    name=action.name,
                    order=action.order,
                    action_type=BattleActionType(action.action_type),
                    target_strategy=TargetSelectionStrategy(action.target_strategy),
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
                for action in ordered_actions
            ),
        )


__all__ = [
    "BattleTemplateNotFoundError",
    "BattleTemplateParser",
]
