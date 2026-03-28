"""突破秘境面板查询适配服务。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from application.breakthrough.reward_service import BreakthroughRewardApplicationResult
from application.breakthrough.trial_service import BreakthroughTrialHubSnapshot, BreakthroughTrialService
from application.character.panel_query_service import CharacterPanelOverview, CharacterPanelQueryService
from application.character.progression_service import BreakthroughPrecheckResult, CharacterProgressionService
from infrastructure.config.static import StaticGameConfig, get_static_config
from infrastructure.config.static.models.breakthrough import BreakthroughTrialDefinition
from infrastructure.db.models import BattleReport, BreakthroughTrialProgress
from infrastructure.db.repositories import BattleRecordRepository, BreakthroughRepository


@dataclass(frozen=True, slots=True)
class BreakthroughBattleReportDigest:
    """突破秘境最近一次战斗的战报摘要。"""

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
class BreakthroughRecentSettlementSnapshot:
    """突破秘境最近一次可复读的结算快照。"""

    mapping_id: str
    trial_name: str
    group_id: str
    group_name: str
    occurred_at: datetime
    settlement: BreakthroughRewardApplicationResult
    battle_report_digest: BreakthroughBattleReportDigest | None


@dataclass(frozen=True, slots=True)
class BreakthroughPanelSnapshot:
    """突破秘境私有面板所需聚合快照。"""

    overview: CharacterPanelOverview
    precheck: BreakthroughPrecheckResult
    hub: BreakthroughTrialHubSnapshot
    recent_settlement: BreakthroughRecentSettlementSnapshot | None


class BreakthroughPanelServiceError(RuntimeError):
    """突破秘境面板查询服务基础异常。"""


class BreakthroughPanelService:
    """聚合角色总览、突破预检、试炼入口与最近结算。"""

    def __init__(
        self,
        *,
        character_panel_query_service: CharacterPanelQueryService,
        progression_service: CharacterProgressionService,
        trial_service: BreakthroughTrialService,
        breakthrough_repository: BreakthroughRepository,
        battle_record_repository: BattleRecordRepository,
        static_config: StaticGameConfig | None = None,
    ) -> None:
        self._character_panel_query_service = character_panel_query_service
        self._progression_service = progression_service
        self._trial_service = trial_service
        self._breakthrough_repository = breakthrough_repository
        self._battle_record_repository = battle_record_repository
        self._static_config = static_config or get_static_config()
        self._trial_by_mapping_id = {
            trial.mapping_id: trial for trial in self._static_config.breakthrough_trials.trials
        }
        self._group_name_by_id = {
            group.group_id: group.name for group in self._static_config.breakthrough_trials.trial_groups
        }

    def get_panel_snapshot(self, *, character_id: int) -> BreakthroughPanelSnapshot:
        """读取突破秘境主面板所需聚合数据。"""
        return BreakthroughPanelSnapshot(
            overview=self._character_panel_query_service.get_overview(character_id=character_id),
            precheck=self._progression_service.get_breakthrough_precheck(character_id=character_id),
            hub=self._trial_service.get_trial_hub(character_id=character_id),
            recent_settlement=self.get_recent_settlement_snapshot(character_id=character_id),
        )

    def get_recent_settlement_snapshot(self, *, character_id: int) -> BreakthroughRecentSettlementSnapshot | None:
        """读取最近一次突破秘境结算及其展示上下文。"""
        progress_entries = self._breakthrough_repository.list_by_character_id(character_id)
        progress_entry, payload, occurred_at = self._resolve_latest_settlement(progress_entries=progress_entries)
        if progress_entry is None or payload is None or occurred_at is None:
            return None
        trial = self._require_trial(progress_entry.mapping_id)
        battle_report_id = _read_optional_int(payload.get("battle_report_id"))
        settlement = self._build_application_result(progress_entry=progress_entry, payload=payload)
        return BreakthroughRecentSettlementSnapshot(
            mapping_id=trial.mapping_id,
            trial_name=trial.name,
            group_id=trial.group_id,
            group_name=self._group_name_by_id.get(trial.group_id, trial.group_id),
            occurred_at=occurred_at,
            settlement=settlement,
            battle_report_digest=self._load_battle_report_digest(
                character_id=character_id,
                battle_report_id=battle_report_id,
            ),
        )

    def _resolve_latest_settlement(
        self,
        *,
        progress_entries: Sequence[BreakthroughTrialProgress],
    ) -> tuple[BreakthroughTrialProgress | None, dict[str, Any] | None, datetime | None]:
        latest_entry: BreakthroughTrialProgress | None = None
        latest_payload: dict[str, Any] | None = None
        latest_occurred_at: datetime | None = None
        for progress_entry in progress_entries:
            payload = _normalize_mapping(progress_entry.last_result_json)
            if not payload:
                continue
            occurred_at = _parse_datetime(payload.get("occurred_at")) or progress_entry.updated_at
            if latest_occurred_at is None or occurred_at > latest_occurred_at:
                latest_entry = progress_entry
                latest_payload = payload
                latest_occurred_at = occurred_at
        return latest_entry, latest_payload, latest_occurred_at

    def _build_application_result(
        self,
        *,
        progress_entry: BreakthroughTrialProgress,
        payload: Mapping[str, Any],
    ) -> BreakthroughRewardApplicationResult:
        reward_payload = _normalize_mapping(payload.get("reward_payload"))
        soft_limit_snapshot = _normalize_optional_mapping(payload.get("soft_limit_snapshot"))
        if soft_limit_snapshot is None:
            soft_limit_snapshot = _normalize_optional_mapping(reward_payload.get("soft_limit"))
        return BreakthroughRewardApplicationResult(
            settlement_type=str(payload.get("settlement_type") or "unknown"),
            victory=str(payload.get("result") or "").lower() == "victory",
            qualification_granted=bool(payload.get("qualification_granted")),
            progress_status=_read_optional_str(payload.get("resulting_progress_status")) or progress_entry.status,
            attempt_count=max(0, progress_entry.attempt_count),
            cleared_count=max(0, progress_entry.cleared_count),
            reward_payload=reward_payload,
            settlement_payload=dict(payload),
            soft_limit_snapshot=soft_limit_snapshot,
            currency_changes=_normalize_int_mapping(payload.get("currency_changes")),
            item_changes=tuple(_normalize_item_changes(payload.get("item_changes"))),
            battle_report_id=_read_optional_int(payload.get("battle_report_id")),
            drop_record_id=_read_optional_int(payload.get("drop_record_id")),
            source_ref=str(payload.get("source_ref") or f"breakthrough_trial:{progress_entry.mapping_id}"),
        )

    def _load_battle_report_digest(
        self,
        *,
        character_id: int,
        battle_report_id: int | None,
    ) -> BreakthroughBattleReportDigest | None:
        if battle_report_id is None:
            return None
        for battle_report in self._battle_record_repository.list_battle_reports(character_id):
            if battle_report.id == battle_report_id:
                return self._build_battle_report_digest(battle_report)
        return None

    @staticmethod
    def _build_battle_report_digest(battle_report: BattleReport) -> BreakthroughBattleReportDigest:
        summary_payload = _normalize_mapping(battle_report.summary_json)
        damage_summary = _normalize_int_mapping(summary_payload.get("damage_summary"))
        healing_summary = _normalize_int_mapping(summary_payload.get("healing_summary"))
        key_trigger_counts = _normalize_int_mapping(summary_payload.get("key_trigger_counts"))
        return BreakthroughBattleReportDigest(
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

    def _require_trial(self, mapping_id: str) -> BreakthroughTrialDefinition:
        trial = self._trial_by_mapping_id.get(mapping_id)
        if trial is None:
            raise BreakthroughPanelServiceError(f"未定义的突破映射：{mapping_id}")
        return trial



def _normalize_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    return {}



def _normalize_optional_mapping(value: Any) -> dict[str, Any] | None:
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    return None



def _normalize_int_mapping(value: Any) -> dict[str, int]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): _read_int(item) for key, item in value.items()}



def _normalize_item_changes(value: Any) -> list[dict[str, object]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    normalized: list[dict[str, object]] = []
    for entry in value:
        if isinstance(entry, Mapping):
            normalized.append({str(key): item for key, item in entry.items()})
    return normalized



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



def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


__all__ = [
    "BreakthroughBattleReportDigest",
    "BreakthroughPanelService",
    "BreakthroughPanelServiceError",
    "BreakthroughPanelSnapshot",
    "BreakthroughRecentSettlementSnapshot",
]
