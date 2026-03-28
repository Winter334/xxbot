"""突破秘境领域模型。"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from enum import StrEnum

_DECIMAL_THOUSAND = Decimal("1000")


class BreakthroughTrialProgressStatus(StrEnum):
    """突破秘境历史进度状态。"""

    FAILED = "failed"
    CLEARED = "cleared"


class BreakthroughSettlementType(StrEnum):
    """突破秘境结算类型。"""

    DEFEAT = "defeat"
    FIRST_CLEAR = "first_clear"
    REPEAT_CLEAR = "repeat_clear"


class BreakthroughRewardKind(StrEnum):
    """突破秘境可发放的奖励类型。"""

    QUALIFICATION = "qualification"
    CURRENCY = "currency"
    MATERIAL = "material"


class BreakthroughRewardDirection(StrEnum):
    """突破秘境重复挑战主资源方向。"""

    SPIRIT_STONE = "spirit_stone"
    ENHANCEMENT_MATERIAL = "enhancement_material"
    REFORGE_MATERIAL = "reforge_material"
    COMPREHENSION_MATERIAL = "comprehension_material"
    ARTIFACT_MATERIAL = "artifact_material"


class BreakthroughRewardCycleType(StrEnum):
    """突破秘境软限制周期类型。"""

    DAILY = "daily"


@dataclass(frozen=True, slots=True)
class BreakthroughProgressSnapshot:
    """突破关卡的只读进度快照。"""

    mapping_id: str
    status: BreakthroughTrialProgressStatus | None
    attempt_count: int
    cleared_count: int = 0
    best_clear_at: str | None = None
    first_cleared_at: str | None = None
    last_cleared_at: str | None = None
    qualification_granted_at: str | None = None
    last_reward_direction: str | None = None


@dataclass(frozen=True, slots=True)
class BreakthroughRewardItem:
    """突破秘境结算中的单条奖励。"""

    reward_kind: BreakthroughRewardKind
    resource_id: str | None = None
    quantity: int | None = None
    bound: bool = True

    def __post_init__(self) -> None:
        if self.reward_kind is BreakthroughRewardKind.QUALIFICATION:
            if self.resource_id is not None or self.quantity is not None:
                raise ValueError("qualification 奖励不能声明资源字段")
            return
        if self.resource_id is None:
            raise ValueError("资源奖励必须声明 resource_id")
        if self.quantity is None or self.quantity <= 0:
            raise ValueError("资源奖励数量必须大于 0")
        if self.reward_kind is BreakthroughRewardKind.MATERIAL and not self.bound:
            raise ValueError("material 奖励必须为绑定物品")

    def to_payload(self) -> dict[str, object]:
        """导出结算与审计共用的奖励载荷。"""
        payload: dict[str, object] = {"reward_kind": self.reward_kind.value}
        if self.resource_id is not None:
            payload["resource_id"] = self.resource_id
        if self.quantity is not None:
            payload["quantity"] = self.quantity
        if self.reward_kind is not BreakthroughRewardKind.QUALIFICATION:
            payload["bound"] = self.bound
        return payload


@dataclass(frozen=True, slots=True)
class BreakthroughSoftLimitSnapshot:
    """重复挑战软限制结算快照。"""

    reward_direction: BreakthroughRewardDirection
    cycle_type: BreakthroughRewardCycleType
    cycle_anchor: str
    high_yield_limit: int
    consumed_count_before: int
    consumed_count_after: int
    applied_ratio: Decimal
    entered_reduced_yield: bool

    def __post_init__(self) -> None:
        if self.high_yield_limit <= 0:
            raise ValueError("high_yield_limit 必须大于 0")
        if self.consumed_count_before < 0 or self.consumed_count_after < 0:
            raise ValueError("软限制次数不能为负数")
        if self.consumed_count_after < self.consumed_count_before:
            raise ValueError("软限制次数不能回退")
        if self.applied_ratio <= 0:
            raise ValueError("软限制倍率必须大于 0")

    def to_payload(self) -> dict[str, object]:
        """导出可写入审计的软限制快照。"""
        return {
            "reward_direction": self.reward_direction.value,
            "cycle_type": self.cycle_type.value,
            "cycle_anchor": self.cycle_anchor,
            "high_yield_limit": self.high_yield_limit,
            "consumed_count_before": self.consumed_count_before,
            "consumed_count_after": self.consumed_count_after,
            "applied_ratio": str(self.applied_ratio),
            "entered_reduced_yield": self.entered_reduced_yield,
        }


@dataclass(frozen=True, slots=True)
class BreakthroughRewardPackage:
    """一次结算最终发放的奖励包。"""

    direction: BreakthroughRewardDirection | None
    items: tuple[BreakthroughRewardItem, ...]
    soft_limit: BreakthroughSoftLimitSnapshot | None = None

    def __post_init__(self) -> None:
        qualification_count = sum(1 for item in self.items if item.reward_kind is BreakthroughRewardKind.QUALIFICATION)
        if qualification_count > 1:
            raise ValueError("同一次结算最多只能包含 1 条 qualification 奖励")
        if qualification_count and self.direction is not None:
            raise ValueError("包含 qualification 的奖励包不能声明重复奖励方向")
        if self.soft_limit is not None and self.direction is None:
            raise ValueError("软限制快照只能用于重复奖励包")
        if self.soft_limit is not None and self.direction is not self.soft_limit.reward_direction:
            raise ValueError("奖励包方向必须与软限制快照方向一致")

    def is_empty(self) -> bool:
        """判断当前奖励包是否为空。"""
        return not self.items

    def to_payload(self) -> dict[str, object]:
        """导出结构化奖励载荷。"""
        return {
            "reward_direction": None if self.direction is None else self.direction.value,
            "items": [item.to_payload() for item in self.items],
            "soft_limit": None if self.soft_limit is None else self.soft_limit.to_payload(),
        }


@dataclass(frozen=True, slots=True)
class BreakthroughSettlementResult:
    """单次突破秘境结算结果。"""

    settlement_type: BreakthroughSettlementType
    victory: bool
    mapping_id: str
    reward_package: BreakthroughRewardPackage
    qualification_granted: bool
    resulting_progress_status: BreakthroughTrialProgressStatus | None

    def __post_init__(self) -> None:
        if self.settlement_type is BreakthroughSettlementType.DEFEAT and self.victory:
            raise ValueError("失败结算不能声明为胜利")
        if self.settlement_type is not BreakthroughSettlementType.DEFEAT and not self.victory:
            raise ValueError("胜利结算必须为首通或重复成功")
        if self.qualification_granted and self.settlement_type is not BreakthroughSettlementType.FIRST_CLEAR:
            raise ValueError("只有首通成功才能发放突破资格")
        if self.settlement_type is BreakthroughSettlementType.REPEAT_CLEAR and self.resulting_progress_status is not BreakthroughTrialProgressStatus.CLEARED:
            raise ValueError("重复胜利后的进度状态必须保持为 cleared")

    def to_progress_payload(
        self,
        *,
        battle_report_id: int | None,
        occurred_at: str,
    ) -> dict[str, object]:
        """导出可写入进度快照的结构化结果。"""
        return {
            "result": "victory" if self.victory else "defeat",
            "settlement_type": self.settlement_type.value,
            "battle_report_id": battle_report_id,
            "reward_direction": None if self.reward_package.direction is None else self.reward_package.direction.value,
            "reward_payload": self.reward_package.to_payload(),
            "soft_limit_snapshot": None if self.reward_package.soft_limit is None else self.reward_package.soft_limit.to_payload(),
            "qualification_granted": self.qualification_granted,
            "resulting_progress_status": None if self.resulting_progress_status is None else self.resulting_progress_status.value,
            "occurred_at": occurred_at,
        }


def scale_reward_quantity(quantity: int, ratio: Decimal) -> int:
    """按倍率缩放奖励数量，至少保留 1 份基础奖励。"""
    if quantity <= 0:
        raise ValueError("quantity 必须大于 0")
    if ratio <= 0:
        raise ValueError("ratio 必须大于 0")
    scaled = (Decimal(quantity) * ratio).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return max(1, int(scaled))


__all__ = [
    "BreakthroughProgressSnapshot",
    "BreakthroughRewardCycleType",
    "BreakthroughRewardDirection",
    "BreakthroughRewardItem",
    "BreakthroughRewardKind",
    "BreakthroughRewardPackage",
    "BreakthroughSettlementResult",
    "BreakthroughSettlementType",
    "BreakthroughSoftLimitSnapshot",
    "BreakthroughTrialProgressStatus",
    "scale_reward_quantity",
]
