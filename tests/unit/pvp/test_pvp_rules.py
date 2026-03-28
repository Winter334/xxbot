"""阶段 9 PVP 领域规则测试。"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

from domain.pvp import (
    PvpBattleOutcome,
    PvpChallengeContext,
    PvpDailyActivitySnapshot,
    PvpDefenseSnapshotState,
    PvpLeaderboardEntry,
    PvpRuleService,
)
from infrastructure.config.static import load_static_config


def _build_rule_service() -> PvpRuleService:
    """构造规则服务。"""
    return PvpRuleService(load_static_config())



def _build_entry(
    *,
    character_id: int,
    rank_position: int,
    public_power_score: int,
    hidden_pvp_score: int,
    realm_id: str = "foundation",
    protected_until: datetime | None = None,
    latest_defense_snapshot_version: int | None = 1,
) -> PvpLeaderboardEntry:
    """构造榜单条目。"""
    return PvpLeaderboardEntry(
        character_id=character_id,
        rank_position=rank_position,
        public_power_score=public_power_score,
        hidden_pvp_score=hidden_pvp_score,
        realm_id=realm_id,
        best_rank=rank_position,
        protected_until=protected_until,
        latest_defense_snapshot_version=latest_defense_snapshot_version,
        challenge_tier="top50",
        reward_preview_tier="top50",
        summary={
            "character_name": f"角色{character_id}",
            "realm_id": realm_id,
            "public_power_score": public_power_score,
            "best_rank": rank_position,
            "score_version": "stage8.v1",
            "challenge_tier": "top50",
            "reward_preview_tier": "top50",
        },
    )



def _build_snapshot_state(
    *,
    character_id: int,
    now: datetime,
    snapshot_version: int = 1,
    lock_expires_at: datetime | None = None,
    build_fingerprint: str | None = None,
    score_version: str = "stage8.v1",
) -> PvpDefenseSnapshotState:
    """构造防守快照状态。"""
    return PvpDefenseSnapshotState(
        character_id=character_id,
        snapshot_id=snapshot_version,
        snapshot_version=snapshot_version,
        build_fingerprint=build_fingerprint or f"fingerprint-{character_id}",
        snapshot_reason="defense_on_demand",
        score_version=score_version,
        rank_position=1,
        public_power_score=1000,
        hidden_pvp_score=1000,
        lock_started_at=now - timedelta(hours=1),
        lock_expires_at=lock_expires_at or now + timedelta(hours=1),
        formation={},
        stats={},
        summary={"character_name": f"角色{character_id}"},
    )



def _build_daily_activity(
    *,
    character_id: int,
    effective_challenge_count: int,
    cycle_anchor_date: date | None = None,
    successful_challenge_count: int = 0,
    defense_failure_count: int = 0,
) -> PvpDailyActivitySnapshot:
    """构造每日活动账本快照。"""
    return PvpDailyActivitySnapshot(
        character_id=character_id,
        cycle_anchor_date=cycle_anchor_date or date(2026, 3, 27),
        effective_challenge_count=effective_challenge_count,
        successful_challenge_count=successful_challenge_count,
        defense_failure_count=defense_failure_count,
    )



def test_build_target_pool_filters_candidates_and_applies_fallback_expansion() -> None:
    """目标池应过滤非法候选，并在候选不足时按配置扩窗。"""
    rule_service = _build_rule_service()
    now = datetime(2026, 3, 27, 12, 0, 0)
    attacker = _build_entry(
        character_id=101,
        rank_position=8,
        public_power_score=1000,
        hidden_pvp_score=1000,
    )
    eligible_a = _build_entry(
        character_id=102,
        rank_position=3,
        public_power_score=1000,
        hidden_pvp_score=1000,
    )
    eligible_b = _build_entry(
        character_id=103,
        rank_position=4,
        public_power_score=1280,
        hidden_pvp_score=1200,
    )
    fallback_target = _build_entry(
        character_id=104,
        rank_position=5,
        public_power_score=1400,
        hidden_pvp_score=1250,
    )
    protected_target = _build_entry(
        character_id=105,
        rank_position=6,
        public_power_score=980,
        hidden_pvp_score=995,
        protected_until=now + timedelta(hours=2),
    )
    snapshot_missing_target = _build_entry(
        character_id=106,
        rank_position=7,
        public_power_score=990,
        hidden_pvp_score=990,
    )

    target_pool = rule_service.build_target_pool(
        attacker=attacker,
        leaderboard_entries=(
            eligible_a,
            eligible_b,
            fallback_target,
            protected_target,
            snapshot_missing_target,
            attacker,
        ),
        defense_snapshot_by_character_id={
            102: _build_snapshot_state(character_id=102, now=now),
            103: _build_snapshot_state(character_id=103, now=now),
            104: _build_snapshot_state(character_id=104, now=now),
            105: _build_snapshot_state(character_id=105, now=now),
        },
        daily_activity_by_character_id={},
        now=now,
    )

    assert [candidate.character_id for candidate in target_pool.candidates] == [102, 103, 104]
    assert target_pool.fallback_triggered is True
    assert target_pool.expansion_steps_applied == (
        "public_power_tolerance_ratio",
        "hidden_score_tolerance_ratio",
    )

    rejected_by_id = {
        candidate.character_id: set(candidate.rejection_reasons)
        for candidate in target_pool.rejected_candidates
    }
    assert rejected_by_id[101] == {"self_target", "missing_active_snapshot"}
    assert rejected_by_id[105] == {"defender_protected"}
    assert rejected_by_id[106] == {"missing_active_snapshot"}



def test_validate_challenge_eligibility_blocks_daily_limit_and_repeat_limit() -> None:
    """达到每日次数上限且对同目标重复挑战达上限时，应同时拦截。"""
    rule_service = _build_rule_service()
    now = datetime(2026, 3, 27, 12, 0, 0)
    cycle_anchor_date = date(2026, 3, 27)
    attacker = _build_entry(
        character_id=201,
        rank_position=8,
        public_power_score=1000,
        hidden_pvp_score=1000,
    )
    defender = _build_entry(
        character_id=202,
        rank_position=5,
        public_power_score=1020,
        hidden_pvp_score=1010,
    )
    defender_snapshot = _build_snapshot_state(character_id=202, now=now)
    target_pool = rule_service.build_target_pool(
        attacker=attacker,
        leaderboard_entries=(defender, attacker),
        defense_snapshot_by_character_id={202: defender_snapshot},
        daily_activity_by_character_id={},
        now=now,
    )
    context = PvpChallengeContext(
        attacker=attacker,
        defender=defender,
        attacker_daily_activity=_build_daily_activity(
            character_id=201,
            cycle_anchor_date=cycle_anchor_date,
            effective_challenge_count=5,
            successful_challenge_count=4,
        ),
        defender_daily_activity=None,
        defender_snapshot_state=defender_snapshot,
        cycle_anchor_date=cycle_anchor_date,
        effective_repeat_count_against_target=2,
        attacker_current_win_streak=1,
    )

    eligibility = rule_service.validate_challenge_eligibility(
        context=context,
        target_pool=target_pool,
        now=now,
    )

    assert eligibility.allowed is False
    assert set(eligibility.block_reasons) == {
        "daily_challenge_limit_reached",
        "repeat_target_limit_reached",
    }
    assert eligibility.daily_quota.remaining_count == 0
    assert eligibility.repeat_target.remaining_count == 0



def test_decide_defense_snapshot_usage_reuses_locked_snapshot_and_refreshes_after_expiry() -> None:
    """锁定期内应复用旧快照，锁定失效后应生成新版本。"""
    rule_service = _build_rule_service()
    now = datetime(2026, 3, 27, 12, 0, 0)
    current_snapshot = _build_snapshot_state(
        character_id=301,
        now=now,
        snapshot_version=3,
        lock_expires_at=now + timedelta(hours=2),
        build_fingerprint="same-build",
    )

    reused = rule_service.decide_defense_snapshot_usage(
        current_snapshot=current_snapshot,
        now=now,
        build_fingerprint="same-build",
        requested_reason="challenge_start",
        score_version="stage8.v1",
    )
    refreshed = rule_service.decide_defense_snapshot_usage(
        current_snapshot=current_snapshot,
        now=now + timedelta(hours=3),
        build_fingerprint="same-build",
        requested_reason="defense_on_demand",
        score_version="stage8.v1",
    )

    assert reused.reuse_existing is True
    assert reused.requires_new_snapshot is False
    assert reused.reason_code == "reuse_active_snapshot"
    assert reused.target_snapshot_version == 3

    assert refreshed.reuse_existing is False
    assert refreshed.requires_new_snapshot is True
    assert refreshed.reason_code == "snapshot_expired"
    assert refreshed.target_snapshot_version == 4
    assert refreshed.lock_expires_at > refreshed.lock_started_at



def test_resolve_rank_change_applies_stable_shift_update() -> None:
    """胜利后应由进攻方占据目标名次，中间角色按顺序顺延。"""
    rule_service = _build_rule_service()
    leaderboard_entries = (
        _build_entry(character_id=401, rank_position=1, public_power_score=1300, hidden_pvp_score=1310),
        _build_entry(character_id=402, rank_position=2, public_power_score=1250, hidden_pvp_score=1260),
        _build_entry(character_id=403, rank_position=3, public_power_score=1200, hidden_pvp_score=1210),
        _build_entry(character_id=404, rank_position=4, public_power_score=1180, hidden_pvp_score=1190),
        _build_entry(character_id=405, rank_position=5, public_power_score=1160, hidden_pvp_score=1170),
    )

    rank_change = rule_service.resolve_rank_change(
        leaderboard_entries=leaderboard_entries,
        attacker_character_id=405,
        defender_character_id=402,
        battle_outcome=PvpBattleOutcome.ALLY_VICTORY,
    )

    assert rank_change.rank_effect_applied is True
    assert rank_change.attacker_rank_before == 5
    assert rank_change.attacker_rank_after == 2
    assert rank_change.defender_rank_before == 2
    assert rank_change.defender_rank_after == 3
    assert rank_change.affected_rank_range is not None
    assert rank_change.affected_rank_range.rank_start == 2
    assert rank_change.affected_rank_range.rank_end == 5
    assert [entry.character_id for entry in rank_change.ordered_entries_after] == [401, 405, 402, 403, 404]
    assert {
        update.character_id: (update.rank_before, update.rank_after)
        for update in rank_change.rank_updates
    } == {
        405: (5, 2),
        402: (2, 3),
        403: (3, 4),
        404: (4, 5),
    }



def test_build_challenge_settlement_contains_honor_coin_preview_and_display_rewards() -> None:
    """单次结算应输出荣誉币结果、奖励预览与展示奖励条目。"""
    rule_service = _build_rule_service()
    now = datetime(2026, 3, 27, 12, 0, 0)
    cycle_anchor_date = date(2026, 3, 27)
    attacker = _build_entry(
        character_id=501,
        rank_position=8,
        public_power_score=1000,
        hidden_pvp_score=1000,
    )
    defender = _build_entry(
        character_id=502,
        rank_position=2,
        public_power_score=1030,
        hidden_pvp_score=1010,
    )
    defender_snapshot = _build_snapshot_state(character_id=502, now=now, snapshot_version=7)
    target_pool = rule_service.build_target_pool(
        attacker=attacker,
        leaderboard_entries=(defender, attacker),
        defense_snapshot_by_character_id={502: defender_snapshot},
        daily_activity_by_character_id={
            502: _build_daily_activity(
                character_id=502,
                cycle_anchor_date=cycle_anchor_date,
                effective_challenge_count=0,
                defense_failure_count=0,
            )
        },
        now=now,
    )
    context = PvpChallengeContext(
        attacker=attacker,
        defender=defender,
        attacker_daily_activity=_build_daily_activity(
            character_id=501,
            cycle_anchor_date=cycle_anchor_date,
            effective_challenge_count=4,
            successful_challenge_count=2,
        ),
        defender_daily_activity=_build_daily_activity(
            character_id=502,
            cycle_anchor_date=cycle_anchor_date,
            effective_challenge_count=0,
            defense_failure_count=0,
        ),
        defender_snapshot_state=defender_snapshot,
        cycle_anchor_date=cycle_anchor_date,
        effective_repeat_count_against_target=1,
        attacker_current_win_streak=2,
    )
    leaderboard_entries = (
        _build_entry(character_id=503, rank_position=1, public_power_score=1200, hidden_pvp_score=1210),
        defender,
        _build_entry(character_id=504, rank_position=3, public_power_score=1100, hidden_pvp_score=1110),
        _build_entry(character_id=505, rank_position=4, public_power_score=1090, hidden_pvp_score=1100),
        _build_entry(character_id=506, rank_position=5, public_power_score=1080, hidden_pvp_score=1090),
        _build_entry(character_id=507, rank_position=6, public_power_score=1070, hidden_pvp_score=1080),
        _build_entry(character_id=508, rank_position=7, public_power_score=1060, hidden_pvp_score=1070),
        attacker,
    )

    settlement = rule_service.build_challenge_settlement(
        context=context,
        target_pool=target_pool,
        leaderboard_entries=leaderboard_entries,
        battle_outcome=PvpBattleOutcome.ALLY_VICTORY,
        now=now,
        balance_before=100,
    )

    assert settlement.rank_change.attacker_rank_after == 2
    assert settlement.honor_coin_settlement.delta == 32
    assert settlement.honor_coin_settlement.balance_after == 132
    assert [component.component_id for component in settlement.honor_coin_settlement.components] == [
        "base",
        "rank_gap_bonus",
        "upset_bonus",
        "streak_bonus",
    ]
    assert settlement.reward_preview is not None
    assert settlement.reward_preview.reward_tier_id == "top3"
    assert settlement.reward_preview.honor_coin_on_win == 32
    assert settlement.reward_preview.honor_coin_on_loss == 6
    assert [item.reward_id for item in settlement.display_rewards] == [
        "top3:title",
        "top3:avatar_frame",
    ]
    assert set(settlement.anti_abuse_flags) == {
        "daily_quota_exhausted",
        "repeat_target_limit_reached",
        "defense_failure_cap_reached",
    }
    assert settlement.defense_snapshot_version == 7
