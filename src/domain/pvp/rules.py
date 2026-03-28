"""PVP 领域规则。"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP

from infrastructure.config.static.models.common import StaticGameConfig
from infrastructure.config.static.models.pvp import PvpConfig, PvpHonorCoinComponent, PvpRewardTierDefinition

from domain.pvp.models import (
    PvpBattleOutcome,
    PvpChallengeContext,
    PvpChallengeEligibility,
    PvpChallengeQuotaDecision,
    PvpChallengeSettlement,
    PvpDailyActivitySnapshot,
    PvpDefenseFailureCapDecision,
    PvpDefenseSnapshotState,
    PvpDefenseSnapshotUsageDecision,
    PvpHonorCoinComponentResult,
    PvpHonorCoinSettlement,
    PvpLeaderboardEntry,
    PvpRankChange,
    PvpRankPositionUpdate,
    PvpRankRange,
    PvpRepeatTargetDecision,
    PvpRewardDisplayItem,
    PvpRewardDisplayType,
    PvpRewardPreview,
    PvpRewardSource,
    PvpRewardState,
    PvpTargetCandidate,
    PvpTargetPoolResult,
)

_RATIO_PRECISION = Decimal("0.0001")
_DEFAULT_DISPLAY_LIBRARY: dict[str, tuple[tuple[PvpRewardDisplayType, str, str], ...]] = {
    "top3": (
        (PvpRewardDisplayType.TITLE, "天榜前三称号", "legendary"),
        (PvpRewardDisplayType.AVATAR_FRAME, "天榜尊荣头像框", "legendary"),
    ),
    "top10": (
        (PvpRewardDisplayType.BADGE, "天榜前十徽记", "epic"),
        (PvpRewardDisplayType.PANEL_FRAME, "天榜前十面板边框", "epic"),
    ),
    "top50": (
        (PvpRewardDisplayType.BADGE, "天榜前五十徽记", "rare"),
    ),
}


class PvpRuleError(ValueError):
    """PVP 规则输入不合法。"""


class PvpRuleService:
    """封装阶段 9 所需的纯领域规则。"""

    def __init__(self, static_config: StaticGameConfig) -> None:
        self._static_config = static_config
        self._config: PvpConfig = static_config.pvp
        self._realm_order_by_id = {
            realm.realm_id: realm.order for realm in static_config.realm_progression.realms
        }
        self._honor_components_by_id = {
            component.component_id: component
            for component in static_config.pvp.honor_coin.components
        }
        self._reward_tiers = static_config.pvp.ordered_reward_tiers
        self._reward_tier_by_id = {
            tier.reward_tier_id: tier for tier in self._reward_tiers
        }
        self._reward_tier_order_by_id = {
            tier.reward_tier_id: index for index, tier in enumerate(self._reward_tiers, start=1)
        }

    @property
    def defense_snapshot_lock_duration(self) -> timedelta:
        """返回防守快照锁定时长。"""
        return timedelta(hours=self._config.protection.defense_snapshot_lock_hours)

    def resolve_new_entry_protected_until(self, *, now: datetime) -> datetime:
        """计算新入榜角色的保护截止时间。"""
        return now + timedelta(hours=self._config.protection.new_entry_protection_hours)

    def check_daily_challenge_limit(self, *, activity: PvpDailyActivitySnapshot) -> PvpChallengeQuotaDecision:
        """校验当日有效挑战次数是否仍可继续。"""
        limit = self._config.daily_limit.effective_challenge_limit
        used_count = activity.effective_challenge_count
        remaining_count = max(0, limit - used_count)
        allowed = used_count < limit
        return PvpChallengeQuotaDecision(
            allowed=allowed,
            limit=limit,
            used_count=used_count,
            remaining_count=remaining_count,
            reason_code=None if allowed else "daily_challenge_limit_reached",
        )

    def check_repeat_target_limit(self, *, used_count: int) -> PvpRepeatTargetDecision:
        """校验同一目标的当日重复挑战次数上限。"""
        if used_count < 0:
            raise PvpRuleError("used_count 不能为负数")
        limit = self._config.daily_limit.repeat_target_limit
        remaining_count = max(0, limit - used_count)
        allowed = used_count < limit
        return PvpRepeatTargetDecision(
            allowed=allowed,
            limit=limit,
            used_count=used_count,
            remaining_count=remaining_count,
            reason_code=None if allowed else "repeat_target_limit_reached",
        )

    def resolve_defense_failure_cap(
        self,
        *,
        rank_position: int,
        defense_failure_count: int,
    ) -> PvpDefenseFailureCapDecision:
        """按当前名次判定高名次防守失败上限。"""
        if rank_position <= 0:
            raise PvpRuleError("rank_position 必须大于 0")
        if defense_failure_count < 0:
            raise PvpRuleError("defense_failure_count 不能为负数")

        for cap_entry in self._config.ordered_defense_failure_caps:
            if cap_entry.rank_start <= rank_position <= cap_entry.rank_end:
                remaining_count = max(0, cap_entry.daily_failure_cap - defense_failure_count)
                cap_reached = defense_failure_count >= cap_entry.daily_failure_cap
                return PvpDefenseFailureCapDecision(
                    cap=cap_entry.daily_failure_cap,
                    used_count=defense_failure_count,
                    remaining_count=remaining_count,
                    cap_reached=cap_reached,
                    can_record_failure=not cap_reached,
                    reason_code=None if not cap_reached else "defense_failure_cap_reached",
                )
        return PvpDefenseFailureCapDecision(
            cap=None,
            used_count=defense_failure_count,
            remaining_count=None,
            cap_reached=False,
            can_record_failure=True,
            reason_code=None,
        )

    def build_target_pool(
        self,
        *,
        attacker: PvpLeaderboardEntry,
        leaderboard_entries: tuple[PvpLeaderboardEntry, ...],
        defense_snapshot_by_character_id: dict[int, PvpDefenseSnapshotState] | None,
        daily_activity_by_character_id: dict[int, PvpDailyActivitySnapshot] | None,
        now: datetime,
    ) -> PvpTargetPoolResult:
        """按设计约束筛选并扩窗生成目标池。"""
        if attacker.character_id <= 0:
            raise PvpRuleError("attacker.character_id 必须大于 0")
        if not leaderboard_entries:
            return PvpTargetPoolResult(
                attacker_character_id=attacker.character_id,
                candidates=(),
                rejected_candidates=(),
                applied_rank_window_up=self._config.target_pool.rank_window_up,
                applied_rank_window_down=self._config.target_pool.rank_window_down,
                applied_public_power_tolerance_ratio=self._config.target_pool.public_power_tolerance_ratio,
                applied_hidden_score_tolerance_ratio=self._config.target_pool.hidden_score_tolerance_ratio,
                fallback_min_candidate_count=self._config.target_pool.fallback_min_candidate_count,
            )

        defense_snapshot_by_character_id = defense_snapshot_by_character_id or {}
        daily_activity_by_character_id = daily_activity_by_character_id or {}
        rank_window_up = self._config.target_pool.rank_window_up
        rank_window_down = self._config.target_pool.rank_window_down
        public_tolerance = self._config.target_pool.public_power_tolerance_ratio
        hidden_tolerance = self._config.target_pool.hidden_score_tolerance_ratio
        expansion_steps_applied: list[str] = []

        final_candidates, final_rejected = self._filter_target_candidates(
            attacker=attacker,
            leaderboard_entries=leaderboard_entries,
            defense_snapshot_by_character_id=defense_snapshot_by_character_id,
            daily_activity_by_character_id=daily_activity_by_character_id,
            now=now,
            rank_window_up=rank_window_up,
            rank_window_down=rank_window_down,
            public_tolerance=public_tolerance,
            hidden_tolerance=hidden_tolerance,
        )

        for step in self._config.target_pool.expansion_order:
            if len(final_candidates) >= self._config.target_pool.fallback_min_candidate_count:
                break
            if step == "public_power_tolerance_ratio":
                public_tolerance = self._config.target_pool.fallback_public_power_tolerance_ratio
            elif step == "hidden_score_tolerance_ratio":
                hidden_tolerance = self._config.target_pool.fallback_hidden_score_tolerance_ratio
            elif step == "rank_window_expand_step":
                rank_window_up += self._config.target_pool.rank_window_expand_step
                rank_window_down += self._config.target_pool.rank_window_expand_step
            else:
                raise PvpRuleError(f"未知目标池扩窗步骤：{step}")
            expansion_steps_applied.append(step)
            final_candidates, final_rejected = self._filter_target_candidates(
                attacker=attacker,
                leaderboard_entries=leaderboard_entries,
                defense_snapshot_by_character_id=defense_snapshot_by_character_id,
                daily_activity_by_character_id=daily_activity_by_character_id,
                now=now,
                rank_window_up=rank_window_up,
                rank_window_down=rank_window_down,
                public_tolerance=public_tolerance,
                hidden_tolerance=hidden_tolerance,
            )

        sorted_candidates = tuple(
            sorted(
                final_candidates,
                key=lambda candidate: (
                    candidate.rank_position,
                    -candidate.rank_gap,
                    candidate.public_power_ratio_delta,
                    candidate.hidden_score_ratio_delta,
                    candidate.character_id,
                ),
            )
        )
        sorted_rejected = tuple(
            sorted(
                final_rejected,
                key=lambda candidate: (
                    candidate.rank_position,
                    candidate.character_id,
                ),
            )
        )
        return PvpTargetPoolResult(
            attacker_character_id=attacker.character_id,
            candidates=sorted_candidates,
            rejected_candidates=sorted_rejected,
            applied_rank_window_up=rank_window_up,
            applied_rank_window_down=rank_window_down,
            applied_public_power_tolerance_ratio=public_tolerance,
            applied_hidden_score_tolerance_ratio=hidden_tolerance,
            fallback_min_candidate_count=self._config.target_pool.fallback_min_candidate_count,
            expansion_steps_applied=tuple(expansion_steps_applied),
            fallback_triggered=bool(expansion_steps_applied),
        )

    def validate_challenge_eligibility(
        self,
        *,
        context: PvpChallengeContext,
        target_pool: PvpTargetPoolResult,
        now: datetime,
    ) -> PvpChallengeEligibility:
        """对单次挑战执行综合资格校验。"""
        daily_quota = self.check_daily_challenge_limit(activity=context.attacker_daily_activity)
        repeat_target = self.check_repeat_target_limit(used_count=context.effective_repeat_count_against_target)
        target_in_pool = target_pool.contains_character(context.defender.character_id)
        defender_protected = context.defender.is_protected(now=now)
        defender_snapshot_available = (
            context.defender_snapshot_state is not None and context.defender_snapshot_state.is_active(now=now)
        )
        defender_failure_count = 0
        if context.defender_daily_activity is not None:
            defender_failure_count = context.defender_daily_activity.defense_failure_count
        defense_failure_cap = self.resolve_defense_failure_cap(
            rank_position=context.defender.rank_position,
            defense_failure_count=defender_failure_count,
        )

        block_reasons: list[str] = []
        if not daily_quota.allowed:
            block_reasons.append(daily_quota.reason_code or "daily_challenge_limit_reached")
        if not repeat_target.allowed:
            block_reasons.append(repeat_target.reason_code or "repeat_target_limit_reached")
        if not target_in_pool:
            block_reasons.append("target_not_in_pool")
        if defender_protected:
            block_reasons.append("defender_protected")
        if not defender_snapshot_available:
            block_reasons.append("missing_active_snapshot")
        if not defense_failure_cap.can_record_failure:
            block_reasons.append(defense_failure_cap.reason_code or "defense_failure_cap_reached")

        return PvpChallengeEligibility(
            allowed=not block_reasons,
            block_reasons=tuple(block_reasons),
            daily_quota=daily_quota,
            repeat_target=repeat_target,
            target_in_pool=target_in_pool,
            defender_protected=defender_protected,
            defender_snapshot_available=defender_snapshot_available,
            defense_failure_cap=defense_failure_cap,
        )

    def decide_defense_snapshot_usage(
        self,
        *,
        current_snapshot: PvpDefenseSnapshotState | None,
        now: datetime,
        build_fingerprint: str,
        requested_reason: str,
        score_version: str | None,
    ) -> PvpDefenseSnapshotUsageDecision:
        """判定防守快照当前应复用还是生成新版本。"""
        if not build_fingerprint:
            raise PvpRuleError("build_fingerprint 不能为空")
        if requested_reason not in set(self._config.anti_abuse.allowed_snapshot_reasons):
            raise PvpRuleError(f"不支持的快照抓取原因：{requested_reason}")

        if current_snapshot is not None and current_snapshot.is_active(now=now):
            if current_snapshot.matches_build(build_fingerprint=build_fingerprint, score_version=score_version):
                assert current_snapshot.snapshot_version is not None
                assert current_snapshot.lock_started_at is not None
                assert current_snapshot.lock_expires_at is not None
                return PvpDefenseSnapshotUsageDecision(
                    requested_reason=requested_reason,
                    resolved_reason=current_snapshot.snapshot_reason or requested_reason,
                    reason_code="reuse_active_snapshot",
                    reuse_existing=True,
                    requires_new_snapshot=False,
                    current_snapshot_version=current_snapshot.snapshot_version,
                    target_snapshot_version=current_snapshot.snapshot_version,
                    build_changed=False,
                    lock_started_at=current_snapshot.lock_started_at,
                    lock_expires_at=current_snapshot.lock_expires_at,
                )

        current_snapshot_version = None if current_snapshot is None else current_snapshot.snapshot_version
        build_changed = current_snapshot is not None and not current_snapshot.matches_build(
            build_fingerprint=build_fingerprint,
            score_version=score_version,
        )
        if current_snapshot is None or current_snapshot.snapshot_version is None:
            resolved_reason = requested_reason
            reason_code = "missing_snapshot"
        elif current_snapshot.lock_expires_at is not None and current_snapshot.lock_expires_at < now:
            resolved_reason = "expired_refresh"
            reason_code = "snapshot_expired"
        elif build_changed:
            resolved_reason = "build_changed"
            reason_code = "build_changed"
        else:
            resolved_reason = requested_reason
            reason_code = "snapshot_refresh_required"

        target_snapshot_version = (current_snapshot_version or 0) + 1
        return PvpDefenseSnapshotUsageDecision(
            requested_reason=requested_reason,
            resolved_reason=resolved_reason,
            reason_code=reason_code,
            reuse_existing=False,
            requires_new_snapshot=True,
            current_snapshot_version=current_snapshot_version,
            target_snapshot_version=target_snapshot_version,
            build_changed=build_changed,
            lock_started_at=now,
            lock_expires_at=now + self.defense_snapshot_lock_duration,
        )

    def resolve_rank_change(
        self,
        *,
        leaderboard_entries: tuple[PvpLeaderboardEntry, ...],
        attacker_character_id: int,
        defender_character_id: int,
        battle_outcome: PvpBattleOutcome,
    ) -> PvpRankChange:
        """按稳定顺延规则计算挑战后的名次变化。"""
        ordered_entries = self._normalize_leaderboard_entries(leaderboard_entries)
        attacker_before = self._get_entry_by_character_id(ordered_entries, attacker_character_id)
        defender_before = self._get_entry_by_character_id(ordered_entries, defender_character_id)

        if battle_outcome is not PvpBattleOutcome.ALLY_VICTORY or attacker_before.rank_position <= defender_before.rank_position:
            return PvpRankChange(
                attacker_character_id=attacker_character_id,
                defender_character_id=defender_character_id,
                attacker_rank_before=attacker_before.rank_position,
                defender_rank_before=defender_before.rank_position,
                attacker_rank_after=attacker_before.rank_position,
                defender_rank_after=defender_before.rank_position,
                rank_effect_applied=False,
                affected_rank_range=None,
                rank_updates=(),
                ordered_entries_after=ordered_entries,
            )

        attacker_index = next(index for index, entry in enumerate(ordered_entries) if entry.character_id == attacker_character_id)
        defender_index = next(index for index, entry in enumerate(ordered_entries) if entry.character_id == defender_character_id)
        reordered_entries = list(ordered_entries)
        attacker_entry = reordered_entries.pop(attacker_index)
        reordered_entries.insert(defender_index, attacker_entry)

        updated_entries: list[PvpLeaderboardEntry] = []
        updates: list[PvpRankPositionUpdate] = []
        for rank_position, entry in enumerate(reordered_entries, start=1):
            updated_entry = self._replace_entry_rank(entry=entry, rank_position=rank_position)
            updated_entries.append(updated_entry)
            if updated_entry.rank_position != entry.rank_position:
                updates.append(
                    PvpRankPositionUpdate(
                        character_id=entry.character_id,
                        rank_before=entry.rank_position,
                        rank_after=updated_entry.rank_position,
                    )
                )

        updated_attacker = self._get_entry_by_character_id(tuple(updated_entries), attacker_character_id)
        updated_defender = self._get_entry_by_character_id(tuple(updated_entries), defender_character_id)
        return PvpRankChange(
            attacker_character_id=attacker_character_id,
            defender_character_id=defender_character_id,
            attacker_rank_before=attacker_before.rank_position,
            defender_rank_before=defender_before.rank_position,
            attacker_rank_after=updated_attacker.rank_position,
            defender_rank_after=updated_defender.rank_position,
            rank_effect_applied=True,
            affected_rank_range=PvpRankRange(
                rank_start=defender_before.rank_position,
                rank_end=attacker_before.rank_position,
            ),
            rank_updates=tuple(updates),
            ordered_entries_after=tuple(updated_entries),
        )

    def calculate_honor_coin_settlement(
        self,
        *,
        attacker: PvpLeaderboardEntry,
        defender: PvpLeaderboardEntry,
        battle_outcome: PvpBattleOutcome,
        attacker_current_win_streak: int,
        balance_before: int | None = None,
    ) -> PvpHonorCoinSettlement:
        """按胜负、名次差、爆冷与连胜计算荣誉币。"""
        if attacker_current_win_streak < 0:
            raise PvpRuleError("attacker_current_win_streak 不能为负数")
        if balance_before is not None and balance_before < 0:
            raise PvpRuleError("balance_before 不能为负数")

        rank_gap = max(0, attacker.rank_position - defender.rank_position)
        challenged_lower_rank_target = attacker.rank_position < defender.rank_position
        components: list[PvpHonorCoinComponentResult] = []
        if battle_outcome is PvpBattleOutcome.ALLY_VICTORY:
            if challenged_lower_rank_target:
                lower_rank_delta = max(0, self._config.honor_coin.win_base // 4)
                components.append(
                    self._build_honor_component_result(
                        component_id="base",
                        applied_delta=lower_rank_delta,
                    )
                )
                delta = lower_rank_delta
            else:
                components.append(
                    self._build_honor_component_result(
                        component_id="base",
                        applied_delta=self._config.honor_coin.win_base,
                    )
                )
                bonus_steps = rank_gap // self._config.honor_coin.rank_gap_bonus_step
                rank_gap_bonus = bonus_steps * self._config.honor_coin.rank_gap_bonus_per_step
                if rank_gap_bonus > 0:
                    components.append(
                        self._build_honor_component_result(
                            component_id="rank_gap_bonus",
                            applied_delta=rank_gap_bonus,
                        )
                    )
                if rank_gap >= self._config.honor_coin.upset_bonus_threshold:
                    components.append(
                        self._build_honor_component_result(
                            component_id="upset_bonus",
                            applied_delta=self._config.honor_coin.upset_bonus,
                        )
                    )
                if attacker_current_win_streak + 1 >= self._config.honor_coin.streak_bonus_trigger:
                    components.append(
                        self._build_honor_component_result(
                            component_id="streak_bonus",
                            applied_delta=self._config.honor_coin.streak_bonus,
                        )
                    )
                delta = max(
                    self._config.honor_coin.win_floor,
                    sum(component.applied_delta for component in components),
                )
        else:
            failure_delta = self._config.honor_coin.loss_base
            if failure_delta < self._config.honor_coin.loss_floor:
                failure_delta = self._config.honor_coin.loss_floor
            components.append(
                self._build_honor_component_result(
                    component_id="loss_floor",
                    applied_delta=failure_delta,
                )
            )
            delta = failure_delta

        balance_after = None if balance_before is None else balance_before + delta
        return PvpHonorCoinSettlement(
            battle_outcome=battle_outcome,
            rank_gap=rank_gap,
            delta=delta,
            balance_before=balance_before,
            balance_after=balance_after,
            components=tuple(components),
            reward_preview=None,
        )

    def build_reward_preview(
        self,
        *,
        rank_position: int,
        honor_coin_on_win: int,
        honor_coin_on_loss: int,
        reward_state: PvpRewardState = PvpRewardState.PREVIEW,
    ) -> PvpRewardPreview:
        """构造展示层可直接消费的奖励预览。"""
        tier = self.resolve_reward_tier(rank_position=rank_position)
        rank_range = PvpRankRange(rank_start=tier.rank_start, rank_end=tier.rank_end)
        display_items = self._build_display_items_for_tier(
            tier=tier,
            reward_state=reward_state,
        )
        return PvpRewardPreview(
            reward_tier_id=tier.reward_tier_id,
            rank_range=rank_range,
            honor_coin_on_win=honor_coin_on_win,
            honor_coin_on_loss=honor_coin_on_loss,
            display_items=display_items,
            summary=f"当前奖励档位：{tier.summary}",
        )

    def build_challenge_settlement(
        self,
        *,
        context: PvpChallengeContext,
        target_pool: PvpTargetPoolResult,
        leaderboard_entries: tuple[PvpLeaderboardEntry, ...],
        battle_outcome: PvpBattleOutcome,
        now: datetime,
        balance_before: int | None = None,
    ) -> PvpChallengeSettlement:
        """组合单次挑战的纯领域结算输出。"""
        eligibility = self.validate_challenge_eligibility(
            context=context,
            target_pool=target_pool,
            now=now,
        )
        if not eligibility.allowed:
            raise PvpRuleError(f"挑战上下文不满足结算前置条件：{','.join(eligibility.block_reasons)}")

        rank_change = self.resolve_rank_change(
            leaderboard_entries=leaderboard_entries,
            attacker_character_id=context.attacker.character_id,
            defender_character_id=context.defender.character_id,
            battle_outcome=battle_outcome,
        )
        honor_coin_settlement = self.calculate_honor_coin_settlement(
            attacker=context.attacker,
            defender=context.defender,
            battle_outcome=battle_outcome,
            attacker_current_win_streak=context.attacker_current_win_streak,
            balance_before=balance_before,
        )
        preview_on_win = self.calculate_honor_coin_settlement(
            attacker=context.attacker,
            defender=context.defender,
            battle_outcome=PvpBattleOutcome.ALLY_VICTORY,
            attacker_current_win_streak=context.attacker_current_win_streak,
            balance_before=None,
        ).delta
        preview_on_loss = self.calculate_honor_coin_settlement(
            attacker=context.attacker,
            defender=context.defender,
            battle_outcome=PvpBattleOutcome.ENEMY_VICTORY,
            attacker_current_win_streak=context.attacker_current_win_streak,
            balance_before=None,
        ).delta
        reward_state = self._resolve_reward_state(
            rank_before=context.attacker.rank_position,
            rank_after=rank_change.attacker_rank_after,
        )
        reward_preview = self.build_reward_preview(
            rank_position=rank_change.attacker_rank_after,
            honor_coin_on_win=preview_on_win,
            honor_coin_on_loss=preview_on_loss,
            reward_state=reward_state,
        )
        honor_coin_settlement = replace(honor_coin_settlement, reward_preview=reward_preview)
        anti_abuse_flags = self._build_post_settlement_flags(
            context=context,
            battle_outcome=battle_outcome,
            defense_failure_cap=eligibility.defense_failure_cap,
            rank_change=rank_change,
        )
        defense_snapshot_version = None
        if context.defender_snapshot_state is not None:
            defense_snapshot_version = context.defender_snapshot_state.snapshot_version
        return PvpChallengeSettlement(
            attacker_character_id=context.attacker.character_id,
            defender_character_id=context.defender.character_id,
            battle_outcome=battle_outcome,
            rank_change=rank_change,
            honor_coin_settlement=honor_coin_settlement,
            reward_preview=reward_preview,
            display_rewards=reward_preview.display_items,
            anti_abuse_flags=anti_abuse_flags,
            defense_snapshot_version=defense_snapshot_version,
        )

    def resolve_reward_tier(self, *, rank_position: int) -> PvpRewardTierDefinition:
        """按名次解析展示奖励档位。"""
        if rank_position <= 0:
            raise PvpRuleError("rank_position 必须大于 0")
        for tier in self._reward_tiers:
            if tier.rank_start <= rank_position <= tier.rank_end:
                return tier
        try:
            return self._reward_tier_by_id[self._config.reward_preview.default_tier_id]
        except KeyError as exc:
            raise PvpRuleError("默认奖励档位未在 PVP 配置中声明") from exc

    def _filter_target_candidates(
        self,
        *,
        attacker: PvpLeaderboardEntry,
        leaderboard_entries: tuple[PvpLeaderboardEntry, ...],
        defense_snapshot_by_character_id: dict[int, PvpDefenseSnapshotState],
        daily_activity_by_character_id: dict[int, PvpDailyActivitySnapshot],
        now: datetime,
        rank_window_up: int,
        rank_window_down: int,
        public_tolerance: Decimal,
        hidden_tolerance: Decimal,
    ) -> tuple[list[PvpTargetCandidate], list[PvpTargetCandidate]]:
        rank_range = PvpRankRange(
            rank_start=max(1, attacker.rank_position - rank_window_up),
            rank_end=attacker.rank_position + rank_window_down,
        )
        candidates: list[PvpTargetCandidate] = []
        rejected_candidates: list[PvpTargetCandidate] = []
        for entry in leaderboard_entries:
            defense_snapshot = defense_snapshot_by_character_id.get(entry.character_id)
            daily_activity = daily_activity_by_character_id.get(entry.character_id)
            defense_failure_count = 0 if daily_activity is None else daily_activity.defense_failure_count
            defense_failure_cap = self.resolve_defense_failure_cap(
                rank_position=entry.rank_position,
                defense_failure_count=defense_failure_count,
            )
            protected = entry.is_protected(now=now)
            has_active_snapshot = defense_snapshot is not None and defense_snapshot.is_active(now=now)
            realm_gap = self._calculate_realm_gap(attacker.realm_id, entry.realm_id)
            public_ratio_delta = self._calculate_ratio_delta(
                left_value=attacker.public_power_score,
                right_value=entry.public_power_score,
            )
            hidden_ratio_delta = self._calculate_ratio_delta(
                left_value=attacker.hidden_pvp_score,
                right_value=entry.hidden_pvp_score,
            )
            rejection_reasons: list[str] = []
            if entry.character_id == attacker.character_id:
                rejection_reasons.append("self_target")
            if protected:
                rejection_reasons.append("defender_protected")
            if not has_active_snapshot:
                rejection_reasons.append("missing_active_snapshot")
            if not rank_range.contains(entry.rank_position):
                rejection_reasons.append("outside_rank_window")
            if realm_gap > self._config.target_pool.max_realm_gap:
                rejection_reasons.append("realm_gap_exceeded")
            if public_ratio_delta > public_tolerance:
                rejection_reasons.append("public_power_gap_exceeded")
            if hidden_ratio_delta > hidden_tolerance:
                rejection_reasons.append("hidden_score_gap_exceeded")
            if defense_failure_cap.cap_reached:
                rejection_reasons.append("defense_failure_cap_reached")
            candidate = PvpTargetCandidate(
                leaderboard_entry=entry,
                defense_snapshot_state=defense_snapshot,
                daily_activity=daily_activity,
                rank_gap=attacker.rank_position - entry.rank_position,
                realm_gap=realm_gap,
                public_power_ratio_delta=public_ratio_delta,
                hidden_score_ratio_delta=hidden_ratio_delta,
                protected=protected,
                has_active_defense_snapshot=has_active_snapshot,
                defense_failure_cap=defense_failure_cap.cap,
                defense_failure_cap_reached=defense_failure_cap.cap_reached,
                rejection_reasons=tuple(rejection_reasons),
            )
            if candidate.is_eligible:
                candidates.append(candidate)
            else:
                rejected_candidates.append(candidate)
        return candidates, rejected_candidates

    def _calculate_realm_gap(self, attacker_realm_id: str, defender_realm_id: str) -> int:
        try:
            attacker_order = self._realm_order_by_id[attacker_realm_id]
        except KeyError as exc:
            raise PvpRuleError(f"未知攻击方境界标识：{attacker_realm_id}") from exc
        try:
            defender_order = self._realm_order_by_id[defender_realm_id]
        except KeyError as exc:
            raise PvpRuleError(f"未知防守方境界标识：{defender_realm_id}") from exc
        return abs(attacker_order - defender_order)

    def _calculate_ratio_delta(self, *, left_value: int, right_value: int) -> Decimal:
        if left_value < 0 or right_value < 0:
            raise PvpRuleError("评分值不能为负数")
        denominator = max(1, left_value, right_value)
        ratio = (Decimal(abs(left_value - right_value)) / Decimal(denominator)).quantize(
            _RATIO_PRECISION,
            rounding=ROUND_HALF_UP,
        )
        return ratio

    def _normalize_leaderboard_entries(
        self,
        entries: tuple[PvpLeaderboardEntry, ...],
    ) -> tuple[PvpLeaderboardEntry, ...]:
        if not entries:
            raise PvpRuleError("leaderboard_entries 不能为空")
        ordered_entries = tuple(sorted(entries, key=lambda entry: entry.rank_position))
        actual_ranks = [entry.rank_position for entry in ordered_entries]
        expected_ranks = list(range(1, len(ordered_entries) + 1))
        if actual_ranks != expected_ranks:
            raise PvpRuleError("leaderboard_entries 的名次必须连续且从 1 开始")
        character_ids = [entry.character_id for entry in ordered_entries]
        if len(character_ids) != len(set(character_ids)):
            raise PvpRuleError("leaderboard_entries 中存在重复角色")
        return ordered_entries

    def _get_entry_by_character_id(
        self,
        entries: tuple[PvpLeaderboardEntry, ...],
        character_id: int,
    ) -> PvpLeaderboardEntry:
        for entry in entries:
            if entry.character_id == character_id:
                return entry
        raise PvpRuleError(f"榜单中缺少角色：{character_id}")

    def _replace_entry_rank(self, *, entry: PvpLeaderboardEntry, rank_position: int) -> PvpLeaderboardEntry:
        best_rank_candidates = [rank_position, entry.rank_position]
        if entry.best_rank is not None:
            best_rank_candidates.append(entry.best_rank)
        best_rank = min(best_rank_candidates)
        reward_tier = self.resolve_reward_tier(rank_position=rank_position)
        summary = dict(entry.summary)
        summary.update(
            {
                "best_rank": best_rank,
                "protected_until": entry.protected_until,
                "latest_defense_snapshot_version": entry.latest_defense_snapshot_version,
                "challenge_tier": reward_tier.reward_tier_id,
                "public_power_score": entry.public_power_score,
                "reward_preview_tier": reward_tier.reward_tier_id,
            }
        )
        return replace(
            entry,
            rank_position=rank_position,
            best_rank=best_rank,
            challenge_tier=reward_tier.reward_tier_id,
            reward_preview_tier=reward_tier.reward_tier_id,
            summary=summary,
        )

    def _build_honor_component_result(
        self,
        *,
        component_id: str,
        applied_delta: int,
    ) -> PvpHonorCoinComponentResult:
        component = self._honor_components_by_id.get(component_id)
        if component is None:
            raise PvpRuleError(f"未声明的荣誉币组件：{component_id}")
        return PvpHonorCoinComponentResult(
            component_id=component_id,
            configured_delta=max(0, component.delta),
            applied_delta=applied_delta,
            summary=component.summary,
            triggered=applied_delta > 0,
        )

    def _build_display_items_for_tier(
        self,
        *,
        tier: PvpRewardTierDefinition,
        reward_state: PvpRewardState,
    ) -> tuple[PvpRewardDisplayItem, ...]:
        raw_items = _DEFAULT_DISPLAY_LIBRARY.get(
            tier.reward_tier_id,
            ((PvpRewardDisplayType.BADGE, tier.name, "rare"),),
        )
        display_items: list[PvpRewardDisplayItem] = []
        for reward_type, name, rarity in raw_items:
            display_items.append(
                PvpRewardDisplayItem(
                    reward_id=f"{tier.reward_tier_id}:{reward_type.value}",
                    reward_type=reward_type,
                    name=name,
                    rarity=rarity,
                    state=reward_state,
                    source=PvpRewardSource.RANK_TIER,
                    meta={
                        "reward_tier_id": tier.reward_tier_id,
                        "rank_start": tier.rank_start,
                        "rank_end": tier.rank_end,
                    },
                )
            )
        return tuple(display_items)

    def _resolve_reward_state(self, *, rank_before: int, rank_after: int) -> PvpRewardState:
        before_tier = self.resolve_reward_tier(rank_position=rank_before)
        after_tier = self.resolve_reward_tier(rank_position=rank_after)
        if self._reward_tier_order_by_id[after_tier.reward_tier_id] < self._reward_tier_order_by_id[before_tier.reward_tier_id]:
            return PvpRewardState.UNLOCKED_NOW
        return PvpRewardState.OWNED

    def _build_post_settlement_flags(
        self,
        *,
        context: PvpChallengeContext,
        battle_outcome: PvpBattleOutcome,
        defense_failure_cap: PvpDefenseFailureCapDecision,
        rank_change: PvpRankChange,
    ) -> tuple[str, ...]:
        flags: list[str] = []
        if context.attacker_daily_activity.effective_challenge_count + 1 >= self._config.daily_limit.effective_challenge_limit:
            flags.append("daily_quota_exhausted")
        if context.effective_repeat_count_against_target + 1 >= self._config.daily_limit.repeat_target_limit:
            flags.append("repeat_target_limit_reached")
        if battle_outcome is PvpBattleOutcome.ALLY_VICTORY and defense_failure_cap.cap is not None:
            if defense_failure_cap.used_count + 1 >= defense_failure_cap.cap:
                flags.append("defense_failure_cap_reached")
        if battle_outcome is PvpBattleOutcome.ALLY_VICTORY and not rank_change.rank_effect_applied:
            flags.append("rank_unchanged")
        return tuple(flags)


__all__ = ["PvpRuleError", "PvpRuleService"]
