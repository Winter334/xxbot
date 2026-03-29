"""无尽副本区域推进与收益规则。"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from domain.character.progression import (
    resolve_endless_region_total_cultivation,
    resolve_endless_region_total_insight,
)
from domain.dungeon.models import EndlessFloorSnapshot, EndlessNodeType, EndlessRegionSnapshot, EndlessRewardBreakdown
from infrastructure.config.static.models.common import StaticGameConfig
from infrastructure.config.static.models.endless_dungeon import (
    EndlessDungeonConfig,
    EndlessNodeRewardDefinition,
    EndlessRegionDefinition,
)


@dataclass(frozen=True, slots=True)
class EndlessAnchorStatus:
    """单层锚点与起点状态。"""

    floor: int
    current_anchor_floor: int
    next_anchor_floor: int
    is_anchor_floor: bool
    unlocked_as_start_floor: bool


class EndlessDungeonRuleError(ValueError):
    """无尽副本规则参数非法。"""


class EndlessDungeonProgression:
    """基于静态配置解析无尽副本区域、节点与收益规则。"""

    def __init__(self, static_config: StaticGameConfig) -> None:
        self._static_config = static_config
        self._config = static_config.endless_dungeon
        self._regions = self._config.ordered_regions
        self._reward_by_node_type = {
            reward.node_type: reward
            for reward in self._config.ordered_node_rewards
        }
        self._node_occurrence_count_by_type = self._build_node_occurrence_count_by_type()
        if not self._regions:
            raise EndlessDungeonRuleError("无尽副本至少需要一个区域")

    @property
    def floors_per_region(self) -> int:
        """返回单个区域包含的层数。"""
        return self._config.structure.floors_per_region

    @property
    def elite_interval(self) -> int:
        """返回精英节点间隔层数。"""
        return self._config.structure.elite_interval

    @property
    def anchor_interval(self) -> int:
        """返回锚点首领间隔层数。"""
        return self._config.structure.anchor_interval

    def resolve_floor(self, floor: int, *, highest_unlocked_anchor_floor: int = 0) -> EndlessFloorSnapshot:
        """按层数解析区域、节点类型与起点状态。"""
        normalized_floor = self._normalize_floor(floor)
        region = self.resolve_region(normalized_floor)
        node_type = self.resolve_node_type(normalized_floor)
        anchor_status = self.resolve_anchor_status(
            normalized_floor,
            highest_unlocked_anchor_floor=highest_unlocked_anchor_floor,
        )
        return EndlessFloorSnapshot(
            floor=normalized_floor,
            region=region,
            node_type=node_type,
            is_elite_floor=node_type is EndlessNodeType.ELITE,
            is_anchor_floor=node_type is EndlessNodeType.ANCHOR_BOSS,
            unlocked_as_start_floor=anchor_status.unlocked_as_start_floor,
            start_floor=1 if highest_unlocked_anchor_floor < self.anchor_interval else highest_unlocked_anchor_floor,
            anchor_floor=anchor_status.current_anchor_floor,
            next_anchor_floor=anchor_status.next_anchor_floor,
        )

    def resolve_region(self, floor: int) -> EndlessRegionSnapshot:
        """按层数解析所在区域。"""
        normalized_floor = self._normalize_floor(floor)
        region_number = ((normalized_floor - 1) // self.floors_per_region) + 1
        region_definition = self._regions[(region_number - 1) % len(self._regions)]
        cycle_offset = ((region_number - 1) // len(self._regions)) * len(self._regions)
        effective_region_index = cycle_offset + region_definition.order
        start_floor = (region_number - 1) * self.floors_per_region + 1
        end_floor = region_number * self.floors_per_region
        return EndlessRegionSnapshot(
            region_index=effective_region_index,
            region_id=region_definition.region_id,
            region_name=region_definition.name,
            region_bias_id=region_definition.region_bias_id,
            start_floor=start_floor,
            end_floor=end_floor,
            theme_summary=region_definition.theme_summary,
        )

    def resolve_node_type(self, floor: int) -> EndlessNodeType:
        """按层数解析节点类型。"""
        normalized_floor = self._normalize_floor(floor)
        if normalized_floor % self.anchor_interval == 0:
            return EndlessNodeType.ANCHOR_BOSS
        if normalized_floor % self.elite_interval == 0:
            return EndlessNodeType.ELITE
        return EndlessNodeType.NORMAL

    def resolve_anchor_status(self, floor: int, *, highest_unlocked_anchor_floor: int = 0) -> EndlessAnchorStatus:
        """解析当前层对应的锚点与起点解锁状态。"""
        normalized_floor = self._normalize_floor(floor)
        if highest_unlocked_anchor_floor < 0:
            raise EndlessDungeonRuleError("highest_unlocked_anchor_floor 不能为负数")
        is_anchor_floor = normalized_floor % self.anchor_interval == 0
        if is_anchor_floor:
            current_anchor_floor = normalized_floor
        else:
            current_anchor_floor = ((normalized_floor - 1) // self.anchor_interval) * self.anchor_interval
        next_anchor_floor = current_anchor_floor + self.anchor_interval
        unlocked_as_start_floor = (
            current_anchor_floor > 0 and highest_unlocked_anchor_floor >= current_anchor_floor
        )
        return EndlessAnchorStatus(
            floor=normalized_floor,
            current_anchor_floor=current_anchor_floor,
            next_anchor_floor=next_anchor_floor,
            is_anchor_floor=is_anchor_floor,
            unlocked_as_start_floor=unlocked_as_start_floor,
        )

    def get_available_start_floors(self, highest_unlocked_anchor_floor: int) -> tuple[int, ...]:
        """按已解锁锚点返回可选起点列表。"""
        if highest_unlocked_anchor_floor < 0:
            raise EndlessDungeonRuleError("highest_unlocked_anchor_floor 不能为负数")
        unlocked_anchor_count = highest_unlocked_anchor_floor // self.anchor_interval
        unlocked_floors = tuple(
            anchor_index * self.anchor_interval
            for anchor_index in range(1, unlocked_anchor_count + 1)
        )
        return (1, *unlocked_floors)

    def build_reward_breakdown(self, floor: int, *, realm_id: str) -> EndlessRewardBreakdown:
        """按层数计算稳定收益与未稳收益。"""
        normalized_floor = self._normalize_floor(floor)
        region_snapshot = self.resolve_region(normalized_floor)
        node_reward = self._require_node_reward(self.resolve_node_type(normalized_floor).value)
        region_bonus_index = region_snapshot.region_index - 1
        scaling = self._config.reward_scaling
        return EndlessRewardBreakdown(
            stable_cultivation=self._resolve_weighted_stable_amount(
                floor=normalized_floor,
                realm_id=realm_id,
                resource_kind="cultivation",
            ),
            stable_insight=self._resolve_weighted_stable_amount(
                floor=normalized_floor,
                realm_id=realm_id,
                resource_kind="insight",
            ),
            stable_refining_essence=(
                node_reward.stable_refining_essence
                + scaling.stable_refining_essence_per_region * region_bonus_index
            ),
            pending_drop_progress=node_reward.pending_drop_progress,
        )

    def settle_failure_pending_rewards(self, rewards: EndlessRewardBreakdown) -> EndlessRewardBreakdown:
        """按战败保留比例结算未稳收益。"""
        keep_ratio = self._config.reward_scaling.failure_pending_keep_ratio
        return EndlessRewardBreakdown(
            stable_cultivation=rewards.stable_cultivation,
            stable_insight=rewards.stable_insight,
            stable_refining_essence=rewards.stable_refining_essence,
            pending_drop_progress=int(rewards.pending_drop_progress * keep_ratio),
        )

    def settle_retreat_rewards(self, rewards: EndlessRewardBreakdown) -> EndlessRewardBreakdown:
        """主动撤离时全额保留收益。"""
        return rewards

    @staticmethod
    def _normalize_floor(floor: int) -> int:
        if floor <= 0:
            raise EndlessDungeonRuleError("floor 必须为正整数")
        return floor

    def _require_node_reward(self, node_type: str) -> EndlessNodeRewardDefinition:
        try:
            return self._reward_by_node_type[node_type]
        except KeyError as exc:
            raise EndlessDungeonRuleError(f"缺少节点收益配置：{node_type}") from exc

    def _resolve_weighted_stable_amount(self, *, floor: int, realm_id: str, resource_kind: str) -> int:
        region_snapshot = self.resolve_region(floor)
        node_type = self.resolve_node_type(floor)
        node_type_totals = self._build_weighted_node_type_totals(
            realm_id=realm_id,
            resource_kind=resource_kind,
        )
        node_total = node_type_totals[node_type.value]
        occurrence_count = self._node_occurrence_count_by_type[node_type.value]
        occurrence_index = self._resolve_node_occurrence_index_in_region(
            floor=floor,
            region_snapshot=region_snapshot,
            node_type=node_type,
        )
        base_amount = node_total // occurrence_count
        remainder = node_total % occurrence_count
        return base_amount + (1 if occurrence_index <= remainder else 0)

    def _build_weighted_node_type_totals(self, *, realm_id: str, resource_kind: str) -> dict[str, int]:
        region_total = self._resolve_region_total_value(realm_id=realm_id, resource_kind=resource_kind)
        total_weight = sum(
            self._resolve_node_weight(node_type=reward.node_type, resource_kind=resource_kind)
            * self._node_occurrence_count_by_type[reward.node_type]
            for reward in self._config.ordered_node_rewards
        )
        if total_weight <= 0:
            raise EndlessDungeonRuleError(f"无尽副本节点权重总和非法：{resource_kind}")

        allocated_totals: dict[str, int] = {}
        remainders: list[tuple[Decimal, int, str]] = []
        allocated_sum = 0
        for reward in self._config.ordered_node_rewards:
            weighted_value = (
                Decimal(region_total)
                * Decimal(
                    self._resolve_node_weight(node_type=reward.node_type, resource_kind=resource_kind)
                    * self._node_occurrence_count_by_type[reward.node_type]
                )
                / Decimal(total_weight)
            )
            base_value = int(weighted_value)
            allocated_totals[reward.node_type] = base_value
            allocated_sum += base_value
            remainders.append((weighted_value - Decimal(base_value), reward.order, reward.node_type))

        remaining = region_total - allocated_sum
        for _, _, node_type in sorted(remainders, key=lambda item: (-item[0], item[1]))[:remaining]:
            allocated_totals[node_type] += 1
        return allocated_totals

    def _resolve_region_total_value(self, *, realm_id: str, resource_kind: str) -> int:
        if resource_kind == "cultivation":
            return resolve_endless_region_total_cultivation(
                static_config=self._static_config,
                realm_id=realm_id,
            )
        if resource_kind == "insight":
            return resolve_endless_region_total_insight(
                static_config=self._static_config,
                realm_id=realm_id,
            )
        raise EndlessDungeonRuleError(f"未支持的无尽副本稳定收益类型：{resource_kind}")

    def _resolve_node_weight(self, *, node_type: str, resource_kind: str) -> int:
        reward = self._require_node_reward(node_type)
        if resource_kind == "cultivation":
            return reward.stable_cultivation
        if resource_kind == "insight":
            return reward.stable_insight
        raise EndlessDungeonRuleError(f"未支持的无尽副本节点权重类型：{resource_kind}")

    def _resolve_node_occurrence_index_in_region(
        self,
        *,
        floor: int,
        region_snapshot: EndlessRegionSnapshot,
        node_type: EndlessNodeType,
    ) -> int:
        occurrence_index = 0
        for current_floor in range(region_snapshot.start_floor, floor + 1):
            if self.resolve_node_type(current_floor) is node_type:
                occurrence_index += 1
        return occurrence_index

    def _build_node_occurrence_count_by_type(self) -> dict[str, int]:
        counts = {node_type: 0 for node_type in self._reward_by_node_type}
        for floor in range(1, self.floors_per_region + 1):
            counts[self.resolve_node_type(floor).value] += 1
        return counts


__all__ = [
    "EndlessAnchorStatus",
    "EndlessDungeonProgression",
    "EndlessDungeonRuleError",
]
