"""无尽副本面板查询适配服务。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from application.character.panel_query_service import CharacterPanelOverview, CharacterPanelQueryService
from application.dungeon import EndlessDungeonService, EndlessRunSettlementResult, EndlessRunStatusSnapshot
from application.naming import ItemNamingBatchService
from infrastructure.db.models import BattleReport
from infrastructure.db.repositories import BattleRecordRepository, StateRepository

_COMPLETED_STATUS = "completed"


@dataclass(frozen=True, slots=True)
class EndlessBattleReportDigest:
    """无尽副本最近战斗的战报摘要。"""

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
class EndlessRecentSettlementSnapshot:
    """无尽副本最近一次可复读的结算快照。"""

    settlement_result: EndlessRunSettlementResult
    selected_start_floor: int | None
    advanced_floor_count: int
    record_floor_before_run: int
    latest_anchor_unlock: dict[str, Any] | None
    latest_node_result: dict[str, Any] | None
    battle_report_digest: EndlessBattleReportDigest | None


@dataclass(frozen=True, slots=True)
class EndlessPanelSnapshot:
    """无尽副本私有面板聚合快照。"""

    overview: CharacterPanelOverview
    run_status: EndlessRunStatusSnapshot
    recent_settlement: EndlessRecentSettlementSnapshot | None


class EndlessPanelQueryServiceError(RuntimeError):
    """无尽副本面板查询服务基础异常。"""


class EndlessPanelQueryService:
    """聚合角色总览、无尽运行态与最近结算，供 Discord 面板读取。"""

    def __init__(
        self,
        *,
        character_panel_query_service: CharacterPanelQueryService,
        endless_dungeon_service: EndlessDungeonService,
        state_repository: StateRepository,
        battle_record_repository: BattleRecordRepository,
        naming_batch_service: ItemNamingBatchService | None = None,
    ) -> None:
        self._character_panel_query_service = character_panel_query_service
        self._endless_dungeon_service = endless_dungeon_service
        self._state_repository = state_repository
        self._battle_record_repository = battle_record_repository
        self._naming_batch_service = naming_batch_service

    def get_panel_snapshot(self, *, character_id: int) -> EndlessPanelSnapshot:
        """读取无尽副本主面板所需聚合数据。"""
        overview = self._character_panel_query_service.get_overview(character_id=character_id)
        run_status = self._endless_dungeon_service.get_current_run_state(character_id=character_id)
        recent_settlement = self.get_recent_settlement_snapshot(character_id=character_id)
        return EndlessPanelSnapshot(
            overview=overview,
            run_status=run_status,
            recent_settlement=recent_settlement,
        )

    def get_recent_settlement_snapshot(self, *, character_id: int) -> EndlessRecentSettlementSnapshot | None:
        """读取最近一次无尽终结结算及其展示上下文。"""
        endless_run_state = self._state_repository.get_endless_run_state(character_id)
        if endless_run_state is None or endless_run_state.status != _COMPLETED_STATUS:
            return None
        settlement_result = self._endless_dungeon_service.get_settlement_result(character_id=character_id)
        if self._naming_batch_service is not None:
            settlement_result = EndlessRunSettlementResult(
                character_id=settlement_result.character_id,
                settlement_type=settlement_result.settlement_type,
                terminated_floor=settlement_result.terminated_floor,
                current_region=settlement_result.current_region,
                stable_rewards=settlement_result.stable_rewards,
                pending_rewards=settlement_result.pending_rewards,
                final_drop_list=self._naming_batch_service.refresh_drop_entries(
                    character_id=character_id,
                    entries=settlement_result.final_drop_list,
                ),
                accounting_completed=settlement_result.accounting_completed,
                can_repeat_read=settlement_result.can_repeat_read,
                settled_at=settlement_result.settled_at,
            )
        run_snapshot_payload = _normalize_optional_mapping(endless_run_state.run_snapshot_json)
        if run_snapshot_payload is None:
            raise EndlessPanelQueryServiceError(f"无尽副本已完成但缺少运行快照：{character_id}")
        latest_node_result = _normalize_optional_mapping(run_snapshot_payload.get("latest_node_result"))
        battle_report_id = None
        if latest_node_result is not None:
            battle_report_id = _read_optional_int(latest_node_result.get("battle_report_id"))
        return EndlessRecentSettlementSnapshot(
            settlement_result=settlement_result,
            selected_start_floor=_read_optional_int(run_snapshot_payload.get("selected_start_floor")),
            advanced_floor_count=_read_int(run_snapshot_payload.get("advanced_floor_count")),
            record_floor_before_run=_read_int(run_snapshot_payload.get("record_floor_before_run")),
            latest_anchor_unlock=_normalize_optional_mapping(run_snapshot_payload.get("latest_anchor_unlock")),
            latest_node_result=latest_node_result,
            battle_report_digest=self._load_battle_report_digest(
                character_id=character_id,
                battle_report_id=battle_report_id,
            ),
        )

    def _load_battle_report_digest(
        self,
        *,
        character_id: int,
        battle_report_id: int | None,
    ) -> EndlessBattleReportDigest | None:
        if battle_report_id is None:
            return None
        for battle_report in self._battle_record_repository.list_battle_reports(character_id):
            if battle_report.id == battle_report_id:
                return self._build_battle_report_digest(battle_report)
        return None

    @staticmethod
    def _build_battle_report_digest(battle_report: BattleReport) -> EndlessBattleReportDigest:
        summary_payload = _normalize_optional_mapping(battle_report.summary_json) or {}
        damage_summary = _normalize_int_mapping(summary_payload.get("damage_summary"))
        healing_summary = _normalize_int_mapping(summary_payload.get("healing_summary"))
        key_trigger_counts = _normalize_int_mapping(summary_payload.get("key_trigger_counts"))
        return EndlessBattleReportDigest(
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


def _normalize_optional_mapping(value: Any) -> dict[str, Any] | None:
    if isinstance(value, Mapping):
        return dict(value)
    return None


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


__all__ = [
    "EndlessBattleReportDigest",
    "EndlessPanelQueryService",
    "EndlessPanelQueryServiceError",
    "EndlessPanelSnapshot",
    "EndlessRecentSettlementSnapshot",
]
