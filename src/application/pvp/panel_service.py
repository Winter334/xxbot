"""PVP 面板查询适配服务。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from application.character.panel_query_service import CharacterPanelOverview, CharacterPanelQueryService
from application.pvp.pvp_service import PvpHubSnapshot, PvpService
from domain.ranking import LeaderboardBoardType
from infrastructure.config.static import StaticGameConfig, get_static_config
from infrastructure.db.models import BattleReport, PvpChallengeRecord, PvpDefenseSnapshot
from infrastructure.db.repositories import BattleRecordRepository, PvpChallengeRepository, SnapshotRepository

_PVP_BOARD_TYPE = LeaderboardBoardType.PVP_CHALLENGE.value


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
        return PvpPanelSnapshot(
            overview=overview,
            hub=hub,
            current_hidden_pvp_score=0 if leaderboard_entry is None else max(0, int(leaderboard_entry.score)),
            current_public_power_score=current_public_power_score,
            current_challenge_tier=current_challenge_tier,
            current_reward_tier_name=None
            if current_challenge_tier is None
            else self._reward_tier_name_by_id.get(current_challenge_tier),
            current_entry_summary=entry_summary,
            daily_challenge_limit=self._daily_challenge_limit,
            repeat_target_limit=self._repeat_target_limit,
            recent_settlement=self.get_recent_settlement_snapshot(character_id=character_id),
        )

    def get_recent_settlement_snapshot(self, *, character_id: int) -> PvpRecentSettlementSnapshot | None:
        """读取最近一次 PVP 挑战结算及其展示上下文。"""
        challenge_record = self._pvp_challenge_repository.get_latest_challenge_record_by_attacker(character_id)
        if challenge_record is None:
            return None
        settlement_payload = _normalize_mapping(challenge_record.settlement_json)
        honor_coin_payload = _normalize_optional_mapping(settlement_payload.get("honor_coin")) or {}
        defender_summary = self._load_defender_summary(snapshot_id=challenge_record.defender_snapshot_id)
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
            reward_preview=_normalize_optional_mapping(settlement_payload.get("reward_preview")),
            display_rewards=tuple(_normalize_mapping_sequence(settlement_payload.get("display_rewards"))),
            settlement_payload=settlement_payload,
            battle_report_digest=self._load_battle_report_digest(
                character_id=character_id,
                battle_report_id=challenge_record.battle_report_id,
            ),
            defender_summary=defender_summary,
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
    "PvpPanelService",
    "PvpPanelServiceError",
    "PvpPanelSnapshot",
    "PvpRecentSettlementSnapshot",
]
