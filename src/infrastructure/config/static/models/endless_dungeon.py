"""无尽副本静态配置模型。"""

from __future__ import annotations

from collections import Counter

from infrastructure.config.static.errors import StaticConfigIssueCollector
from infrastructure.config.static.models.common import (
    NonNegativeInt,
    OrderedConfigItem,
    PercentageDecimal,
    PositiveDecimal,
    PositiveInt,
    ShortText,
    StableId,
    StaticConfigModel,
    VersionedSectionConfig,
)

LAUNCH_ENDLESS_REGION_IDS: tuple[str, ...] = ("wind", "flame", "frost", "shade", "thunder")
ALLOWED_ENDLESS_NODE_TYPE_IDS: tuple[str, ...] = ("normal", "elite", "anchor_boss")


class EndlessStructureConfig(StaticConfigModel):
    """无尽副本层数结构配置。"""

    floors_per_region: PositiveInt
    elite_interval: PositiveInt
    anchor_interval: PositiveInt


class EndlessEncounterConfig(StaticConfigModel):
    """无尽副本遭遇生成附加参数。"""

    normal_enemy_count: PositiveInt
    elite_enemy_count: PositiveInt
    boss_enemy_count: PositiveInt
    favored_race_bonus: PositiveDecimal
    favored_template_bonus: PositiveDecimal


class EndlessRewardScalingConfig(StaticConfigModel):
    """无尽副本收益随区域递增的规则。"""

    stable_cultivation_per_region: PositiveInt
    stable_insight_per_region: PositiveInt
    stable_refining_essence_per_region: PositiveInt
    pending_equipment_per_region: PositiveInt
    pending_artifact_per_region: PositiveInt
    pending_dao_pattern_per_region: PositiveInt
    failure_pending_keep_ratio: PercentageDecimal


class EndlessRegionDefinition(OrderedConfigItem):
    """无尽副本区域定义。"""

    region_id: StableId
    region_bias_id: StableId
    theme_summary: ShortText


class EndlessNodeRewardDefinition(OrderedConfigItem):
    """单类节点的基础收益定义。"""

    node_type: StableId
    stable_cultivation: PositiveInt
    stable_insight: PositiveInt
    stable_refining_essence: PositiveInt
    pending_equipment_score: NonNegativeInt
    pending_artifact_score: NonNegativeInt
    pending_dao_pattern_score: NonNegativeInt


