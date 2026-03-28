"""PVP 主应用服务。"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from application.battle import AutoBattleRequest, AutoBattleService
from application.character.current_attribute_service import CurrentAttributeService
from application.character.growth_service import CharacterGrowthStateError, CharacterNotFoundError
from application.pvp.defense_snapshot_service import PvpDefenseSnapshotBundle, PvpDefenseSnapshotService
from application.pvp.honor_coin_service import HonorCoinApplicationResult, HonorCoinService
from domain.battle import BattleOutcome, BattleSide, BattleSnapshot, BattleUnitSnapshot
from domain.pvp import (
    PvpBattleOutcome,
    PvpChallengeContext,
    PvpChallengeSettlement,
    PvpDailyActivitySnapshot,
    PvpDefenseSnapshotState,
    PvpLeaderboardEntry,
    PvpRewardDisplayItem,
    PvpRewardPreview,
    PvpRuleService,
    PvpTargetCandidate,
    PvpTargetPoolResult,
)
from domain.ranking import LeaderboardBoardType
from infrastructure.config.static import StaticGameConfig, get_static_config
from infrastructure.db.models import (
    CharacterProgress,
    CharacterScoreSnapshot,
    LeaderboardEntrySnapshot,
    LeaderboardSnapshot,
    PvpChallengeRecord,
    PvpDailyActivityLedger,
)
from infrastructure.db.repositories import CharacterAggregate, CharacterRepository, PvpChallengeRepository, SnapshotRepository

_PVP_BATTLE_TYPE = "pvp_challenge"
_PVP_LEDGER_SOURCE_TYPE = "pvp_challenge"
_PVP_LEADERBOARD_SCOPE_SCHEMA = "stage9.pvp.v1"
_PVP_TARGET_SYNC_GENERATED_BY = "pvp_service.target_pool_sync"
_PVP_PRE_CHALLENGE_SYNC_GENERATED_BY = "pvp_service.pre_challenge_sync"
_PVP_CHALLENGE_GENERATED_BY = "pvp_service.challenge_target"
_PVP_ENTER_LADDER_GENERATED_BY = "pvp_service.enter_ladder"
_PVP_SEED_GENERATED_BY = "pvp_service.seed_leaderboard"
_PVP_ROUND_LIMIT = 12


@dataclass(frozen=True, slots=True)
class PvpTargetView:
    """单个目标池候选项的展示结果。"""

    character_id: int
    rank_position: int
    rank_gap: int
    public_power_score: int
    hidden_pvp_score: int
    challenge_tier: str | None
    reward_preview_tier: str | None
    latest_defense_snapshot_version: int | None
    has_active_defense_snapshot: bool
    protected: bool
    rejection_reasons: tuple[str, ...]
    display_summary: str
    reward_preview: dict[str, object]
    summary: dict[str, object]


@dataclass(frozen=True, slots=True)
class PvpTargetListSnapshot:
    """目标池查询结果。"""

    character_id: int
    cycle_anchor_date: date
    generated_at: datetime
    current_rank_position: int
    current_best_rank: int
    targets: tuple[PvpTargetView, ...]
    rejected_targets: tuple[PvpTargetView, ...]
    fallback_triggered: bool
    expansion_steps_applied: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PvpHubSnapshot:
    """PVP 主界面聚合结果。"""

    character_id: int
    cycle_anchor_date: date
    current_rank_position: int
    current_best_rank: int
    protected_until: str | None
    remaining_challenge_count: int
    honor_coin_balance: int
    reward_preview: dict[str, object]
    defense_snapshot_summary: dict[str, object] | None
    target_list: PvpTargetListSnapshot


@dataclass(frozen=True, slots=True)
class PvpChallengeResult:
    """单次 PVP 挑战后的应用层结果。"""

    attacker_character_id: int
    defender_character_id: int
    cycle_anchor_date: date
    battle_outcome: str
    battle_report_id: int
    leaderboard_snapshot_id: int
    defender_snapshot_id: int
    challenge_record_id: int
    rank_before_attacker: int
    rank_after_attacker: int
    rank_before_defender: int
    rank_after_defender: int
    rank_effect_applied: bool
    honor_coin_delta: int
    honor_coin_balance_after: int
    anti_abuse_flags: tuple[str, ...]
    reward_preview: dict[str, object] | None
    display_rewards: tuple[dict[str, object], ...]
    settlement: dict[str, object]
    environment_snapshot: dict[str, object]


@dataclass(frozen=True, slots=True)
class _TargetPoolSupport:
    """目标池构造前的辅助上下文。"""

    leaderboard_entries: tuple[PvpLeaderboardEntry, ...]
    defense_snapshot_by_character_id: dict[int, PvpDefenseSnapshotState]
    daily_activity_by_character_id: dict[int, PvpDailyActivitySnapshot]
    current_win_streak: int
    changed: bool


class PvpServiceError(RuntimeError):
    """PVP 服务基础异常。"""


class PvpStateError(PvpServiceError):
    """PVP 依赖状态不完整。"""


class PvpChallengeNotAllowedError(PvpServiceError):
    """当前挑战不满足规则限制。"""


class PvpTargetNotFoundError(PvpServiceError):
    """请求的目标角色不存在于当前 PVP 榜。"""


class PvpService:
    """负责编排阶段 9 的目标池、战斗与结算持久化。"""

    def __init__(
        self,
        *,
        character_repository: CharacterRepository,
        snapshot_repository: SnapshotRepository,
        pvp_challenge_repository: PvpChallengeRepository,
        auto_battle_service: AutoBattleService,
        defense_snapshot_service: PvpDefenseSnapshotService,
        honor_coin_service: HonorCoinService,
        static_config: StaticGameConfig | None = None,
        rule_service: PvpRuleService | None = None,
    ) -> None:
        self._character_repository = character_repository
        self._snapshot_repository = snapshot_repository
        self._pvp_challenge_repository = pvp_challenge_repository
        self._auto_battle_service = auto_battle_service
        self._defense_snapshot_service = defense_snapshot_service
        self._honor_coin_service = honor_coin_service
        self._static_config = static_config or get_static_config()
        self._rule_service = rule_service or PvpRuleService(self._static_config)
        self._cycle_timezone = ZoneInfo(self._static_config.pvp.anti_abuse.cycle_timezone)
        self._realm_name_by_id = {
            realm.realm_id: realm.name
            for realm in self._static_config.realm_progression.realms
        }
        self._stage_name_by_id = {
            stage.stage_id: stage.name
            for stage in self._static_config.realm_progression.stages
        }

    def get_pvp_hub(
        self,
        *,
        character_id: int,
        now: datetime | None = None,
    ) -> PvpHubSnapshot:
        """读取 PVP 主界面聚合信息。"""
        current_time = self._resolve_current_time(now)
        cycle_anchor_date = self._resolve_cycle_anchor_date(current_time)
        leaderboard_entries, _ = self._ensure_leaderboard_entry(
            character_id=character_id,
            now=current_time,
        )
        support = self._prepare_target_pool_support(
            leaderboard_entries=leaderboard_entries,
            attacker_character_id=character_id,
            cycle_anchor_date=cycle_anchor_date,
            now=current_time,
        )
        if support.changed:
            support = replace(
                support,
                leaderboard_entries=self._persist_pvp_leaderboard(
                    leaderboard_entries=support.leaderboard_entries,
                    generated_at=current_time,
                    generated_by=_PVP_TARGET_SYNC_GENERATED_BY,
                ),
                changed=False,
            )
        attacker_entry = self._require_leaderboard_entry(
            support.leaderboard_entries,
            character_id=character_id,
        )
        target_pool = self._rule_service.build_target_pool(
            attacker=attacker_entry,
            leaderboard_entries=support.leaderboard_entries,
            defense_snapshot_by_character_id=support.defense_snapshot_by_character_id,
            daily_activity_by_character_id=support.daily_activity_by_character_id,
            now=current_time,
        )
        target_list = self._build_target_list_snapshot(
            attacker_entry=attacker_entry,
            target_pool=target_pool,
            cycle_anchor_date=cycle_anchor_date,
            generated_at=current_time,
            attacker_current_win_streak=support.current_win_streak,
        )
        activity_snapshot = support.daily_activity_by_character_id.get(
            character_id,
            self._build_empty_daily_activity(character_id=character_id, cycle_anchor_date=cycle_anchor_date),
        )
        quota = self._rule_service.check_daily_challenge_limit(activity=activity_snapshot)
        balance = self._honor_coin_service.get_balance(character_id=character_id)
        active_snapshot_state = self._defense_snapshot_service.get_active_snapshot_state(
            character_id=character_id,
            now=current_time,
        )
        reward_preview = self._honor_coin_service.preview_rank_rewards(rank_position=attacker_entry.rank_position)
        return PvpHubSnapshot(
            character_id=character_id,
            cycle_anchor_date=cycle_anchor_date,
            current_rank_position=attacker_entry.rank_position,
            current_best_rank=attacker_entry.best_rank or attacker_entry.rank_position,
            protected_until=None if attacker_entry.protected_until is None else attacker_entry.protected_until.isoformat(),
            remaining_challenge_count=quota.remaining_count,
            honor_coin_balance=balance.honor_coin,
            reward_preview=self._serialize_reward_preview(reward_preview),
            defense_snapshot_summary=None
            if active_snapshot_state is None
            else self._defense_snapshot_service.build_display_summary(snapshot_state=active_snapshot_state),
            target_list=target_list,
        )

    def list_targets(
        self,
        *,
        character_id: int,
        now: datetime | None = None,
    ) -> PvpTargetListSnapshot:
        """读取当前角色可挑战目标池。"""
        current_time = self._resolve_current_time(now)
        cycle_anchor_date = self._resolve_cycle_anchor_date(current_time)
        leaderboard_entries, _ = self._ensure_leaderboard_entry(
            character_id=character_id,
            now=current_time,
        )
        support = self._prepare_target_pool_support(
            leaderboard_entries=leaderboard_entries,
            attacker_character_id=character_id,
            cycle_anchor_date=cycle_anchor_date,
            now=current_time,
        )
        if support.changed:
            support = replace(
                support,
                leaderboard_entries=self._persist_pvp_leaderboard(
                    leaderboard_entries=support.leaderboard_entries,
                    generated_at=current_time,
                    generated_by=_PVP_TARGET_SYNC_GENERATED_BY,
                ),
                changed=False,
            )
        attacker_entry = self._require_leaderboard_entry(
            support.leaderboard_entries,
            character_id=character_id,
        )
        target_pool = self._rule_service.build_target_pool(
            attacker=attacker_entry,
            leaderboard_entries=support.leaderboard_entries,
            defense_snapshot_by_character_id=support.defense_snapshot_by_character_id,
            daily_activity_by_character_id=support.daily_activity_by_character_id,
            now=current_time,
        )
        return self._build_target_list_snapshot(
            attacker_entry=attacker_entry,
            target_pool=target_pool,
            cycle_anchor_date=cycle_anchor_date,
            generated_at=current_time,
            attacker_current_win_streak=support.current_win_streak,
        )

    def challenge_target(
        self,
        *,
        character_id: int,
        target_character_id: int,
        now: datetime | None = None,
        seed: int | None = None,
    ) -> PvpChallengeResult:
        """执行一次完整 PVP 挑战并完成结算写入。"""
        current_time = self._resolve_current_time(now)
        cycle_anchor_date = self._resolve_cycle_anchor_date(current_time)
        leaderboard_entries, _ = self._ensure_leaderboard_entry(
            character_id=character_id,
            now=current_time,
        )
        support = self._prepare_target_pool_support(
            leaderboard_entries=leaderboard_entries,
            attacker_character_id=character_id,
            cycle_anchor_date=cycle_anchor_date,
            now=current_time,
        )
        if support.changed:
            leaderboard_entries = self._persist_pvp_leaderboard(
                leaderboard_entries=support.leaderboard_entries,
                generated_at=current_time,
                generated_by=_PVP_TARGET_SYNC_GENERATED_BY,
            )
            support = replace(support, leaderboard_entries=leaderboard_entries, changed=False)
        else:
            leaderboard_entries = support.leaderboard_entries
        attacker_entry = self._require_leaderboard_entry(leaderboard_entries, character_id=character_id)
        target_pool = self._rule_service.build_target_pool(
            attacker=attacker_entry,
            leaderboard_entries=leaderboard_entries,
            defense_snapshot_by_character_id=support.defense_snapshot_by_character_id,
            daily_activity_by_character_id=support.daily_activity_by_character_id,
            now=current_time,
        )
        if not target_pool.contains_character(target_character_id):
            raise PvpChallengeNotAllowedError(f"目标不在当前可挑战目标池内：{target_character_id}")
        defender_entry = self._require_leaderboard_entry(leaderboard_entries, character_id=target_character_id)
        attacker_daily_ledger = self._pvp_challenge_repository.get_or_create_daily_activity(character_id, cycle_anchor_date)
        defender_daily_ledger = self._pvp_challenge_repository.get_daily_activity(target_character_id, cycle_anchor_date)
        repeat_count = self._pvp_challenge_repository.count_effective_challenges_against_target(
            character_id,
            target_character_id,
            cycle_anchor_date,
        )
        attacker_bundle = self._defense_snapshot_service.ensure_snapshot(
            character_id=character_id,
            now=current_time,
            requested_reason="challenge_start",
            leaderboard_entry=attacker_entry,
        )
        defender_bundle = self._defense_snapshot_service.ensure_snapshot(
            character_id=target_character_id,
            now=current_time,
            requested_reason="defense_on_demand",
            leaderboard_entry=defender_entry,
        )
        defender_snapshot_id = self._require_snapshot_id(
            defender_bundle.snapshot_state,
            character_id=target_character_id,
        )
        version_map = {
            current_character_id: snapshot_state.snapshot_version
            for current_character_id, snapshot_state in support.defense_snapshot_by_character_id.items()
        }
        version_map[character_id] = attacker_bundle.snapshot_state.snapshot_version
        version_map[target_character_id] = defender_bundle.snapshot_state.snapshot_version
        leaderboard_entries, version_changed = self._merge_latest_defense_versions(
            leaderboard_entries,
            version_map,
        )
        if version_changed:
            leaderboard_entries = self._persist_pvp_leaderboard(
                leaderboard_entries=leaderboard_entries,
                generated_at=current_time,
                generated_by=_PVP_PRE_CHALLENGE_SYNC_GENERATED_BY,
            )
        attacker_entry = self._require_leaderboard_entry(leaderboard_entries, character_id=character_id)
        defender_entry = self._require_leaderboard_entry(leaderboard_entries, character_id=target_character_id)
        attacker_daily_activity = self._to_daily_activity_snapshot(attacker_daily_ledger)
        defender_daily_activity = (
            None
            if defender_daily_ledger is None
            else self._to_daily_activity_snapshot(defender_daily_ledger)
        )
        context = PvpChallengeContext(
            attacker=attacker_entry,
            defender=defender_entry,
            attacker_daily_activity=attacker_daily_activity,
            defender_daily_activity=defender_daily_activity,
            defender_snapshot_state=defender_bundle.snapshot_state,
            cycle_anchor_date=cycle_anchor_date,
            effective_repeat_count_against_target=repeat_count,
            attacker_current_win_streak=support.current_win_streak,
        )
        eligibility = self._rule_service.validate_challenge_eligibility(
            context=context,
            target_pool=target_pool,
            now=current_time,
        )
        if not eligibility.allowed:
            raise PvpChallengeNotAllowedError(
                f"当前挑战不满足条件：{','.join(eligibility.block_reasons)}"
            )
        request = self._build_auto_battle_request(
            attacker_entry=attacker_entry,
            attacker_bundle=attacker_bundle,
            defender_entry=defender_entry,
            defender_bundle=defender_bundle,
            cycle_anchor_date=cycle_anchor_date,
            seed=seed,
            now=current_time,
        )
        execution_result = self._auto_battle_service.execute(
            request=request,
            persist=True,
        )
        battle_report_id = execution_result.persisted_battle_report_id
        if battle_report_id is None:
            raise PvpStateError("PVP 挑战缺少持久化战报标识")
        battle_outcome = self._map_battle_outcome(execution_result.domain_result.outcome)
        balance_before = self._honor_coin_service.get_balance(character_id=character_id).honor_coin
        challenge_settlement = self._rule_service.build_challenge_settlement(
            context=context,
            target_pool=target_pool,
            leaderboard_entries=leaderboard_entries,
            battle_outcome=battle_outcome,
            now=current_time,
            balance_before=balance_before,
        )
        settled_entries, _ = self._merge_latest_defense_versions(
            challenge_settlement.rank_change.ordered_entries_after,
            version_map,
        )
        self._persist_pvp_leaderboard(
            leaderboard_entries=settled_entries,
            generated_at=current_time,
            generated_by=_PVP_CHALLENGE_GENERATED_BY,
        )
        persisted_leaderboard_snapshot = self._snapshot_repository.get_latest_leaderboard(
            LeaderboardBoardType.PVP_CHALLENGE.value,
        )
        if persisted_leaderboard_snapshot is None:
            raise PvpStateError("PVP 挑战后缺少正式榜单快照")
        honor_coin_result = self._honor_coin_service.apply_settlement(
            character_id=character_id,
            source_type=_PVP_LEDGER_SOURCE_TYPE,
            source_ref=f"battle_report:{battle_report_id}",
            settlement=challenge_settlement.honor_coin_settlement,
            occurred_at=current_time,
            detail_extension={
                "battle_report_id": battle_report_id,
                "leaderboard_snapshot_id": persisted_leaderboard_snapshot.id,
                "defender_snapshot_id": defender_snapshot_id,
                "cycle_anchor_date": cycle_anchor_date.isoformat(),
            },
        )
        self._update_daily_activity_ledgers(
            attacker_daily_ledger=attacker_daily_ledger,
            defender_daily_ledger=defender_daily_ledger,
            defender_character_id=target_character_id,
            cycle_anchor_date=cycle_anchor_date,
            battle_outcome=battle_outcome,
            occurred_at=current_time,
            defender_rank_position=defender_entry.rank_position,
        )
        challenge_record = self._pvp_challenge_repository.add_challenge_record(
            PvpChallengeRecord(
                attacker_character_id=character_id,
                defender_character_id=target_character_id,
                defender_snapshot_id=defender_snapshot_id,
                leaderboard_snapshot_id=persisted_leaderboard_snapshot.id,
                battle_report_id=battle_report_id,
                cycle_anchor_date=cycle_anchor_date,
                battle_outcome=battle_outcome.value,
                rank_before_attacker=challenge_settlement.rank_change.attacker_rank_before,
                rank_before_defender=challenge_settlement.rank_change.defender_rank_before,
                rank_after_attacker=challenge_settlement.rank_change.attacker_rank_after,
                rank_after_defender=challenge_settlement.rank_change.defender_rank_after,
                honor_coin_delta=honor_coin_result.settlement.delta,
                rank_effect_applied=challenge_settlement.rank_change.rank_effect_applied,
                settlement_json=self._build_challenge_record_settlement_payload(
                    challenge_settlement=challenge_settlement,
                    honor_coin_result=honor_coin_result,
                    battle_report_id=battle_report_id,
                    leaderboard_snapshot_id=persisted_leaderboard_snapshot.id,
                    defender_snapshot_id=defender_snapshot_id,
                ),
                created_at=current_time,
            )
        )
        return PvpChallengeResult(
            attacker_character_id=character_id,
            defender_character_id=target_character_id,
            cycle_anchor_date=cycle_anchor_date,
            battle_outcome=battle_outcome.value,
            battle_report_id=battle_report_id,
            leaderboard_snapshot_id=persisted_leaderboard_snapshot.id,
            defender_snapshot_id=defender_snapshot_id,
            challenge_record_id=challenge_record.id,
            rank_before_attacker=challenge_settlement.rank_change.attacker_rank_before,
            rank_after_attacker=challenge_settlement.rank_change.attacker_rank_after,
            rank_before_defender=challenge_settlement.rank_change.defender_rank_before,
            rank_after_defender=challenge_settlement.rank_change.defender_rank_after,
            rank_effect_applied=challenge_settlement.rank_change.rank_effect_applied,
            honor_coin_delta=honor_coin_result.settlement.delta,
            honor_coin_balance_after=honor_coin_result.settlement.balance_after or 0,
            anti_abuse_flags=challenge_settlement.anti_abuse_flags,
            reward_preview=None
            if challenge_settlement.reward_preview is None
            else self._serialize_reward_preview(challenge_settlement.reward_preview),
            display_rewards=tuple(
                self._serialize_reward_item(item)
                for item in challenge_settlement.display_rewards
            ),
            settlement=self._build_result_settlement_payload(
                challenge_settlement=challenge_settlement,
                honor_coin_result=honor_coin_result,
                challenge_record_id=challenge_record.id,
            ),
            environment_snapshot=dict(request.environment_snapshot or {}),
        )

    def _prepare_target_pool_support(
        self,
        *,
        leaderboard_entries: tuple[PvpLeaderboardEntry, ...],
        attacker_character_id: int,
        cycle_anchor_date: date,
        now: datetime,
    ) -> _TargetPoolSupport:
        defense_snapshot_by_character_id: dict[int, PvpDefenseSnapshotState] = {}
        daily_activity_by_character_id: dict[int, PvpDailyActivitySnapshot] = {}
        defense_version_by_character_id: dict[int, int | None] = {}
        for entry in leaderboard_entries:
            activity_ledger = self._pvp_challenge_repository.get_daily_activity(
                entry.character_id,
                cycle_anchor_date,
            )
            if activity_ledger is not None:
                daily_activity_by_character_id[entry.character_id] = self._to_daily_activity_snapshot(activity_ledger)
            snapshot_state = self._defense_snapshot_service.get_active_snapshot_state(
                character_id=entry.character_id,
                now=now,
            )
            if snapshot_state is None and entry.character_id != attacker_character_id:
                bundle = self._defense_snapshot_service.ensure_snapshot(
                    character_id=entry.character_id,
                    now=now,
                    requested_reason="defense_on_demand",
                    leaderboard_entry=entry,
                )
                snapshot_state = bundle.snapshot_state
            if snapshot_state is not None:
                defense_snapshot_by_character_id[entry.character_id] = snapshot_state
                defense_version_by_character_id[entry.character_id] = snapshot_state.snapshot_version
        updated_entries, changed = self._merge_latest_defense_versions(
            leaderboard_entries,
            defense_version_by_character_id,
        )
        return _TargetPoolSupport(
            leaderboard_entries=updated_entries,
            defense_snapshot_by_character_id=defense_snapshot_by_character_id,
            daily_activity_by_character_id=daily_activity_by_character_id,
            current_win_streak=self._calculate_current_win_streak(
                attacker_character_id=attacker_character_id,
                cycle_anchor_date=cycle_anchor_date,
            ),
            changed=changed,
        )

    def _ensure_leaderboard_entry(
        self,
        *,
        character_id: int,
        now: datetime,
    ) -> tuple[tuple[PvpLeaderboardEntry, ...], PvpLeaderboardEntry]:
        leaderboard_entries = self._ensure_pvp_leaderboard(now=now)
        existing_entry = self._find_leaderboard_entry(leaderboard_entries, character_id=character_id)
        if existing_entry is not None:
            return leaderboard_entries, existing_entry
        aggregate = self._require_character_aggregate(character_id)
        progress = self._require_progress(aggregate)
        score_snapshot = self._require_score_snapshot(aggregate)
        rank_position = len(leaderboard_entries) + 1
        protected_until = self._rule_service.resolve_new_entry_protected_until(now=now)
        new_entry = self._build_seed_entry(
            aggregate=aggregate,
            progress=progress,
            score_snapshot=score_snapshot,
            rank_position=rank_position,
            protected_until=protected_until,
        )
        bundle = self._defense_snapshot_service.ensure_snapshot(
            character_id=character_id,
            now=now,
            requested_reason="enter_ladder",
            leaderboard_entry=new_entry,
        )
        updated_entries, _ = self._merge_latest_defense_versions(
            leaderboard_entries + (new_entry,),
            {character_id: bundle.snapshot_state.snapshot_version},
        )
        persisted_entries = self._persist_pvp_leaderboard(
            leaderboard_entries=updated_entries,
            generated_at=now,
            generated_by=_PVP_ENTER_LADDER_GENERATED_BY,
        )
        return persisted_entries, self._require_leaderboard_entry(persisted_entries, character_id=character_id)

    def _ensure_pvp_leaderboard(self, *, now: datetime) -> tuple[PvpLeaderboardEntry, ...]:
        latest_snapshot = self._snapshot_repository.get_latest_leaderboard(LeaderboardBoardType.PVP_CHALLENGE.value)
        if latest_snapshot is not None:
            return self._load_leaderboard_entries(latest_snapshot)
        seed_entries = self._build_seed_leaderboard_entries(now=now)
        return self._persist_pvp_leaderboard(
            leaderboard_entries=seed_entries,
            generated_at=now,
            generated_by=_PVP_SEED_GENERATED_BY,
        )

    def _build_seed_leaderboard_entries(self, *, now: datetime) -> tuple[PvpLeaderboardEntry, ...]:
        aggregates = [
            aggregate
            for aggregate in self._character_repository.list_aggregates_for_ranking()
            if aggregate.progress is not None and aggregate.score_snapshot is not None
        ]
        aggregates.sort(
            key=lambda aggregate: (
                -aggregate.character.hidden_pvp_score,
                -aggregate.character.public_power_score,
                aggregate.character.id,
            )
        )
        protected_until = self._rule_service.resolve_new_entry_protected_until(now=now)
        entries = [
            self._build_seed_entry(
                aggregate=aggregate,
                progress=self._require_progress(aggregate),
                score_snapshot=self._require_score_snapshot(aggregate),
                rank_position=index,
                protected_until=protected_until,
            )
            for index, aggregate in enumerate(aggregates, start=1)
        ]
        return tuple(entries)

    def _build_seed_entry(
        self,
        *,
        aggregate: CharacterAggregate,
        progress: CharacterProgress,
        score_snapshot: CharacterScoreSnapshot,
        rank_position: int,
        protected_until: datetime | None,
    ) -> PvpLeaderboardEntry:
        breakdown = score_snapshot.breakdown_json if isinstance(score_snapshot.breakdown_json, dict) else {}
        skill_breakdown = breakdown.get("skill") if isinstance(breakdown.get("skill"), dict) else {}
        reward_tier = self._rule_service.resolve_reward_tier(rank_position=rank_position)
        summary = {
            "character_name": aggregate.character.name,
            "character_title": aggregate.character.title,
            "realm_id": progress.realm_id,
            "realm_name": self._realm_name_by_id.get(progress.realm_id, progress.realm_id),
            "stage_id": progress.stage_id,
            "stage_name": self._stage_name_by_id.get(progress.stage_id, progress.stage_id),
            "main_path_id": skill_breakdown.get("main_path_id"),
            "main_path_name": skill_breakdown.get("main_path_name"),
            "main_skill_name": skill_breakdown.get("main_skill_name", skill_breakdown.get("main_path_name")),
            "main_skill": skill_breakdown.get("main_skill"),
            "auxiliary_skills": [
                skill_breakdown.get("guard_skill"),
                skill_breakdown.get("movement_skill"),
                skill_breakdown.get("spirit_skill"),
            ],
            "public_power_score": score_snapshot.public_power_score,
            "highest_endless_floor": progress.highest_endless_floor,
            "score_version": score_snapshot.score_version,
            "display_score": f"{reward_tier.reward_tier_id}·{score_snapshot.public_power_score}",
            "challenge_tier": reward_tier.reward_tier_id,
            "build_summary": {
                "main_skill_name": skill_breakdown.get("main_skill_name", skill_breakdown.get("main_path_name")),
                "main_skill": skill_breakdown.get("main_skill"),
                "auxiliary_skills": [
                    skill_breakdown.get("guard_skill"),
                    skill_breakdown.get("movement_skill"),
                    skill_breakdown.get("spirit_skill"),
                ],
                "public_power_score": score_snapshot.public_power_score,
                "pvp_adjustment_score": score_snapshot.pvp_adjustment_score,
                "highest_endless_floor": progress.highest_endless_floor,
            },
            "hidden_score_exposed": False,
            "latest_defense_snapshot_version": None,
            "best_rank": rank_position,
            "protected_until": None if protected_until is None else protected_until.isoformat(),
            "reward_preview_tier": reward_tier.reward_tier_id,
        }
        return PvpLeaderboardEntry(
            character_id=aggregate.character.id,
            rank_position=rank_position,
            public_power_score=score_snapshot.public_power_score,
            hidden_pvp_score=score_snapshot.hidden_pvp_score,
            realm_id=progress.realm_id,
            best_rank=rank_position,
            protected_until=protected_until,
            latest_defense_snapshot_version=None,
            challenge_tier=reward_tier.reward_tier_id,
            reward_preview_tier=reward_tier.reward_tier_id,
            summary=summary,
        )

    def _persist_pvp_leaderboard(
        self,
        *,
        leaderboard_entries: tuple[PvpLeaderboardEntry, ...],
        generated_at: datetime,
        generated_by: str,
    ) -> tuple[PvpLeaderboardEntry, ...]:
        score_version = self._resolve_leaderboard_score_version(leaderboard_entries)
        persisted_entries = [
            LeaderboardEntrySnapshot(
                character_id=entry.character_id,
                rank_position=entry.rank_position,
                score=entry.hidden_pvp_score,
                summary_json=self._serialize_leaderboard_summary(entry),
            )
            for entry in sorted(leaderboard_entries, key=lambda current: current.rank_position)
        ]
        snapshot = LeaderboardSnapshot(
            board_type=LeaderboardBoardType.PVP_CHALLENGE.value,
            generated_at=generated_at,
            scope_json={
                "schema_version": _PVP_LEADERBOARD_SCOPE_SCHEMA,
                "score_version": score_version,
                "entry_count": len(persisted_entries),
                "generated_by": generated_by,
                "board_type": LeaderboardBoardType.PVP_CHALLENGE.value,
            },
            entries=persisted_entries,
        )
        self._snapshot_repository.replace_leaderboard_snapshot(snapshot)
        latest_snapshot = self._snapshot_repository.get_latest_leaderboard(LeaderboardBoardType.PVP_CHALLENGE.value)
        if latest_snapshot is None:
            raise PvpStateError("写入 PVP 正式榜失败")
        return self._load_leaderboard_entries(latest_snapshot)

    def _build_target_list_snapshot(
        self,
        *,
        attacker_entry: PvpLeaderboardEntry,
        target_pool: PvpTargetPoolResult,
        cycle_anchor_date: date,
        generated_at: datetime,
        attacker_current_win_streak: int,
    ) -> PvpTargetListSnapshot:
        return PvpTargetListSnapshot(
            character_id=attacker_entry.character_id,
            cycle_anchor_date=cycle_anchor_date,
            generated_at=generated_at,
            current_rank_position=attacker_entry.rank_position,
            current_best_rank=attacker_entry.best_rank or attacker_entry.rank_position,
            targets=tuple(
                self._build_target_view(
                    attacker_entry=attacker_entry,
                    candidate=candidate,
                    attacker_current_win_streak=attacker_current_win_streak,
                )
                for candidate in target_pool.candidates
            ),
            rejected_targets=tuple(
                self._build_target_view(
                    attacker_entry=attacker_entry,
                    candidate=candidate,
                    attacker_current_win_streak=attacker_current_win_streak,
                )
                for candidate in target_pool.rejected_candidates
            ),
            fallback_triggered=target_pool.fallback_triggered,
            expansion_steps_applied=target_pool.expansion_steps_applied,
        )

    def _build_target_view(
        self,
        *,
        attacker_entry: PvpLeaderboardEntry,
        candidate: PvpTargetCandidate,
        attacker_current_win_streak: int,
    ) -> PvpTargetView:
        reward_preview = self._honor_coin_service.preview_challenge_rewards(
            attacker=attacker_entry,
            defender=candidate.leaderboard_entry,
            attacker_current_win_streak=attacker_current_win_streak,
            rank_position_on_win=candidate.rank_position,
        )
        summary = dict(candidate.leaderboard_entry.summary)
        display_summary = str(
            summary.get("display_score")
            or summary.get("character_name")
            or f"第 {candidate.rank_position} 名"
        )
        return PvpTargetView(
            character_id=candidate.character_id,
            rank_position=candidate.rank_position,
            rank_gap=candidate.rank_gap,
            public_power_score=candidate.leaderboard_entry.public_power_score,
            hidden_pvp_score=candidate.leaderboard_entry.hidden_pvp_score,
            challenge_tier=candidate.leaderboard_entry.challenge_tier,
            reward_preview_tier=candidate.leaderboard_entry.reward_preview_tier,
            latest_defense_snapshot_version=candidate.leaderboard_entry.latest_defense_snapshot_version,
            has_active_defense_snapshot=candidate.has_active_defense_snapshot,
            protected=candidate.protected,
            rejection_reasons=candidate.rejection_reasons,
            display_summary=display_summary,
            reward_preview=self._serialize_reward_preview(reward_preview),
            summary=self._json_ready(summary),
        )

    def _build_auto_battle_request(
        self,
        *,
        attacker_entry: PvpLeaderboardEntry,
        attacker_bundle: PvpDefenseSnapshotBundle,
        defender_entry: PvpLeaderboardEntry,
        defender_bundle: PvpDefenseSnapshotBundle,
        cycle_anchor_date: date,
        seed: int | None,
        now: datetime,
    ) -> AutoBattleRequest:
        attacker_summary = attacker_entry.summary if isinstance(attacker_entry.summary, dict) else {}
        ally_snapshot = self._build_unit_from_snapshot_state(
            snapshot_state=attacker_bundle.snapshot_state,
            side=BattleSide.ALLY,
            unit_id=f"pvp:attacker:{attacker_entry.character_id}",
            unit_name=str(attacker_summary.get("character_name") or f"角色{attacker_entry.character_id}"),
        )
        enemy_snapshot = defender_bundle.battle_unit_snapshot
        resolved_seed = seed if seed is not None else self._resolve_battle_seed(
            now=now,
            attacker_character_id=attacker_entry.character_id,
            defender_character_id=defender_entry.character_id,
        )
        template_patches_by_template_id = self._merge_template_patch_maps(
            self._build_template_patches_by_template_id(attacker_bundle.snapshot_state),
            self._build_template_patches_by_template_id(defender_bundle.snapshot_state),
        )
        template_path_id_by_template_id = self._merge_template_path_maps(
            self._build_template_path_id_by_template_id(attacker_bundle.snapshot_state),
            self._build_template_path_id_by_template_id(defender_bundle.snapshot_state),
        )
        return AutoBattleRequest(
            character_id=attacker_entry.character_id,
            battle_type=_PVP_BATTLE_TYPE,
            snapshot=BattleSnapshot(
                seed=resolved_seed,
                allies=(ally_snapshot,),
                enemies=(enemy_snapshot,),
                round_limit=_PVP_ROUND_LIMIT,
                environment_tags=(
                    _PVP_BATTLE_TYPE,
                    f"attacker_rank_{attacker_entry.rank_position}",
                    f"defender_rank_{defender_entry.rank_position}",
                ),
            ),
            opponent_ref=f"pvp:{defender_entry.character_id}:v{defender_bundle.snapshot_state.snapshot_version}",
            focus_unit_id=ally_snapshot.unit_id,
            environment_snapshot={
                "cycle_anchor_date": cycle_anchor_date.isoformat(),
                "attacker_character_id": attacker_entry.character_id,
                "attacker_rank": attacker_entry.rank_position,
                "attacker_snapshot_version": attacker_bundle.snapshot_state.snapshot_version,
                "defender_character_id": defender_entry.character_id,
                "defender_rank": defender_entry.rank_position,
                "defender_snapshot_version": defender_bundle.snapshot_state.snapshot_version,
            },
            template_patches_by_template_id=None if not template_patches_by_template_id else template_patches_by_template_id,
            template_path_id_by_template_id=None if not template_path_id_by_template_id else template_path_id_by_template_id,
            persist_progress_writeback=False,
        )

    @staticmethod
    def _build_unit_from_snapshot_state(
        *,
        snapshot_state: PvpDefenseSnapshotState,
        side: BattleSide,
        unit_id: str,
        unit_name: str,
    ) -> BattleUnitSnapshot:
        stats_payload = snapshot_state.stats if isinstance(snapshot_state.stats, dict) else {}
        max_hp = _read_int(stats_payload.get("max_hp"), default=1)
        max_resource = _read_int(stats_payload.get("max_resource"), default=100)
        special_effect_payloads = stats_payload.get("special_effect_payloads")
        normalized_special_effect_payloads = tuple(
            dict(payload)
            for payload in special_effect_payloads
            if isinstance(payload, dict)
        ) if isinstance(special_effect_payloads, list) else ()
        return BattleUnitSnapshot(
            unit_id=unit_id,
            unit_name=unit_name,
            side=side,
            behavior_template_id=str(stats_payload.get("behavior_template_id") or "zhanqing_sword"),
            realm_id=str(stats_payload.get("realm_id") or "mortal"),
            stage_id=str(stats_payload.get("stage_id") or "early"),
            max_hp=max_hp,
            current_hp=_read_int(stats_payload.get("current_hp"), default=max_hp),
            current_shield=_read_int(stats_payload.get("current_shield"), default=0),
            max_resource=max_resource,
            current_resource=_read_int(stats_payload.get("current_resource"), default=max_resource),
            attack_power=_read_int(stats_payload.get("attack_power"), default=1),
            guard_power=_read_int(stats_payload.get("guard_power"), default=0),
            speed=_read_int(stats_payload.get("speed"), default=1),
            crit_rate_permille=_read_int(stats_payload.get("crit_rate_permille"), default=0),
            crit_damage_bonus_permille=_read_int(stats_payload.get("crit_damage_bonus_permille"), default=0),
            hit_rate_permille=_read_int(stats_payload.get("hit_rate_permille"), default=1000),
            dodge_rate_permille=_read_int(stats_payload.get("dodge_rate_permille"), default=0),
            control_bonus_permille=_read_int(stats_payload.get("control_bonus_permille"), default=0),
            control_resist_permille=_read_int(stats_payload.get("control_resist_permille"), default=0),
            healing_power_permille=_read_int(stats_payload.get("healing_power_permille"), default=0),
            shield_power_permille=_read_int(stats_payload.get("shield_power_permille"), default=0),
            damage_bonus_permille=_read_int(stats_payload.get("damage_bonus_permille"), default=0),
            damage_reduction_permille=_read_int(stats_payload.get("damage_reduction_permille"), default=0),
            counter_rate_permille=_read_int(stats_payload.get("counter_rate_permille"), default=0),
            special_effect_payloads=normalized_special_effect_payloads,
        )

    @staticmethod
    def _build_template_patches_by_template_id(
        snapshot_state: PvpDefenseSnapshotState,
    ) -> dict[str, tuple[object, ...]]:
        stats_payload = snapshot_state.stats if isinstance(snapshot_state.stats, dict) else {}
        template_id = str(stats_payload.get("behavior_template_id") or "").strip()
        if not template_id:
            return {}
        patches = CurrentAttributeService.deserialize_template_patches(
            stats_payload.get("template_patch_payloads") if isinstance(stats_payload, dict) else None
        )
        if not patches:
            return {}
        return {template_id: patches}

    @staticmethod
    def _build_template_path_id_by_template_id(
        snapshot_state: PvpDefenseSnapshotState,
    ) -> dict[str, str]:
        stats_payload = snapshot_state.stats if isinstance(snapshot_state.stats, dict) else {}
        template_id = str(stats_payload.get("behavior_template_id") or "").strip()
        path_id = str(stats_payload.get("template_path_id") or template_id).strip()
        if not template_id or not path_id:
            return {}
        return {template_id: path_id}

    @staticmethod
    def _merge_template_patch_maps(
        *mappings: dict[str, tuple[object, ...]],
    ) -> dict[str, tuple[object, ...]]:
        merged: dict[str, tuple[object, ...]] = {}
        for mapping in mappings:
            for template_id, patches in mapping.items():
                merged[template_id] = merged.get(template_id, ()) + tuple(patches)
        return merged

    @staticmethod
    def _merge_template_path_maps(
        *mappings: dict[str, str],
    ) -> dict[str, str]:
        merged: dict[str, str] = {}
        for mapping in mappings:
            merged.update(mapping)
        return merged

    def _update_daily_activity_ledgers(
        self,
        *,
        attacker_daily_ledger: PvpDailyActivityLedger,
        defender_daily_ledger: PvpDailyActivityLedger | None,
        defender_character_id: int,
        cycle_anchor_date: date,
        battle_outcome: PvpBattleOutcome,
        occurred_at: datetime,
        defender_rank_position: int,
    ) -> None:
        attacker_daily_ledger.effective_challenge_count += 1
        attacker_daily_ledger.last_challenge_at = occurred_at
        if battle_outcome is PvpBattleOutcome.ALLY_VICTORY:
            attacker_daily_ledger.successful_challenge_count += 1
        self._pvp_challenge_repository.save_daily_activity(attacker_daily_ledger)
        if battle_outcome is not PvpBattleOutcome.ALLY_VICTORY:
            return
        defense_failure_cap = self._rule_service.resolve_defense_failure_cap(
            rank_position=defender_rank_position,
            defense_failure_count=0 if defender_daily_ledger is None else defender_daily_ledger.defense_failure_count,
        )
        if not defense_failure_cap.can_record_failure:
            return
        ledger = defender_daily_ledger or self._pvp_challenge_repository.get_or_create_daily_activity(
            defender_character_id,
            cycle_anchor_date,
        )
        ledger.defense_failure_count += 1
        ledger.last_challenge_at = occurred_at
        self._pvp_challenge_repository.save_daily_activity(ledger)

    def _calculate_current_win_streak(
        self,
        *,
        attacker_character_id: int,
        cycle_anchor_date: date,
    ) -> int:
        records = self._pvp_challenge_repository.list_challenge_records_by_attacker(
            attacker_character_id,
            cycle_anchor_date,
        )
        win_streak = 0
        for record in records:
            if record.battle_outcome != PvpBattleOutcome.ALLY_VICTORY.value:
                break
            win_streak += 1
        return win_streak

    def _build_challenge_record_settlement_payload(
        self,
        *,
        challenge_settlement: PvpChallengeSettlement,
        honor_coin_result: HonorCoinApplicationResult,
        battle_report_id: int,
        leaderboard_snapshot_id: int,
        defender_snapshot_id: int,
    ) -> dict[str, object]:
        return {
            "battle_outcome": challenge_settlement.battle_outcome.value,
            "battle_report_id": battle_report_id,
            "leaderboard_snapshot_id": leaderboard_snapshot_id,
            "defender_snapshot_id": defender_snapshot_id,
            "rank_change": {
                "attacker_rank_before": challenge_settlement.rank_change.attacker_rank_before,
                "attacker_rank_after": challenge_settlement.rank_change.attacker_rank_after,
                "defender_rank_before": challenge_settlement.rank_change.defender_rank_before,
                "defender_rank_after": challenge_settlement.rank_change.defender_rank_after,
                "rank_effect_applied": challenge_settlement.rank_change.rank_effect_applied,
                "affected_rank_range": None
                if challenge_settlement.rank_change.affected_rank_range is None
                else {
                    "rank_start": challenge_settlement.rank_change.affected_rank_range.rank_start,
                    "rank_end": challenge_settlement.rank_change.affected_rank_range.rank_end,
                },
                "rank_updates": [
                    {
                        "character_id": item.character_id,
                        "rank_before": item.rank_before,
                        "rank_after": item.rank_after,
                        "rank_shift": item.rank_shift,
                    }
                    for item in challenge_settlement.rank_change.rank_updates
                ],
            },
            "honor_coin": dict(honor_coin_result.detail),
            "anti_abuse_flags": list(challenge_settlement.anti_abuse_flags),
            "reward_preview": None
            if challenge_settlement.reward_preview is None
            else self._serialize_reward_preview(challenge_settlement.reward_preview),
            "display_rewards": [
                self._serialize_reward_item(item)
                for item in challenge_settlement.display_rewards
            ],
        }

    def _build_result_settlement_payload(
        self,
        *,
        challenge_settlement: PvpChallengeSettlement,
        honor_coin_result: HonorCoinApplicationResult,
        challenge_record_id: int,
    ) -> dict[str, object]:
        payload = self._build_challenge_record_settlement_payload(
            challenge_settlement=challenge_settlement,
            honor_coin_result=honor_coin_result,
            battle_report_id=int(honor_coin_result.detail.get("battle_report_id") or 0),
            leaderboard_snapshot_id=int(honor_coin_result.detail.get("leaderboard_snapshot_id") or 0),
            defender_snapshot_id=int(honor_coin_result.detail.get("defender_snapshot_id") or 0),
        )
        payload["challenge_record_id"] = challenge_record_id
        return payload

    def _load_leaderboard_entries(self, snapshot: LeaderboardSnapshot) -> tuple[PvpLeaderboardEntry, ...]:
        ordered_entries = sorted(snapshot.entries, key=lambda entry: entry.rank_position)
        return tuple(self._to_leaderboard_entry(entry) for entry in ordered_entries)

    def _to_leaderboard_entry(self, entry_model: LeaderboardEntrySnapshot) -> PvpLeaderboardEntry:
        summary = dict(entry_model.summary_json) if isinstance(entry_model.summary_json, dict) else {}
        return PvpLeaderboardEntry(
            character_id=entry_model.character_id,
            rank_position=entry_model.rank_position,
            public_power_score=_read_int(summary.get("public_power_score"), default=0),
            hidden_pvp_score=entry_model.score,
            realm_id=str(summary.get("realm_id") or "mortal"),
            best_rank=_read_optional_int(summary.get("best_rank")),
            protected_until=_read_optional_datetime(summary.get("protected_until")),
            latest_defense_snapshot_version=_read_optional_int(summary.get("latest_defense_snapshot_version")),
            challenge_tier=_read_optional_str(summary.get("challenge_tier")),
            reward_preview_tier=_read_optional_str(summary.get("reward_preview_tier")),
            summary=summary,
        )

    @staticmethod
    def _merge_latest_defense_versions(
        leaderboard_entries: tuple[PvpLeaderboardEntry, ...],
        latest_version_by_character_id: dict[int, int | None],
    ) -> tuple[tuple[PvpLeaderboardEntry, ...], bool]:
        updated_entries: list[PvpLeaderboardEntry] = []
        changed = False
        for entry in leaderboard_entries:
            resolved_version = latest_version_by_character_id.get(
                entry.character_id,
                entry.latest_defense_snapshot_version,
            )
            if resolved_version != entry.latest_defense_snapshot_version:
                summary = dict(entry.summary)
                summary["latest_defense_snapshot_version"] = resolved_version
                updated_entries.append(
                    replace(
                        entry,
                        latest_defense_snapshot_version=resolved_version,
                        summary=summary,
                    )
                )
                changed = True
                continue
            updated_entries.append(entry)
        return tuple(updated_entries), changed

    @staticmethod
    def _find_leaderboard_entry(
        leaderboard_entries: tuple[PvpLeaderboardEntry, ...],
        *,
        character_id: int,
    ) -> PvpLeaderboardEntry | None:
        for entry in leaderboard_entries:
            if entry.character_id == character_id:
                return entry
        return None

    def _require_leaderboard_entry(
        self,
        leaderboard_entries: tuple[PvpLeaderboardEntry, ...],
        *,
        character_id: int,
    ) -> PvpLeaderboardEntry:
        entry = self._find_leaderboard_entry(leaderboard_entries, character_id=character_id)
        if entry is None:
            raise PvpTargetNotFoundError(f"当前榜单中不存在角色：{character_id}")
        return entry

    def _serialize_leaderboard_summary(self, entry: PvpLeaderboardEntry) -> dict[str, object]:
        summary = dict(entry.summary)
        summary.setdefault("public_power_score", entry.public_power_score)
        summary.setdefault("best_rank", entry.best_rank or entry.rank_position)
        summary.setdefault("display_score", f"{entry.challenge_tier or 'pvp'}·{entry.public_power_score}")
        summary["latest_defense_snapshot_version"] = entry.latest_defense_snapshot_version
        summary["challenge_tier"] = entry.challenge_tier
        summary["reward_preview_tier"] = entry.reward_preview_tier
        if entry.protected_until is not None:
            summary["protected_until"] = entry.protected_until.isoformat()
        elif "protected_until" not in summary:
            summary["protected_until"] = None
        return self._json_ready(summary)

    def _resolve_leaderboard_score_version(self, leaderboard_entries: tuple[PvpLeaderboardEntry, ...]) -> str:
        for entry in leaderboard_entries:
            score_version = entry.summary.get("score_version") if isinstance(entry.summary, dict) else None
            if isinstance(score_version, str) and score_version:
                return score_version
        return self._static_config.pvp.config_version

    def _require_character_aggregate(self, character_id: int) -> CharacterAggregate:
        aggregate = self._character_repository.get_aggregate(character_id)
        if aggregate is None:
            raise CharacterNotFoundError(f"角色不存在：{character_id}")
        return aggregate

    @staticmethod
    def _require_progress(aggregate: CharacterAggregate) -> CharacterProgress:
        if aggregate.progress is None:
            raise CharacterGrowthStateError(f"角色缺少成长状态：{aggregate.character.id}")
        return aggregate.progress

    @staticmethod
    def _require_score_snapshot(aggregate: CharacterAggregate) -> CharacterScoreSnapshot:
        if aggregate.score_snapshot is None:
            raise PvpStateError(f"角色缺少评分明细快照：{aggregate.character.id}")
        return aggregate.score_snapshot

    @staticmethod
    def _require_snapshot_id(snapshot_state: PvpDefenseSnapshotState, *, character_id: int) -> int:
        if snapshot_state.snapshot_id is None or snapshot_state.snapshot_id <= 0:
            raise PvpStateError(f"角色缺少有效防守快照主键：{character_id}")
        return snapshot_state.snapshot_id

    @staticmethod
    def _to_daily_activity_snapshot(ledger: PvpDailyActivityLedger) -> PvpDailyActivitySnapshot:
        return PvpDailyActivitySnapshot(
            character_id=ledger.character_id,
            cycle_anchor_date=ledger.cycle_anchor_date,
            effective_challenge_count=max(0, ledger.effective_challenge_count),
            successful_challenge_count=max(0, ledger.successful_challenge_count),
            defense_failure_count=max(0, ledger.defense_failure_count),
            last_challenge_at=ledger.last_challenge_at,
        )

    @staticmethod
    def _build_empty_daily_activity(*, character_id: int, cycle_anchor_date: date) -> PvpDailyActivitySnapshot:
        return PvpDailyActivitySnapshot(
            character_id=character_id,
            cycle_anchor_date=cycle_anchor_date,
            effective_challenge_count=0,
            successful_challenge_count=0,
            defense_failure_count=0,
            last_challenge_at=None,
        )

    def _resolve_cycle_anchor_date(self, current_time: datetime) -> date:
        return current_time.replace(tzinfo=UTC).astimezone(self._cycle_timezone).date()

    @staticmethod
    def _resolve_current_time(current_time: datetime | None) -> datetime:
        if current_time is None:
            return datetime.utcnow()
        if current_time.tzinfo is not None:
            return current_time.astimezone(UTC).replace(tzinfo=None)
        return current_time

    @staticmethod
    def _resolve_battle_seed(
        *,
        now: datetime,
        attacker_character_id: int,
        defender_character_id: int,
    ) -> int:
        return int(now.timestamp()) * 1009 + attacker_character_id * 37 + defender_character_id * 53

    @staticmethod
    def _map_battle_outcome(outcome: object) -> PvpBattleOutcome:
        if not isinstance(outcome, BattleOutcome):
            raise PvpStateError(f"自动战斗返回了无效战斗结果：{outcome}")
        if outcome is BattleOutcome.ALLY_VICTORY:
            return PvpBattleOutcome.ALLY_VICTORY
        if outcome is BattleOutcome.ENEMY_VICTORY:
            return PvpBattleOutcome.ENEMY_VICTORY
        return PvpBattleOutcome.DRAW

    @staticmethod
    def _serialize_reward_preview(preview: PvpRewardPreview) -> dict[str, object]:
        return {
            "reward_tier_id": preview.reward_tier_id,
            "rank_range": {
                "rank_start": preview.rank_range.rank_start,
                "rank_end": preview.rank_range.rank_end,
            },
            "honor_coin_on_win": preview.honor_coin_on_win,
            "honor_coin_on_loss": preview.honor_coin_on_loss,
            "summary": preview.summary,
            "display_items": [
                PvpService._serialize_reward_item(item)
                for item in preview.display_items
            ],
        }

    @staticmethod
    def _serialize_reward_item(item: PvpRewardDisplayItem) -> dict[str, object]:
        return {
            "reward_id": item.reward_id,
            "reward_type": item.reward_type.value,
            "name": item.name,
            "rarity": item.rarity,
            "state": item.state.value,
            "source": item.source.value,
            "meta": dict(item.meta),
        }

    @classmethod
    def _json_ready(cls, value: object) -> object:
        if isinstance(value, dict):
            return {str(key): cls._json_ready(current) for key, current in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [cls._json_ready(current) for current in value]
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, date):
            return value.isoformat()
        return value



def _read_int(value: object, *, default: int = 0) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    return default



def _read_optional_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    return None



def _read_optional_str(value: object) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None



def _read_optional_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


__all__ = [
    "PvpChallengeNotAllowedError",
    "PvpChallengeResult",
    "PvpHubSnapshot",
    "PvpService",
    "PvpServiceError",
    "PvpStateError",
    "PvpTargetListSnapshot",
    "PvpTargetNotFoundError",
    "PvpTargetView",
]
