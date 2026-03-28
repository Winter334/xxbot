"""敌人与区域偏置配置模型。"""

from __future__ import annotations

from collections import Counter

from infrastructure.config.static.errors import StaticConfigIssueCollector
from infrastructure.config.static.models.common import (
    OrderedConfigItem,
    PositiveDecimal,
    PositiveInt,
    ShortText,
    StableId,
    VersionedSectionConfig,
)

LAUNCH_ENEMY_TEMPLATE_IDS: tuple[str, ...] = (
    "berserker",
    "guardian",
    "swift",
    "caster",
    "restorer",
)
LAUNCH_ENEMY_RACE_IDS: tuple[str, ...] = (
    "beast",
    "heretic",
    "puppet",
    "spirit",
    "ancient_demon",
)
LAUNCH_REGION_BIAS_IDS: tuple[str, ...] = ("wind", "flame", "frost", "shade", "thunder")


class EnemyTemplateDefinition(OrderedConfigItem):
    """敌人模板定义。"""

    template_id: StableId
    combat_profile: ShortText
    normal_active_skill_count: PositiveInt
    normal_passive_skill_count: PositiveInt
    elite_active_skill_count: PositiveInt
    elite_passive_skill_count: PositiveInt
    boss_active_skill_count: PositiveInt
    boss_passive_skill_count: PositiveInt


class EnemyRaceDefinition(OrderedConfigItem):
    """敌人族群定义。"""

    race_id: StableId
    favored_template_ids: tuple[StableId, ...]
    combat_profile: ShortText


class RegionTemplateWeight(OrderedConfigItem):
    """区域内模板权重。"""

    template_id: StableId
    weight: PositiveDecimal


class RegionBiasDefinition(OrderedConfigItem):
    """区域偏置定义。"""

    region_bias_id: StableId
    theme_summary: ShortText
    favored_race_ids: tuple[StableId, ...]
    template_weights: tuple[RegionTemplateWeight, ...]


class EnemyConfig(VersionedSectionConfig):
    """敌人模板、族群、区域偏置配置。"""

    templates: tuple[EnemyTemplateDefinition, ...]
    races: tuple[EnemyRaceDefinition, ...]
    region_biases: tuple[RegionBiasDefinition, ...]

    def collect_issues(self, *, filename: str, collector: StaticConfigIssueCollector) -> None:
        """收集当前配置节的结构与交叉引用错误。"""
        self._collect_templates(filename=filename, collector=collector)
        self._collect_races(filename=filename, collector=collector)
        self._collect_region_biases(filename=filename, collector=collector)

    def _collect_templates(self, *, filename: str, collector: StaticConfigIssueCollector) -> None:
        ordered_templates = tuple(sorted(self.templates, key=lambda item: item.order))
        template_ids = tuple(template.template_id for template in ordered_templates)
        if template_ids != LAUNCH_ENEMY_TEMPLATE_IDS:
            collector.add(
                filename=filename,
                config_path="templates",
                identifier="template_sequence",
                reason="敌人模板必须固定为首发五类模板，且顺序不可变",
            )

        for template in self.templates:
            if template.normal_active_skill_count != 1 or template.normal_passive_skill_count != 1:
                collector.add(
                    filename=filename,
                    config_path="templates[].normal_active_skill_count",
                    identifier=template.template_id,
                    reason="普通怪复杂度必须固定为 1 个主动技能加 1 个被动",
                )
            if template.elite_active_skill_count != 2 or template.elite_passive_skill_count != 1:
                collector.add(
                    filename=filename,
                    config_path="templates[].elite_active_skill_count",
                    identifier=template.template_id,
                    reason="精英怪复杂度必须固定为 2 个主动技能加 1 个被动",
                )
            if template.boss_active_skill_count not in (2, 3) or template.boss_passive_skill_count != 2:
                collector.add(
                    filename=filename,
                    config_path="templates[].boss_active_skill_count",
                    identifier=template.template_id,
                    reason="锚点首领复杂度必须固定为 2 到 3 个主动技能加 2 个被动",
                )

    def _collect_races(self, *, filename: str, collector: StaticConfigIssueCollector) -> None:
        ordered_races = tuple(sorted(self.races, key=lambda item: item.order))
        race_ids = tuple(race.race_id for race in ordered_races)
        known_template_ids = {template.template_id for template in self.templates}
        if race_ids != LAUNCH_ENEMY_RACE_IDS:
            collector.add(
                filename=filename,
                config_path="races",
                identifier="race_sequence",
                reason="敌人族群必须固定为首发五类族群，且顺序不可变",
            )

        for race in self.races:
            for template_id in race.favored_template_ids:
                if template_id not in known_template_ids:
                    collector.add(
                        filename=filename,
                        config_path="races[].favored_template_ids",
                        identifier=race.race_id,
                        reason=f"族群引用了未定义模板 {template_id}",
                    )

    def _collect_region_biases(self, *, filename: str, collector: StaticConfigIssueCollector) -> None:
        ordered_regions = tuple(sorted(self.region_biases, key=lambda item: item.order))
        region_ids = tuple(region.region_bias_id for region in ordered_regions)
        known_race_ids = {race.race_id for race in self.races}
        known_template_ids = {template.template_id for template in self.templates}

        if region_ids != LAUNCH_REGION_BIAS_IDS:
            collector.add(
                filename=filename,
                config_path="region_biases",
                identifier="region_bias_sequence",
                reason="区域偏置必须固定为首发五个区域主题，且顺序不可变",
            )

        for region in self.region_biases:
            weight_counter = Counter(weight.template_id for weight in region.template_weights)
            for race_id in region.favored_race_ids:
                if race_id not in known_race_ids:
                    collector.add(
                        filename=filename,
                        config_path="region_biases[].favored_race_ids",
                        identifier=region.region_bias_id,
                        reason=f"区域偏置引用了未定义族群 {race_id}",
                    )
            for weight in region.template_weights:
                if weight.template_id not in known_template_ids:
                    collector.add(
                        filename=filename,
                        config_path="region_biases[].template_weights[].template_id",
                        identifier=region.region_bias_id,
                        reason=f"区域偏置引用了未定义模板 {weight.template_id}",
                    )
                if weight_counter[weight.template_id] > 1:
                    collector.add(
                        filename=filename,
                        config_path="region_biases[].template_weights",
                        identifier=f"{region.region_bias_id}:{weight.template_id}",
                        reason="同一区域内模板权重重复",
                    )
