"""阶段 7 突破秘境领域规则测试。"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from domain.breakthrough import (
    BreakthroughProgressSnapshot,
    BreakthroughRewardDirection,
    BreakthroughRewardKind,
    BreakthroughRuleError,
    BreakthroughRuleService,
    BreakthroughSettlementType,
    BreakthroughTrialProgressStatus,
)
from infrastructure.config.static import load_static_config



def _build_progress_snapshot(
    *,
    mapping_id: str,
    status: BreakthroughTrialProgressStatus | None,
    attempt_count: int = 0,
    cleared_count: int = 0,
) -> BreakthroughProgressSnapshot:
    """构造突破关卡只读进度快照，便于聚焦规则判定。"""
    return BreakthroughProgressSnapshot(
        mapping_id=mapping_id,
        status=status,
        attempt_count=attempt_count,
        cleared_count=cleared_count,
    )



def test_resolve_first_clear_grants_qualification_for_current_realm_trial() -> None:
    """当前大境界对应关卡首通后应发放突破资格。"""
    rule_service = BreakthroughRuleService(load_static_config())
    progress = _build_progress_snapshot(
        mapping_id="foundation_to_core",
        status=BreakthroughTrialProgressStatus.FAILED,
        attempt_count=1,
    )

    result = rule_service.resolve_first_clear(
        current_realm_id="foundation",
        progress=progress,
        occurred_at="2026-03-26T20:00:00",
    )

    assert result.settlement_type is BreakthroughSettlementType.FIRST_CLEAR
    assert result.victory is True
    assert result.qualification_granted is True
    assert result.resulting_progress_status is BreakthroughTrialProgressStatus.CLEARED
    assert len(result.reward_package.items) == 1
    assert result.reward_package.items[0].reward_kind is BreakthroughRewardKind.QUALIFICATION



def test_resolve_repeat_clear_does_not_grant_qualification_again() -> None:
    """已首通过的关卡再次胜利时只应进入重复奖励结算。"""
    rule_service = BreakthroughRuleService(load_static_config())
    progress = _build_progress_snapshot(
        mapping_id="foundation_to_core",
        status=BreakthroughTrialProgressStatus.CLEARED,
        attempt_count=3,
        cleared_count=1,
    )

    result = rule_service.resolve_repeat_clear(
        mapping_id="foundation_to_core",
        progress=progress,
        cycle_anchor=date(2026, 3, 26),
        consumed_count_before=0,
    )

    assert result.settlement_type is BreakthroughSettlementType.REPEAT_CLEAR
    assert result.victory is True
    assert result.qualification_granted is False
    assert result.resulting_progress_status is BreakthroughTrialProgressStatus.CLEARED
    assert {item.reward_kind for item in result.reward_package.items} == {BreakthroughRewardKind.MATERIAL}



def test_resolve_defeat_returns_empty_reward_package() -> None:
    """失败不会发放资格或任何奖励。"""
    rule_service = BreakthroughRuleService(load_static_config())

    result = rule_service.resolve_defeat(
        mapping_id="foundation_to_core",
        previous_status=BreakthroughTrialProgressStatus.FAILED,
    )

    assert result.settlement_type is BreakthroughSettlementType.DEFEAT
    assert result.victory is False
    assert result.qualification_granted is False
    assert result.resulting_progress_status is BreakthroughTrialProgressStatus.FAILED
    assert result.reward_package.items == ()
    assert result.reward_package.is_empty() is True



def test_can_challenge_trial_only_allows_current_or_cleared_history() -> None:
    """未首通时只能挑战当前映射，已首通历史关卡可重复挑战，未来关卡不可提前进入。"""
    rule_service = BreakthroughRuleService(load_static_config())
    cleared_mapping_ids = {"qi_refining_to_foundation"}

    assert rule_service.can_challenge_trial(
        current_realm_id="foundation",
        target_mapping_id="foundation_to_core",
        cleared_mapping_ids=cleared_mapping_ids,
    ) is True
    assert rule_service.can_challenge_trial(
        current_realm_id="foundation",
        target_mapping_id="qi_refining_to_foundation",
        cleared_mapping_ids=cleared_mapping_ids,
    ) is True
    assert rule_service.can_challenge_trial(
        current_realm_id="foundation",
        target_mapping_id="core_to_nascent_soul",
        cleared_mapping_ids=cleared_mapping_ids,
    ) is False



def test_resolve_repeat_clear_enters_reduced_yield_after_direction_limit() -> None:
    """超过方向级高收益次数后，应按衰减倍率结算。"""
    rule_service = BreakthroughRuleService(load_static_config())
    progress = _build_progress_snapshot(
        mapping_id="mortal_to_qi_refining",
        status=BreakthroughTrialProgressStatus.CLEARED,
        attempt_count=7,
        cleared_count=1,
    )

    result = rule_service.resolve_repeat_clear(
        mapping_id="mortal_to_qi_refining",
        progress=progress,
        cycle_anchor=date(2026, 3, 26),
        consumed_count_before=6,
    )

    assert result.reward_package.direction is BreakthroughRewardDirection.SPIRIT_STONE
    assert result.reward_package.soft_limit is not None
    assert result.reward_package.soft_limit.entered_reduced_yield is True
    assert result.reward_package.soft_limit.applied_ratio == Decimal("0.40")
    assert result.reward_package.soft_limit.consumed_count_after == 7
    assert len(result.reward_package.items) == 1
    assert result.reward_package.items[0].resource_id == "spirit_stone"
    assert result.reward_package.items[0].quantity == 480



def test_resolve_repeat_clear_uses_same_direction_soft_limit_for_shared_pool_trials() -> None:
    """共享同一方向奖励池的不同关卡，在相同已消耗次数下应落到同一衰减结算。"""
    rule_service = BreakthroughRuleService(load_static_config())
    progress = _build_progress_snapshot(
        mapping_id="core_to_nascent_soul",
        status=BreakthroughTrialProgressStatus.CLEARED,
        attempt_count=5,
        cleared_count=1,
    )

    first_result = rule_service.resolve_repeat_clear(
        mapping_id="core_to_nascent_soul",
        progress=progress,
        cycle_anchor=date(2026, 3, 26),
        consumed_count_before=3,
    )
    second_result = rule_service.resolve_repeat_clear(
        mapping_id="nascent_soul_to_deity_transformation",
        progress=_build_progress_snapshot(
            mapping_id="nascent_soul_to_deity_transformation",
            status=BreakthroughTrialProgressStatus.CLEARED,
            attempt_count=4,
            cleared_count=1,
        ),
        cycle_anchor=date(2026, 3, 26),
        consumed_count_before=3,
    )

    assert first_result.reward_package.direction is BreakthroughRewardDirection.COMPREHENSION_MATERIAL
    assert second_result.reward_package.direction is BreakthroughRewardDirection.COMPREHENSION_MATERIAL
    assert first_result.reward_package.soft_limit is not None
    assert second_result.reward_package.soft_limit is not None
    assert first_result.reward_package.soft_limit.entered_reduced_yield is True
    assert second_result.reward_package.soft_limit.entered_reduced_yield is True
    assert first_result.reward_package.soft_limit.applied_ratio == Decimal("0.55")
    assert second_result.reward_package.soft_limit.applied_ratio == Decimal("0.55")
    assert [item.quantity for item in first_result.reward_package.items] == [1, 1]
    assert [item.quantity for item in second_result.reward_package.items] == [1, 1]



def test_resolve_repeat_clear_rejects_uncleared_trial() -> None:
    """未首通关卡不能直接走重复奖励分支。"""
    rule_service = BreakthroughRuleService(load_static_config())
    progress = _build_progress_snapshot(
        mapping_id="foundation_to_core",
        status=BreakthroughTrialProgressStatus.FAILED,
        attempt_count=1,
    )

    with pytest.raises(BreakthroughRuleError, match="未首通的关卡不能结算重复挑战奖励"):
        rule_service.resolve_repeat_clear(
            mapping_id="foundation_to_core",
            progress=progress,
            cycle_anchor=date(2026, 3, 26),
            consumed_count_before=0,
        )
