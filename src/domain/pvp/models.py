"""PVP 领域模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum


class PvpBattleOutcome(StrEnum):
    """PVP 挑战的战斗结果。"""

    ALLY_VICTORY = "ally_victory"
    ENEMY_VICTORY = "enemy_victory"
    DRAW = "draw"


class PvpRewardDisplayType(StrEnum):
    """展示奖励类型。"""

    TITLE = "title"
    BADGE = "badge"
    PANEL_FRAME = "panel_frame"
    AVATAR_FRAME = "avatar_frame"


class PvpRewardState(StrEnum):
    """展示奖励当前状态。"""

    PREVIEW = "preview"
    UNLOCKED_NOW = "unlocked_now"
    OWNED = "owned"


class PvpRewardSource(StrEnum):
    """展示奖励来源。"""

    RANK_TIER = "rank_tier"
    CHALLENGE_SETTLEMENT = "challenge_settlement"


@dataclass(frozen=True, slots=True)
class PvpRankRange:
    """连续名次区间。"""

    rank_start: int
    rank_end: int

    def __post_init__(self) -> None:
        if self.rank_start <= 0:
            raise ValueError("rank_start 必须大于 0")
        if self.rank_end < self.rank_start:
            raise ValueError("rank_end 不能小于 rank_start")

    def contains(self, rank_position: int) -> bool:
        """判断指定名次是否落在当前区间内。"""
        return self.rank_start <= rank_position <= self.rank_end


@dataclass(frozen=True, slots=True)
class PvpLeaderboardEntry:
    """PVP 榜单中的单个角色快照。"""

    character_id: int
    rank_position: int
    public_power_score: int
    hidden_pvp_score: int
    realm_id: str
    best_rank: int | None = None
    protected_until: datetime | None = None
    latest_defense_snapshot_version: int | None = None
    challenge_tier: str | None = None
    reward_preview_tier: str | None = None
    summary: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.character_id <= 0:
            raise ValueError("character_id 必须大于 0")
        if self.rank_position <= 0:
            raise ValueError("rank_position 必须大于 0")
        if self.public_power_score < 0:
            raise ValueError("public_power_score 不能为负数")
        if self.hidden_pvp_score < 0:
            raise ValueError("hidden_pvp_score 不能为负数")
        if not self.realm_id:
            raise ValueError("realm_id 不能为空")
        if self.best_rank is not None and self.best_rank <= 0:
            raise ValueError("best_rank 必须大于 0")
        if self.latest_defense_snapshot_version is not None and self.latest_defense_snapshot_version <= 0:
            raise ValueError("latest_defense_snapshot_version 必须大于 0")

    def is_protected(self, *, now: datetime) -> bool:
        """判断当前角色是否处于新入榜保护期内。"""
        return self.protected_until is not None and self.protected_until > now


@dataclass(frozen=True, slots=True)
class PvpDefenseSnapshotState:
    """防守快照当前状态。"""

    character_id: int
    snapshot_id: int | None = None
    snapshot_version: int | None = None
    build_fingerprint: str | None = None
    snapshot_reason: str | None = None
    score_version: str | None = None
    rank_position: int | None = None
    public_power_score: int = 0
    hidden_pvp_score: int = 0
    lock_started_at: datetime | None = None
    lock_expires_at: datetime | None = None
    formation: dict[str, object] = field(default_factory=dict)
    stats: dict[str, object] = field(default_factory=dict)
    summary: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.character_id <= 0:
            raise ValueError("character_id 必须大于 0")
        if self.snapshot_id is not None and self.snapshot_id <= 0:
            raise ValueError("snapshot_id 必须大于 0")
        if self.snapshot_version is not None and self.snapshot_version <= 0:
            raise ValueError("snapshot_version 必须大于 0")
        if self.rank_position is not None and self.rank_position <= 0:
            raise ValueError("rank_position 必须大于 0")
        if self.public_power_score < 0:
            raise ValueError("public_power_score 不能为负数")
        if self.hidden_pvp_score < 0:
            raise ValueError("hidden_pvp_score 不能为负数")
        if self.lock_started_at is not None and self.lock_expires_at is not None and self.lock_expires_at < self.lock_started_at:
            raise ValueError("lock_expires_at 不能早于 lock_started_at")

    def is_active(self, *, now: datetime) -> bool:
        """判断快照当前是否仍在锁定有效期内。"""
        return self.snapshot_version is not None and self.lock_expires_at is not None and self.lock_expires_at >= now

    def matches_build(self, *, build_fingerprint: str, score_version: str | None = None) -> bool:
        """判断快照是否与目标构筑一致。"""
        if not build_fingerprint or self.build_fingerprint is None:
            return False
        if self.build_fingerprint != build_fingerprint:
            return False
        if score_version is not None and self.score_version != score_version:
            return False
        return True


@dataclass(frozen=True, slots=True)
class PvpDailyActivitySnapshot:
    """PVP 自然日活动账本快照。"""

    character_id: int
    cycle_anchor_date: date
    effective_challenge_count: int
    successful_challenge_count: int = 0
    defense_failure_count: int = 0
    last_challenge_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.character_id <= 0:
            raise ValueError("character_id 必须大于 0")
        if self.effective_challenge_count < 0:
            raise ValueError("effective_challenge_count 不能为负数")
        if self.successful_challenge_count < 0:
            raise ValueError("successful_challenge_count 不能为负数")
        if self.defense_failure_count < 0:
            raise ValueError("defense_failure_count 不能为负数")
        if self.successful_challenge_count > self.effective_challenge_count:
            raise ValueError("successful_challenge_count 不能大于 effective_challenge_count")

    def remaining_effective_challenges(self, *, limit: int) -> int:
        """返回当日剩余有效挑战次数。"""
        if limit < 0:
            raise ValueError("limit 不能为负数")
        return max(0, limit - self.effective_challenge_count)


@dataclass(frozen=True, slots=True)
class PvpChallengeQuotaDecision:
    """每日有效挑战次数校验结果。"""

    allowed: bool
    limit: int
    used_count: int
    remaining_count: int
    reason_code: str | None = None

    def __post_init__(self) -> None:
        if self.limit < 0:
            raise ValueError("limit 不能为负数")
        if self.used_count < 0:
            raise ValueError("used_count 不能为负数")
        if self.remaining_count < 0:
            raise ValueError("remaining_count 不能为负数")
        if self.remaining_count != max(0, self.limit - self.used_count):
            raise ValueError("remaining_count 与 limit、used_count 不一致")
        if self.allowed != (self.used_count < self.limit):
            raise ValueError("allowed 与次数状态不一致")


@dataclass(frozen=True, slots=True)
class PvpRepeatTargetDecision:
    """同目标重复挑战限制校验结果。"""

    allowed: bool
    limit: int
    used_count: int
    remaining_count: int
    reason_code: str | None = None

    def __post_init__(self) -> None:
        if self.limit < 0:
            raise ValueError("limit 不能为负数")
        if self.used_count < 0:
            raise ValueError("used_count 不能为负数")
        if self.remaining_count < 0:
            raise ValueError("remaining_count 不能为负数")
        if self.remaining_count != max(0, self.limit - self.used_count):
            raise ValueError("remaining_count 与 limit、used_count 不一致")
        if self.allowed != (self.used_count < self.limit):
            raise ValueError("allowed 与次数状态不一致")


@dataclass(frozen=True, slots=True)
class PvpDefenseFailureCapDecision:
    """高名次防守失败上限判定结果。"""

    cap: int | None
    used_count: int
    remaining_count: int | None
    cap_reached: bool
    can_record_failure: bool
    reason_code: str | None = None

    def __post_init__(self) -> None:
        if self.used_count < 0:
            raise ValueError("used_count 不能为负数")
        if self.cap is None:
            if self.remaining_count is not None:
                raise ValueError("未配置 cap 时 remaining_count 必须为空")
            if self.cap_reached:
                raise ValueError("未配置 cap 时 cap_reached 必须为 False")
            if not self.can_record_failure:
                raise ValueError("未配置 cap 时 can_record_failure 必须为 True")
            return
        if self.cap <= 0:
            raise ValueError("cap 必须大于 0")
        if self.remaining_count is None:
            raise ValueError("配置 cap 时 remaining_count 不能为空")
        if self.remaining_count != max(0, self.cap - self.used_count):
            raise ValueError("remaining_count 与 cap、used_count 不一致")
        if self.cap_reached != (self.used_count >= self.cap):
            raise ValueError("cap_reached 与次数状态不一致")
        if self.can_record_failure != (self.used_count < self.cap):
            raise ValueError("can_record_failure 与次数状态不一致")


@dataclass(frozen=True, slots=True)
class PvpDefenseSnapshotUsageDecision:
    """防守快照锁定与复用判定结果。"""

    requested_reason: str
    resolved_reason: str
    reason_code: str
    reuse_existing: bool
    requires_new_snapshot: bool
    current_snapshot_version: int | None
    target_snapshot_version: int
    build_changed: bool
    lock_started_at: datetime
    lock_expires_at: datetime

    def __post_init__(self) -> None:
        if not self.requested_reason:
            raise ValueError("requested_reason 不能为空")
        if not self.resolved_reason:
            raise ValueError("resolved_reason 不能为空")
        if not self.reason_code:
            raise ValueError("reason_code 不能为空")
        if self.target_snapshot_version <= 0:
            raise ValueError("target_snapshot_version 必须大于 0")
        if self.current_snapshot_version is not None and self.current_snapshot_version <= 0:
            raise ValueError("current_snapshot_version 必须大于 0")
        if self.lock_expires_at < self.lock_started_at:
            raise ValueError("lock_expires_at 不能早于 lock_started_at")
        if self.reuse_existing == self.requires_new_snapshot:
            raise ValueError("reuse_existing 与 requires_new_snapshot 必须一真一假")
        if self.reuse_existing and self.current_snapshot_version != self.target_snapshot_version:
            raise ValueError("复用快照时 target_snapshot_version 必须等于 current_snapshot_version")
        if self.reuse_existing and self.current_snapshot_version is None:
            raise ValueError("复用快照时 current_snapshot_version 不能为空")


@dataclass(frozen=True, slots=True)
class PvpTargetCandidate:
    """单个目标池候选项。"""

    leaderboard_entry: PvpLeaderboardEntry
    defense_snapshot_state: PvpDefenseSnapshotState | None
    daily_activity: PvpDailyActivitySnapshot | None
    rank_gap: int
    realm_gap: int
    public_power_ratio_delta: Decimal
    hidden_score_ratio_delta: Decimal
    protected: bool
    has_active_defense_snapshot: bool
    defense_failure_cap: int | None
    defense_failure_cap_reached: bool
    rejection_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.realm_gap < 0:
            raise ValueError("realm_gap 不能为负数")
        if self.public_power_ratio_delta < 0:
            raise ValueError("public_power_ratio_delta 不能为负数")
        if self.hidden_score_ratio_delta < 0:
            raise ValueError("hidden_score_ratio_delta 不能为负数")
        if self.defense_failure_cap is not None and self.defense_failure_cap <= 0:
            raise ValueError("defense_failure_cap 必须大于 0")

    @property
    def character_id(self) -> int:
        """返回候选角色标识。"""
        return self.leaderboard_entry.character_id

    @property
    def rank_position(self) -> int:
        """返回候选角色当前名次。"""
        return self.leaderboard_entry.rank_position

    @property
    def is_eligible(self) -> bool:
        """判断当前候选项是否通过全部过滤规则。"""
        return not self.rejection_reasons


@dataclass(frozen=True, slots=True)
class PvpTargetPoolResult:
    """目标池筛选与扩窗后的最终结果。"""

    attacker_character_id: int
    candidates: tuple[PvpTargetCandidate, ...]
    rejected_candidates: tuple[PvpTargetCandidate, ...]
    applied_rank_window_up: int
    applied_rank_window_down: int
    applied_public_power_tolerance_ratio: Decimal
    applied_hidden_score_tolerance_ratio: Decimal
    fallback_min_candidate_count: int
    expansion_steps_applied: tuple[str, ...] = ()
    fallback_triggered: bool = False

    def __post_init__(self) -> None:
        if self.attacker_character_id <= 0:
            raise ValueError("attacker_character_id 必须大于 0")
        if self.applied_rank_window_up <= 0:
            raise ValueError("applied_rank_window_up 必须大于 0")
        if self.applied_rank_window_down <= 0:
            raise ValueError("applied_rank_window_down 必须大于 0")
        if self.applied_public_power_tolerance_ratio < 0:
            raise ValueError("applied_public_power_tolerance_ratio 不能为负数")
        if self.applied_hidden_score_tolerance_ratio < 0:
            raise ValueError("applied_hidden_score_tolerance_ratio 不能为负数")
        if self.fallback_min_candidate_count <= 0:
            raise ValueError("fallback_min_candidate_count 必须大于 0")

    @property
    def eligible_character_ids(self) -> tuple[int, ...]:
        """返回可挑战角色标识集合。"""
        return tuple(candidate.character_id for candidate in self.candidates)

    def contains_character(self, character_id: int) -> bool:
        """判断指定角色是否仍在合法目标池中。"""
        return any(candidate.character_id == character_id for candidate in self.candidates)


@dataclass(frozen=True, slots=True)
class PvpChallengeContext:
    """单次挑战规则计算所需的输入聚合。"""

    attacker: PvpLeaderboardEntry
    defender: PvpLeaderboardEntry
    attacker_daily_activity: PvpDailyActivitySnapshot
    defender_daily_activity: PvpDailyActivitySnapshot | None
    defender_snapshot_state: PvpDefenseSnapshotState | None
    cycle_anchor_date: date
    effective_repeat_count_against_target: int
    attacker_current_win_streak: int = 0

    def __post_init__(self) -> None:
        if self.attacker.character_id == self.defender.character_id:
            raise ValueError("攻击方与防守方不能相同")
        if self.attacker_daily_activity.character_id != self.attacker.character_id:
            raise ValueError("attacker_daily_activity 与 attacker 不匹配")
        if self.attacker_daily_activity.cycle_anchor_date != self.cycle_anchor_date:
            raise ValueError("攻击方活动账本自然日锚点不匹配")
        if self.defender_daily_activity is not None:
            if self.defender_daily_activity.character_id != self.defender.character_id:
                raise ValueError("defender_daily_activity 与 defender 不匹配")
            if self.defender_daily_activity.cycle_anchor_date != self.cycle_anchor_date:
                raise ValueError("防守方活动账本自然日锚点不匹配")
        if self.defender_snapshot_state is not None and self.defender_snapshot_state.character_id != self.defender.character_id:
            raise ValueError("defender_snapshot_state 与 defender 不匹配")
        if self.effective_repeat_count_against_target < 0:
            raise ValueError("effective_repeat_count_against_target 不能为负数")
        if self.attacker_current_win_streak < 0:
            raise ValueError("attacker_current_win_streak 不能为负数")


@dataclass(frozen=True, slots=True)
class PvpChallengeEligibility:
    """一次挑战在规则层的综合资格判定。"""

    allowed: bool
    block_reasons: tuple[str, ...]
    daily_quota: PvpChallengeQuotaDecision
    repeat_target: PvpRepeatTargetDecision
    target_in_pool: bool
    defender_protected: bool
    defender_snapshot_available: bool
    defense_failure_cap: PvpDefenseFailureCapDecision

    def __post_init__(self) -> None:
        if self.allowed != (len(self.block_reasons) == 0):
            raise ValueError("allowed 与 block_reasons 不一致")


@dataclass(frozen=True, slots=True)
class PvpRankPositionUpdate:
    """单个角色的名次变动明细。"""

    character_id: int
    rank_before: int
    rank_after: int

    def __post_init__(self) -> None:
        if self.character_id <= 0:
            raise ValueError("character_id 必须大于 0")
        if self.rank_before <= 0:
            raise ValueError("rank_before 必须大于 0")
        if self.rank_after <= 0:
            raise ValueError("rank_after 必须大于 0")

    @property
    def rank_shift(self) -> int:
        """返回名次变化值，正数表示名次提升。"""
        return self.rank_before - self.rank_after


@dataclass(frozen=True, slots=True)
class PvpRankChange:
    """挑战结算后的榜单名次变化。"""

    attacker_character_id: int
    defender_character_id: int
    attacker_rank_before: int
    defender_rank_before: int
    attacker_rank_after: int
    defender_rank_after: int
    rank_effect_applied: bool
    affected_rank_range: PvpRankRange | None = None
    rank_updates: tuple[PvpRankPositionUpdate, ...] = ()
    ordered_entries_after: tuple[PvpLeaderboardEntry, ...] = ()

    def __post_init__(self) -> None:
        if self.attacker_character_id <= 0:
            raise ValueError("attacker_character_id 必须大于 0")
        if self.defender_character_id <= 0:
            raise ValueError("defender_character_id 必须大于 0")
        if self.attacker_rank_before <= 0 or self.defender_rank_before <= 0:
            raise ValueError("结算前名次必须大于 0")
        if self.attacker_rank_after <= 0 or self.defender_rank_after <= 0:
            raise ValueError("结算后名次必须大于 0")
        if not self.rank_effect_applied:
            if self.attacker_rank_before != self.attacker_rank_after:
                raise ValueError("未生效名次更新时攻击方名次不能变化")
            if self.defender_rank_before != self.defender_rank_after:
                raise ValueError("未生效名次更新时防守方名次不能变化")
            if self.affected_rank_range is not None:
                raise ValueError("未生效名次更新时 affected_rank_range 必须为空")
        if self.ordered_entries_after:
            ranked_entries = sorted(self.ordered_entries_after, key=lambda entry: entry.rank_position)
            expected_ranks = list(range(1, len(ranked_entries) + 1))
            actual_ranks = [entry.rank_position for entry in ranked_entries]
            if actual_ranks != expected_ranks:
                raise ValueError("ordered_entries_after 的名次必须连续且从 1 开始")
            character_ids = [entry.character_id for entry in ranked_entries]
            if len(character_ids) != len(set(character_ids)):
                raise ValueError("ordered_entries_after 中 character_id 不能重复")

    @property
    def attacker_rank_shift(self) -> int:
        """返回攻击方名次变化值，正数表示名次提升。"""
        return self.attacker_rank_before - self.attacker_rank_after

    @property
    def defender_rank_shift(self) -> int:
        """返回防守方名次变化值，正数表示名次提升。"""
        return self.defender_rank_before - self.defender_rank_after


@dataclass(frozen=True, slots=True)
class PvpHonorCoinComponentResult:
    """荣誉币单个计算组件的结果。"""

    component_id: str
    configured_delta: int
    applied_delta: int
    summary: str
    triggered: bool = True

    def __post_init__(self) -> None:
        if not self.component_id:
            raise ValueError("component_id 不能为空")
        if self.configured_delta < 0:
            raise ValueError("configured_delta 不能为负数")
        if self.applied_delta < 0:
            raise ValueError("applied_delta 不能为负数")
        if not self.summary:
            raise ValueError("summary 不能为空")


@dataclass(frozen=True, slots=True)
class PvpRewardDisplayItem:
    """展示层可消费的单条奖励描述。"""

    reward_id: str
    reward_type: PvpRewardDisplayType
    name: str
    rarity: str
    state: PvpRewardState
    source: PvpRewardSource
    meta: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.reward_id:
            raise ValueError("reward_id 不能为空")
        if not self.name:
            raise ValueError("name 不能为空")
        if not self.rarity:
            raise ValueError("rarity 不能为空")


@dataclass(frozen=True, slots=True)
class PvpRewardPreview:
    """PVP 奖励预览结构。"""

    reward_tier_id: str
    rank_range: PvpRankRange
    honor_coin_on_win: int
    honor_coin_on_loss: int
    display_items: tuple[PvpRewardDisplayItem, ...]
    summary: str

    def __post_init__(self) -> None:
        if not self.reward_tier_id:
            raise ValueError("reward_tier_id 不能为空")
        if self.honor_coin_on_win < 0:
            raise ValueError("honor_coin_on_win 不能为负数")
        if self.honor_coin_on_loss < 0:
            raise ValueError("honor_coin_on_loss 不能为负数")
        if not self.summary:
            raise ValueError("summary 不能为空")


@dataclass(frozen=True, slots=True)
class PvpHonorCoinSettlement:
    """荣誉币结算结果。"""

    battle_outcome: PvpBattleOutcome
    rank_gap: int
    delta: int
    balance_before: int | None = None
    balance_after: int | None = None
    components: tuple[PvpHonorCoinComponentResult, ...] = ()
    reward_preview: PvpRewardPreview | None = None

    def __post_init__(self) -> None:
        if self.rank_gap < 0:
            raise ValueError("rank_gap 不能为负数")
        if self.delta < 0:
            raise ValueError("delta 不能为负数")
        if self.balance_before is None and self.balance_after is not None:
            raise ValueError("balance_before 为空时 balance_after 也必须为空")
        if self.balance_before is not None:
            if self.balance_before < 0:
                raise ValueError("balance_before 不能为负数")
            if self.balance_after is None:
                raise ValueError("声明 balance_before 时 balance_after 不能为空")
            if self.balance_after < self.balance_before:
                raise ValueError("balance_after 不能小于 balance_before")
            if self.balance_after - self.balance_before != self.delta:
                raise ValueError("balance_after 与 balance_before 的差值必须等于 delta")

    @property
    def victory(self) -> bool:
        """判断当前结算是否为攻击方胜利。"""
        return self.battle_outcome is PvpBattleOutcome.ALLY_VICTORY


@dataclass(frozen=True, slots=True)
class PvpChallengeSettlement:
    """单次 PVP 挑战的结构化领域输出。"""

    attacker_character_id: int
    defender_character_id: int
    battle_outcome: PvpBattleOutcome
    rank_change: PvpRankChange
    honor_coin_settlement: PvpHonorCoinSettlement
    reward_preview: PvpRewardPreview | None
    display_rewards: tuple[PvpRewardDisplayItem, ...]
    anti_abuse_flags: tuple[str, ...] = ()
    defense_snapshot_version: int | None = None

    def __post_init__(self) -> None:
        if self.attacker_character_id <= 0:
            raise ValueError("attacker_character_id 必须大于 0")
        if self.defender_character_id <= 0:
            raise ValueError("defender_character_id 必须大于 0")
        if self.defense_snapshot_version is not None and self.defense_snapshot_version <= 0:
            raise ValueError("defense_snapshot_version 必须大于 0")


__all__ = [
    "PvpBattleOutcome",
    "PvpChallengeContext",
    "PvpChallengeEligibility",
    "PvpChallengeQuotaDecision",
    "PvpChallengeSettlement",
    "PvpDailyActivitySnapshot",
    "PvpDefenseFailureCapDecision",
    "PvpDefenseSnapshotState",
    "PvpDefenseSnapshotUsageDecision",
    "PvpHonorCoinComponentResult",
    "PvpHonorCoinSettlement",
    "PvpLeaderboardEntry",
    "PvpRankChange",
    "PvpRankPositionUpdate",
    "PvpRankRange",
    "PvpRepeatTargetDecision",
    "PvpRewardDisplayItem",
    "PvpRewardDisplayType",
    "PvpRewardPreview",
    "PvpRewardSource",
    "PvpRewardState",
    "PvpTargetCandidate",
    "PvpTargetPoolResult",
]