class EndlessDungeonConfig(VersionedSectionConfig):
    """无尽副本静态配置。"""

    structure: EndlessStructureConfig
    encounter: EndlessEncounterConfig
    reward_scaling: EndlessRewardScalingConfig
    regions: tuple[EndlessRegionDefinition, ...]
    node_rewards: tuple[EndlessNodeRewardDefinition, ...]

    @property
    def ordered_regions(self) -> tuple[EndlessRegionDefinition, ...]:
        """按顺序返回全部区域定义。"""
        return tuple(sorted(self.regions, key=lambda item: item.order))

    @property
    def ordered_node_rewards(self) -> tuple[EndlessNodeRewardDefinition, ...]:
        """按顺序返回全部节点收益定义。"""
        return tuple(sorted(self.node_rewards, key=lambda item: item.order))

    def get_node_reward(self, node_type: str) -> EndlessNodeRewardDefinition | None:
        """按节点类型读取收益定义。"""
        for reward in self.ordered_node_rewards:
            if reward.node_type == node_type:
                return reward
        return None

    def collect_issues(self, *, filename: str, collector: StaticConfigIssueCollector) -> None:
        """收集当前配置节的结构与边界错误。"""
        self._collect_structure_issues(filename=filename, collector=collector)
        self._collect_region_issues(filename=filename, collector=collector)
        self._collect_node_reward_issues(filename=filename, collector=collector)

    def _collect_structure_issues(self, *, filename: str, collector: StaticConfigIssueCollector) -> None:
        structure = self.structure
        if structure.floors_per_region != 20:
            collector.add(
                filename=filename,
                config_path="structure.floors_per_region",
                identifier="floors_per_region",
                reason="无尽副本区域必须固定为每 20 层一个区域",
            )
        if structure.elite_interval != 5:
            collector.add(
                filename=filename,
                config_path="structure.elite_interval",
                identifier="elite_interval",
                reason="无尽副本必须固定为每 5 层出现一次精英节点",
            )
        if structure.anchor_interval != 10:
            collector.add(
                filename=filename,
                config_path="structure.anchor_interval",
                identifier="anchor_interval",
                reason="无尽副本必须固定为每 10 层出现一次锚点首领",
            )
        if structure.anchor_interval % structure.elite_interval != 0:
            collector.add(
                filename=filename,
                config_path="structure.anchor_interval",
                identifier="anchor_alignment",
                reason="锚点间隔必须是精英间隔的整数倍",
            )
        if structure.floors_per_region % structure.anchor_interval != 0:
            collector.add(
                filename=filename,
                config_path="structure.floors_per_region",
                identifier="region_anchor_alignment",
                reason="区域层数必须能被锚点间隔整除",
            )

    def _collect_region_issues(self, *, filename: str, collector: StaticConfigIssueCollector) -> None:
        ordered_regions = self.ordered_regions
        region_ids = tuple(region.region_id for region in ordered_regions)
        order_counter = Counter(region.order for region in self.regions)
        region_id_counter = Counter(region.region_id for region in self.regions)
        region_bias_counter = Counter(region.region_bias_id for region in self.regions)

        if region_ids != LAUNCH_ENDLESS_REGION_IDS:
            collector.add(
                filename=filename,
                config_path="regions",
                identifier="region_sequence",
                reason="无尽副本区域顺序必须固定为 wind、flame、frost、shade、thunder",
            )

        for region in self.regions:
            if order_counter[region.order] > 1:
                collector.add(
                    filename=filename,
                    config_path="regions[].order",
                    identifier=region.region_id,
                    reason=f"无尽副本区域顺序值 {region.order} 重复",
                )
            if region_id_counter[region.region_id] > 1:
                collector.add(
                    filename=filename,
                    config_path="regions[].region_id",
                    identifier=region.region_id,
                    reason="无尽副本区域标识重复",
                )
            if region_bias_counter[region.region_bias_id] > 1:
                collector.add(
                    filename=filename,
                    config_path="regions[].region_bias_id",
                    identifier=region.region_bias_id,
                    reason="无尽副本区域偏置映射重复",
                )

    def _collect_node_reward_issues(self, *, filename: str, collector: StaticConfigIssueCollector) -> None:
        ordered_rewards = self.ordered_node_rewards
        node_types = tuple(reward.node_type for reward in ordered_rewards)
        order_counter = Counter(reward.order for reward in self.node_rewards)
        node_type_counter = Counter(reward.node_type for reward in self.node_rewards)

        if node_types != ALLOWED_ENDLESS_NODE_TYPE_IDS:
            collector.add(
                filename=filename,
                config_path="node_rewards",
                identifier="node_type_sequence",
                reason="无尽副本收益节点必须固定为 normal、elite、anchor_boss",
            )

        for reward in self.node_rewards:
            if order_counter[reward.order] > 1:
                collector.add(
                    filename=filename,
                    config_path="node_rewards[].order",
                    identifier=reward.node_type,
                    reason=f"无尽副本节点收益顺序值 {reward.order} 重复",
                )
            if node_type_counter[reward.node_type] > 1:
                collector.add(
                    filename=filename,
                    config_path="node_rewards[].node_type",
                    identifier=reward.node_type,
                    reason="无尽副本节点收益类型重复",
                )

        ordered_by_type = {reward.node_type: reward for reward in ordered_rewards}
        normal_reward = ordered_by_type.get("normal")
        elite_reward = ordered_by_type.get("elite")
        anchor_reward = ordered_by_type.get("anchor_boss")
        if normal_reward is None or elite_reward is None or anchor_reward is None:
            return

        if elite_reward.stable_cultivation <= normal_reward.stable_cultivation:
            collector.add(
                filename=filename,
                config_path="node_rewards[].stable_cultivation",
                identifier="elite",
                reason="精英节点的稳定修为收益必须高于普通节点",
            )
        if anchor_reward.stable_cultivation <= elite_reward.stable_cultivation:
            collector.add(
                filename=filename,
                config_path="node_rewards[].stable_cultivation",
                identifier="anchor_boss",
                reason="锚点首领的稳定修为收益必须高于精英节点",
            )
        if anchor_reward.pending_equipment_score < elite_reward.pending_equipment_score:
            collector.add(
                filename=filename,
                config_path="node_rewards[].pending_equipment_score",
                identifier="anchor_boss",
                reason="锚点首领的未稳装备收益不得低于精英节点",
            )


__all__ = [
    "ALLOWED_ENDLESS_NODE_TYPE_IDS",
    "LAUNCH_ENDLESS_REGION_IDS",
    "EndlessDungeonConfig",
    "EndlessEncounterConfig",
    "EndlessNodeRewardDefinition",
    "EndlessRegionDefinition",
    "EndlessRewardScalingConfig",
    "EndlessStructureConfig",
]
