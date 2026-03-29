"""无尽副本领域模型。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class EndlessNodeType(StrEnum):
    """无尽副本节点类型。"""

    NORMAL = "normal"
    ELITE = "elite"
    ANCHOR_BOSS = "anchor_boss"


@dataclass(frozen=True, slots=True)
class EndlessRegionSnapshot:
    """单层所属区域快照。"""

    region_index: int
    region_id: str
    region_name: str
    region_bias_id: str
    start_floor: int
    end_floor: int
    theme_summary: str


@dataclass(frozen=True, slots=True)
class EndlessFloorSnapshot:
    """单层规则快照。"""

    floor: int
    region: EndlessRegionSnapshot
    node_type: EndlessNodeType
    is_elite_floor: bool
    is_anchor_floor: bool
    unlocked_as_start_floor: bool
    start_floor: int
    anchor_floor: int
    next_anchor_floor: int


@dataclass(frozen=True, slots=True)
class EndlessEnemyEncounter:
    """单层敌人遭遇结果。"""

    floor: int
    region_id: str
    region_bias_id: str
    node_type: EndlessNodeType
    race_id: str
    template_id: str
    enemy_count: int
    seed: int


@dataclass(frozen=True, slots=True)
class EndlessRewardBreakdown:
    """无尽副本收益拆分结果。"""

    stable_cultivation: int
    stable_insight: int
    stable_refining_essence: int
    pending_drop_progress: int

    def merge(self, other: "EndlessRewardBreakdown") -> "EndlessRewardBreakdown":
        """合并两段收益。"""
        return EndlessRewardBreakdown(
            stable_cultivation=self.stable_cultivation + other.stable_cultivation,
            stable_insight=self.stable_insight + other.stable_insight,
            stable_refining_essence=self.stable_refining_essence + other.stable_refining_essence,
            pending_drop_progress=self.pending_drop_progress + other.pending_drop_progress,
        )

    def to_stable_payload(self) -> dict[str, int]:
        """导出稳定收益载荷。"""
        return {
            "cultivation": self.stable_cultivation,
            "insight": self.stable_insight,
            "refining_essence": self.stable_refining_essence,
        }

    def to_pending_payload(self) -> dict[str, int]:
        """导出未稳收益载荷。"""
        return {"drop_progress": self.pending_drop_progress}

    @property
    def pending_equipment_score(self) -> int:
        """兼容旧字段：统一掉落进度语义下恒为 0。"""
        return 0

    @property
    def pending_artifact_score(self) -> int:
        """兼容旧字段：统一掉落进度语义下恒为 0。"""
        return 0

    @property
    def pending_dao_pattern_score(self) -> int:
        """兼容旧字段：统一掉落进度语义下恒为 0。"""
        return 0


__all__ = [
    "EndlessEnemyEncounter",
    "EndlessFloorSnapshot",
    "EndlessNodeType",
    "EndlessRegionSnapshot",
    "EndlessRewardBreakdown",
]
