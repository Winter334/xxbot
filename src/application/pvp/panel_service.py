"""PVP 面板查询适配服务。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from application.character.panel_query_service import CharacterPanelOverview, CharacterPanelQueryService
from application.pvp.pvp_service import PvpHubSnapshot, PvpService, PvpTargetView
from domain.ranking import LeaderboardBoardType
from infrastructure.config.static import StaticGameConfig, get_static_config
from infrastructure.db.models import BattleReport
from infrastructure.db.repositories import BattleRecordRepository, PvpChallengeRepository, SnapshotRepository

_PVP_BOARD_TYPE = LeaderboardBoardType.PVP_CHALLENGE.value
_VISIBLE_REWARD_TYPE_NAME_BY_VALUE = {
    "title": "称号",
    "badge": "徽记",
}


@dataclass(frozen=True, slots=True)
class PvpBattleReportDigest:
    """PVP 最近一次战斗的战报摘要。"""

    battle_report_id: int
    result: str
    completed_rounds: int
    focus_unit_name: str
    final_hp_ratio: str
    final_mp_ratio: str
    ally_damage_dealt: int
    ally_damage_taken: int
    ally_healing_done: int
    successful_hits: int
    critical_hits: int
    control_skips: int


@dataclass(frozen=True, slots=True)
class PvpRewardCard:
    """PVP 奖励卡片摘要。"""

    tier_name: str | None
    summary: str | None
    honor_coin_on_win: int
    honor_coin_on_loss: int
    visible_reward_lines: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PvpStatusCard:
    """PVP 我方状态卡。"""

    rank_position: int
    best_rank: int
    remaining_challenge_count: int
    daily_challenge_limit: int
    used_challenge_count: int
    honor_coin_balance: int
    public_power_score: int
    hidden_pvp_score: int
    reward_tier_name: str | None
    protected_until: str | None


@dataclass(frozen=True, slots=True)
class PvpOpponentCard:
    """PVP 对手卡。"""

    character_id: int
    character_name: str
    character_title: str | None
    rank_position: int
    realm_name: str | None
    stage_name: str | None
    main_path_name: str | None
    public_power_score: int
    hidden_pvp_score: int
    rank_gap: int
    display_summary: str | None
    reward_card: PvpRewardCard


@dataclass(frozen=True, slots=True)
class PvpRecentResultCard:
    """PVP 最近结果卡。"""

    opponent_name: str
    outcome: str
    occurred_at: datetime
    rank_before: int
    rank_after: int
    rank_shift: int
    honor_coin_delta: int


@dataclass(frozen=True, slots=True)
class PvpRecentSettlementSnapshot:
    """PVP 最近一次可复读的结算快照。"""

    challenge_record_id: int
    occurred_at: datetime
    cycle_anchor_date: date
    attacker_character_id: int
    defender_character_id: int
    defender_snapshot_id: int
    leaderboard_snapshot_id: int
    battle_report_id: int
    battle_outcome: str
    rank_before_attacker: int
    rank_after_attacker: int
    rank_before_defender: int
    rank_after_defender: int
    rank_effect_applied: bool
    honor_coin_delta: int
    honor_coin_balance_after: int | None
    anti_abuse_flags: tuple[str, ...]
    reward_preview: dict[str, object] | None
    display_rewards: tuple[dict[str, object], ...]
    reward_card: PvpRewardCard
    opponent_card: PvpOpponentCard
    recent_result_card: PvpRecentResultCard
    settlement_payload: dict[str, object]
    battle_report_digest: PvpBattleReportDigest | None
    defender_summary: dict[str, object]


@dataclass(frozen=True, slots=True)
class PvpPanelSnapshot:
    """PVP 私有面板所需的聚合快照。"""

    overview: CharacterPanelOverview
    hub: PvpHubSnapshot
    current_hidden_pvp_score: int
    current_public_power_score: int
    current_challenge_tier: str | None
    current_reward_tier_name: str | None
    current_entry_summary: dict[str, object]
    daily_challenge_limit: int
    repeat_target_limit: int
    status_card: PvpStatusCard
    target_cards: tuple[PvpOpponentCard, ...]
    recent_result_card: PvpRecentResultCard | None
    recent_settlement: PvpRecentSettlementSnapshot | None


class PvpPanelServiceError(RuntimeError):
    """PVP 面板查询服务基础异常。"""


class PvpPanelService:
    """聚合角色总览、PVP hub 与最近一次挑战结算。"""

    def __init__(
        self,
        *,
        character_panel_query_service: CharacterPanelQueryService,
        pvp_service: PvpService,
        snapshot_repository: SnapshotRepository,
        pvp_challenge_repository: PvpChallengeRepository,
        battle_record_repository: BattleRecordRepository,
        static_config: StaticGameConfig | None = None,
    ) -> None:
        self._character_panel_query_service = character_panel_query_service
        self._pvp_service = pvp_service
        self._snapshot_repository = snapshot_repository
        self._pvp_challenge_repository = pvp_challenge_repository
        self._battle_record_repository = battle_record_repository
        self._static_config = static_config or get_static_config()
        self._reward_tier_name_by_id = {
            tier.reward_tier_id: tier.name for tier in self._static_config.pvp.ordered_reward_tiers
        }
        self._daily_challenge_limit = self._static_config.pvp.daily_limit.effective_challenge_limit
        self._repeat_target_limit = self._static_config.pvp.daily_limit.repeat_target_limit

    def get_panel_snapshot(self, *, character_id: int) -> PvpPanelSnapshot:
        """读取 PVP 主面板所需聚合数据。"""
        overview = self._character_panel_query_service.get_overview(character_id=character_id)
        hub = self._pvp_service.get_pvp_hub(character_id=character_id)
        leaderboard_entry = self._snapshot_repository.get_latest_leaderboard_entry(_PVP_BOARD_TYPE, character_id)
        entry_summary = _normalize_optional_mapping(
            None if leaderboard_entry is None else leaderboard_entry.summary_json,
        ) or {}
        current_challenge_tier = _read_optional_str(entry_summary.get("challenge_tier")) or _read_optional_str(
            hub.reward_preview.get("reward_tier_id"),
        )
        current_public_power_score = _read_int(
            entry_summary.get("public_power_score"),
            default=overview.public_power_score,
        )
        current_hidden_pvp_score = 0 if leaderboard_entry is None else max(0, int(leaderboard_entry.score))
        recent_settlement = self.get_recent_settlement_snapshot(character_id=character_id)
        current_reward_tier_name = None
        if current_challenge_tier is not None:
            current_reward_tier_name = self._reward_tier_name_by_id.get(current_challenge_tier)
        return PvpPanelSnapshot(
            overview=overview,
            hub=hub,
            current_hidden_pvp_score=current_hidden_pvp_score,
            current_public_power_score=current_public_power_score,
            current_challenge_tier=current_challenge_tier,
            current_reward_tier_name=current_reward_tier_name,
            current_entry_summary=entry_summary,
            daily_challenge_limit=self._daily_challenge_limit,
            repeat_target_limit=self._repeat_target_limit,
            status_card=self._build_status_card(
                hub=hub,
                current_public_power_score=current_public_power_score,
                current_hidden_pvp_score=current_hidden_pvp_score,
                current_reward_tier_name=current_reward_tier_name,
            ),
            target_cards=tuple(self._build_target_card(target=target) for target in hub.target_list.targets),
            recent_result_card=None if recent_settlement is None else recent_settlement.recent_result_card,
            recent_settlement=recent_settlement,
        )

    def get_recent_settlement_snapshot(self, *, character_id: int) -> PvpRecentSettlementSnapshot | None:
        """读取最近一次 PVP 挑战结算及其展示上下文。"""
        challenge_record = self._pvp_challenge_repository.get_latest_challenge_record_by_attacker(character_id)
        if challenge_record is None:
            return None
        settlement_payload = _normalize_mapping(challenge_record.settlement_json)
        honor_coin_payload = _normalize_optional_mapping(settlement_payload.get("honor_coin")) or {}
        reward_preview = _normalize_optional_mapping(settlement_payload.get("reward_preview"))
        display_rewards = tuple(_normalize_mapping_sequence(settlement_payload.get("display_rewards")))
        defender_summary = self._load_defender_summary(snapshot_id=challenge_record.defender_snapshot_id)
        reward_card = self._build_reward_card(
            reward_preview=reward_preview,
            display_rewards=display_rewards,
        )
        opponent_card = self._build_opponent_card_from_summary(
            character_id=challenge_record.defender_character_id,
            rank_position=challenge_record.rank_before_defender,
            rank_gap=challenge_record.rank_before_defender - challenge_record.rank_before_attacker,
            public_power_score=_read_int(defender_summary.get("public_power_score")),
            hidden_pvp_score=_read_int(defender_summary.get("hidden_pvp_score")),
            summary=defender_summary,
            reward_card=reward_card,
        )
        recent_result_card = self._build_recent_result_card(
            opponent_name=opponent_card.character_name,
            outcome=challenge_record.battle_outcome,
            occurred_at=challenge_record.created_at,
            rank_before=challenge_record.rank_before_attacker,
            rank_after=challenge_record.rank_after_attacker,
            honor_coin_delta=challenge_record.honor_coin_delta,
        )
        return PvpRecentSettlementSnapshot(
            challenge_record_id=challenge_record.id,
            occurred_at=challenge_record.created_at,
            cycle_anchor_date=challenge_record.cycle_anchor_date,
            attacker_character_id=challenge_record.attacker_character_id,
            defender_character_id=challenge_record.defender_character_id,
            defender_snapshot_id=challenge_record.defender_snapshot_id,
            leaderboard_snapshot_id=challenge_record.leaderboard_snapshot_id,
            battle_report_id=challenge_record.battle_report_id,
            battle_outcome=challenge_record.battle_outcome,
            rank_before_attacker=challenge_record.rank_before_attacker,
            rank_after_attacker=challenge_record.rank_after_attacker,
            rank_before_defender=challenge_record.rank_before_defender,
            rank_after_defender=challenge_record.rank_after_defender,
            rank_effect_applied=bool(challenge_record.rank_effect_applied),
            honor_coin_delta=challenge_record.honor_coin_delta,
            honor_coin_balance_after=_read_optional_int(honor_coin_payload.get("balance_after")),
            anti_abuse_flags=_normalize_str_sequence(settlement_payload.get("anti_abuse_flags")),
            reward_preview=reward_preview,
            display_rewards=display_rewards,
            reward_card=reward_card,
            opponent_card=opponent_card,
            recent_result_card=recent_result_card,
            settlement_payload=settlement_payload,
            battle_report_digest=self._load_battle_report_digest(
                character_id=character_id,
                battle_report_id=challenge_record.battle_report_id,
            ),
            defender_summary=defender_summary,
        )

    def _build_status_card(
        self,
        *,
        hub: PvpHubSnapshot,
        current_public_power_score: int,
        current_hidden_pvp_score: int,
        current_reward_tier_name: str | None,
    ) -> PvpStatusCard:
        used_challenge_count = max(0, self._daily_challenge_limit - hub.remaining_challenge_count)
        return PvpStatusCard(
            rank_position=hub.current_rank_position,
            best_rank=hub.current_best_rank,
            remaining_challenge_count=hub.remaining_challenge_count,
            daily_challenge_limit=self._daily_challenge_limit,
            used_challenge_count=used_challenge_count,
            honor_coin_balance=hub.honor_coin_balance,
            public_power_score=current_public_power_score,
            hidden_pvp_score=current_hidden_pvp_score,
            reward_tier_name=current_reward_tier_name,
            protected_until=hub.protected_until,
        )

    def _build_target_card(self, *, target: PvpTargetView) -> PvpOpponentCard:
        summary = _normalize_optional_mapping(target.summary) or {}
        reward_preview = _normalize_optional_mapping(target.reward_preview) or {}
        reward_tier_id = (
            _read_optional_str(reward_preview.get("reward_tier_id"))
            or target.reward_preview_tier
            or target.challenge_tier
        )
        reward_card = self._build_reward_card(
            reward_preview=reward_preview,
            fallback_tier_id=reward_tier_id,
        )
        return self._build_opponent_card_from_summary(
            character_id=target.character_id,
            rank_position=target.rank_position,
            rank_gap=target.rank_gap,
            public_power_score=target.public_power_score,
            hidden_pvp_score=target.hidden_pvp_score,
            summary=summary,
            reward_card=reward_card,
            fallback_name=target.display_summary,
        )

    def _build_opponent_card_from_summary(
        self,
        *,
        character_id: int,
        rank_position: int,
        rank_gap: int,
        public_power_score: int,
        hidden_pvp_score: int,
        summary: Mapping[str, Any],
        reward_card: PvpRewardCard,
        fallback_name: str | None = None,
    ) -> PvpOpponentCard:
        character_name = _read_optional_str(summary.get("character_name")) or fallback_name or f"角色 {character_id}"
        main_path_name = _read_optional_str(summary.get("main_skill_name")) or _read_optional_str(
            summary.get("main_path_name"),
        )
        display_summary = _read_optional_str(summary.get("display_summary")) or fallback_name
        return PvpOpponentCard(
            character_id=character_id,
            character_name=character_name,
            character_title=_read_optional_str(summary.get("character_title")),
            rank_position=rank_position,
            realm_name=_read_optional_str(summary.get("realm_name")),
            stage_name=_read_optional_str(summary.get("stage_name")),
            main_path_name=main_path_name,
            public_power_score=public_power_score,
            hidden_pvp_score=hidden_pvp_score,
            rank_gap=rank_gap,
            display_summary=display_summary,
            reward_card=reward_card,
        )

    def _build_reward_card(
        self,
        *,
        reward_preview: Mapping[str, Any] | None,
        fallback_tier_id: str | None = None,
        display_rewards: Sequence[Mapping[str, object]] = (),
    ) -> PvpRewardCard:
        normalized_preview = _normalize_optional_mapping(reward_preview) or {}
        reward_tier_id = _read_optional_str(normalized_preview.get("reward_tier_id")) or fallback_tier_id
        tier_name = None if reward_tier_id is None else self._reward_tier_name_by_id.get(reward_tier_id)
        if tier_name is None:
            tier_name = _read_optional_str(normalized_preview.get("summary"))
        visible_reward_items = list(_normalize_mapping_sequence(normalized_preview.get("display_items")))
        for reward in display_rewards:
            if isinstance(reward, Mapping):
                visible_reward_items.append({str(key): item for key, item in reward.items()})
        return PvpRewardCard(
            tier_name=tier_name,
            summary=_read_optional_str(normalized_preview.get("summary")),
            honor_coin_on_win=_read_int(normalized_preview.get("honor_coin_on_win")),
            honor_coin_on_loss=_read_int(normalized_preview.get("honor_coin_on_loss")),
            visible_reward_lines=self._format_visible_reward_lines(visible_reward_items),
        )

    @staticmethod
    def _format_visible_reward_lines(rewards: Sequence[Mapping[str, object]]) -> tuple[str, ...]:
        lines: list[str] = []
        for reward in rewards:
            reward_type = _read_optional_str(reward.get("reward_type"))
            if reward_type not in _VISIBLE_REWARD_TYPE_NAME_BY_VALUE:
                continue
            reward_name = _read_optional_str(reward.get("name")) or "-"
            state = _read_optional_str(reward.get("state"))
            state_suffix = ""
            if state == "unlocked_now":
                state_suffix = "（本次）"
            elif state == "owned":
                state_suffix = "（已持有）"
            line = f"{_VISIBLE_REWARD_TYPE_NAME_BY_VALUE[reward_type]}｜{reward_name}{state_suffix}"
            if line not in lines:
                lines.append(line)
        return tuple(lines)

    @staticmethod
    def _build_recent_result_card(
        *,
        opponent_name: str,
        outcome: str,
        occurred_at: datetime,
        rank_before: int,
        rank_after: int,
        honor_coin_delta: int,
    ) -> PvpRecentResultCard:
        return PvpRecentResultCard(
            opponent_name=opponent_name,
            outcome=outcome,
            occurred_at=occurred_at,
            rank_before=rank_before,
            rank_after=rank_after,
            rank_shift=rank_before - rank_after,
            honor_coin_delta=honor_coin_delta,
        )

    def _load_defender_summary(self, *, snapshot_id: int) -> dict[str, object]:
        snapshot_model = self._snapshot_repository.get_pvp_defense_snapshot(snapshot_id)
        if snapshot_model is None:
            return {}
        return _normalize_optional_mapping(snapshot_model.summary_json) or {}

    def _load_battle_report_digest(
        self,
        *,
        character_id: int,
        battle_report_id: int | None,
    ) -> PvpBattleReportDigest | None:
        if battle_report_id is None:
            return None
        for battle_report in self._battle_record_repository.list_battle_reports(character_id):
            if battle_report.id == battle_report_id:
                return self._build_battle_report_digest(battle_report)
        return None

    @staticmethod
    def _build_battle_report_digest(battle_report: BattleReport) -> PvpBattleReportDigest:
        summary_payload = _normalize_optional_mapping(battle_report.summary_json) or {}
        damage_summary = _normalize_int_mapping(summary_payload.get("damage_summary"))
        healing_summary = _normalize_int_mapping(summary_payload.get("healing_summary"))
        key_trigger_counts = _normalize_int_mapping(summary_payload.get("key_trigger_counts"))
        return PvpBattleReportDigest(
            battle_report_id=battle_report.id,
            result=str(summary_payload.get("result") or battle_report.result),
            completed_rounds=_read_int(summary_payload.get("completed_rounds")),
            focus_unit_name=str(summary_payload.get("focus_unit_name") or f"角色 {battle_report.character_id}"),
            final_hp_ratio=str(summary_payload.get("final_hp_ratio") or "0.0000"),
            final_mp_ratio=str(summary_payload.get("final_mp_ratio") or "0.0000"),
            ally_damage_dealt=_read_int(damage_summary.get("ally_damage_dealt")),
            ally_damage_taken=_read_int(damage_summary.get("ally_damage_taken")),
            ally_healing_done=_read_int(healing_summary.get("ally_healing_done")),
            successful_hits=_read_int(key_trigger_counts.get("successful_hits")),
            critical_hits=_read_int(key_trigger_counts.get("critical_hits")),
            control_skips=_read_int(key_trigger_counts.get("control_skips")),
        )


def _normalize_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    return {}


def _normalize_optional_mapping(value: Any) -> dict[str, Any] | None:
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    return None


def _normalize_mapping_sequence(value: Any) -> list[dict[str, object]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    normalized: list[dict[str, object]] = []
    for entry in value:
        if isinstance(entry, Mapping):
            normalized.append({str(key): item for key, item in entry.items()})
    return normalized


def _normalize_str_sequence(value: Any) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return ()
    normalized: list[str] = []
    for entry in value:
        if isinstance(entry, str) and entry:
            normalized.append(entry)
    return tuple(normalized)


def _normalize_int_mapping(value: Any) -> dict[str, int]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): _read_int(item) for key, item in value.items()}


def _read_int(value: Any, *, default: int = 0) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    return default


def _read_optional_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    return None


def _read_optional_str(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


__all__ = [
    "PvpBattleReportDigest",
    "PvpOpponentCard",
    "PvpPanelService",
    "PvpPanelServiceError",
    "PvpPanelSnapshot",
    "PvpRecentResultCard",
    "PvpRecentSettlementSnapshot",
    "PvpRewardCard",
    "PvpStatusCard",
]
