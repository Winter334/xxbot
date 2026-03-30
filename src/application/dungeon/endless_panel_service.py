"""无尽副本面板查询适配服务。"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from application.character.panel_query_service import CharacterPanelOverview, CharacterPanelQueryService
from application.dungeon import (
    EndlessDungeonService,
    EndlessFloorAdvanceResult,
    EndlessRunSettlementResult,
    EndlessRunStatusSnapshot,
)
from application.naming import ItemNamingBatchService
from infrastructure.config.static import StaticGameConfig, get_static_config
from infrastructure.db.models import BattleReport
from infrastructure.db.repositories import BattleRecordRepository, StateRepository

_COMPLETED_STATUS = "completed"
_STATUS_RUNNING = "running"
_STATUS_PENDING_DEFEAT_SETTLEMENT = "pending_defeat_settlement"
_NODE_LABEL_BY_VALUE = {
    "normal": "常规层",
    "elite": "精英层",
    "anchor_boss": "首领层",
}
_BATTLE_OUTCOME_LABEL_BY_VALUE = {
    "ally_victory": "胜利",
    "enemy_victory": "战败",
    "draw": "平局",
}
_STOP_REASON_LABEL_BY_VALUE = {
    "advanced": "完成当前层",
    "decision": "抵达决策点",
    "defeat": "战败停止",
}
_ROUND_STATUS_EVENT_TYPES = frozenset(
    {
        "status_applied",
        "status_replaced",
        "status_stacked",
        "status_refreshed",
        "status_expired",
        "status_kept_existing",
        "turn_skipped_by_control",
    }
)


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
    unit_defeated: int
    action_highlights: tuple[str, ...]
    round_highlights: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EndlessEnemyUnitDigest:
    """无尽副本单体敌人的展示摘要。"""

    unit_name: str
    realm_id: str
    stage_id: str
    max_hp: int
    attack_power: int
    guard_power: int
    speed: int
    behavior_template_id: str


@dataclass(frozen=True, slots=True)
class EndlessFloorPanelSnapshot:
    """单层敌阵、战斗结果与掉落进度的展示快照。"""

    floor: int
    node_type: str
    node_label: str
    region_id: str
    region_name: str
    region_theme: str
    race_id: str
    race_name: str
    race_profile: str
    template_id: str
    template_name: str
    template_profile: str
    enemy_count: int
    realm_name: str
    stage_name: str
    style_tags: tuple[str, ...]
    enemy_units: tuple[EndlessEnemyUnitDigest, ...]
    battle_outcome: str | None
    battle_outcome_label: str | None
    reward_granted: bool | None
    battle_report_id: int | None
    stable_reward_summary: dict[str, int]
    pending_reward_summary: dict[str, int]
    drop_progress_gained: int
    cumulative_drop_progress: int | None
    claimable_drop_count: int | None
    current_hp_ratio: str | None
    current_mp_ratio: str | None
    battle_report_digest: EndlessBattleReportDigest | None


@dataclass(frozen=True, slots=True)
class EndlessRunPresentationSnapshot:
    """无尽副本当前运行态的当前层场景投影。"""

    phase: str
    phase_label: str
    stopped_floor: int | None
    decision_floor: int | None
    next_floor: int | None
    can_continue: bool
    can_settle_retreat: bool
    can_settle_defeat: bool
    battle_count: int
    advanced_floor_count: int
    pending_drop_progress: int
    claimable_drop_count: int
    current_scene_kind: str
    current_scene_floor: EndlessFloorPanelSnapshot | None
    latest_floor_result: EndlessFloorPanelSnapshot | None
    upcoming_floor_preview: EndlessFloorPanelSnapshot | None


@dataclass(frozen=True, slots=True)
class EndlessAdvancePresentation:
    """单层推进后的 UI 投影。"""

    stopped_reason: str
    stopped_reason_label: str
    stopped_floor: int
    decision_floor: int | None
    next_floor: int | None
    can_settle_retreat: bool
    pending_drop_progress: int
    claimable_drop_count: int
    floor_result: EndlessFloorPanelSnapshot
    upcoming_floor_preview: EndlessFloorPanelSnapshot | None


@dataclass(frozen=True, slots=True)
class EndlessRecentSettlementSnapshot:
    """无尽副本最近一次可复读的结算快照。"""

    settlement_result: EndlessRunSettlementResult
    selected_start_floor: int | None
    advanced_floor_count: int
    record_floor_before_run: int
    last_floor_result: EndlessFloorPanelSnapshot | None


@dataclass(frozen=True, slots=True)
class EndlessPanelSnapshot:
    """无尽副本私有面板聚合快照。"""

    overview: CharacterPanelOverview
    run_status: EndlessRunStatusSnapshot
    run_presentation: EndlessRunPresentationSnapshot
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
        static_config: StaticGameConfig | None = None,
    ) -> None:
        self._character_panel_query_service = character_panel_query_service
        self._endless_dungeon_service = endless_dungeon_service
        self._state_repository = state_repository
        self._battle_record_repository = battle_record_repository
        self._naming_batch_service = naming_batch_service
        self._static_config = static_config or getattr(endless_dungeon_service, "_static_config", None) or get_static_config()
        self._enemy_templates_by_id = {
            entry.template_id: entry
            for entry in self._static_config.enemies.templates
        }
        self._enemy_races_by_id = {
            entry.race_id: entry
            for entry in self._static_config.enemies.races
        }
        self._regions_by_id = {
            entry.region_id: entry
            for entry in self._static_config.endless_dungeon.regions
        }

    def get_panel_snapshot(self, *, character_id: int) -> EndlessPanelSnapshot:
        """读取无尽副本主面板所需聚合数据。"""
        overview = self._character_panel_query_service.get_overview(character_id=character_id)
        run_status = self._endless_dungeon_service.get_current_run_state(character_id=character_id)
        run_presentation = self._build_run_presentation(
            character_id=character_id,
            overview=overview,
            run_status=run_status,
        )
        recent_settlement = self.get_recent_settlement_snapshot(character_id=character_id, overview=overview)
        return EndlessPanelSnapshot(
            overview=overview,
            run_status=run_status,
            run_presentation=run_presentation,
            recent_settlement=recent_settlement,
        )

    def build_advance_presentation(
        self,
        *,
        character_id: int,
        result: EndlessFloorAdvanceResult,
        overview: CharacterPanelOverview | None = None,
    ) -> EndlessAdvancePresentation:
        """把单层推进结果转换为 UI 可直接消费的展示数据。"""
        resolved_overview = overview or self._character_panel_query_service.get_overview(character_id=character_id)
        battle_reports_by_id = self._load_battle_reports_by_id(
            character_id=character_id,
            battle_report_ids=_collect_battle_report_ids((result.latest_node_result,)),
        )
        reward_ledger = result.run_status.reward_ledger
        final_pending_drop_progress = 0 if reward_ledger is None else reward_ledger.pending_drop_progress
        final_claimable_drop_count = 0 if reward_ledger is None else reward_ledger.drop_count
        floor_result = self._build_floor_panel_snapshot(
            node_result=result.latest_node_result,
            overview=resolved_overview,
            battle_reports_by_id=battle_reports_by_id,
            cumulative_drop_progress=final_pending_drop_progress,
            claimable_drop_count=final_claimable_drop_count,
        )
        upcoming_floor_preview = self._build_upcoming_floor_preview(
            character_id=character_id,
            overview=resolved_overview,
            run_status=result.run_status,
            current_pending_drop_progress=final_pending_drop_progress,
            current_claimable_drop_count=final_claimable_drop_count,
        )
        return EndlessAdvancePresentation(
            stopped_reason=result.stopped_reason,
            stopped_reason_label=_STOP_REASON_LABEL_BY_VALUE.get(result.stopped_reason, result.stopped_reason),
            stopped_floor=result.stopped_floor,
            decision_floor=result.decision_floor,
            next_floor=result.next_floor,
            can_settle_retreat=result.stopped_reason == "decision" and result.decision_floor is not None,
            pending_drop_progress=final_pending_drop_progress,
            claimable_drop_count=final_claimable_drop_count,
            floor_result=floor_result,
            upcoming_floor_preview=upcoming_floor_preview,
        )

    def get_recent_settlement_snapshot(
        self,
        *,
        character_id: int,
        overview: CharacterPanelOverview | None = None,
    ) -> EndlessRecentSettlementSnapshot | None:
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
        resolved_overview = overview or self._character_panel_query_service.get_overview(character_id=character_id)
        latest_node_result = _normalize_optional_mapping(run_snapshot_payload.get("latest_node_result"))
        last_floor_result = None
        if latest_node_result is not None:
            battle_reports_by_id = self._load_battle_reports_by_id(
                character_id=character_id,
                battle_report_ids=_collect_battle_report_ids((latest_node_result,)),
            )
            final_drop_progress = _read_int(settlement_result.pending_rewards.original.get("drop_progress"))
            last_floor_result = self._build_floor_panel_snapshot(
                node_result=latest_node_result,
                overview=resolved_overview,
                battle_reports_by_id=battle_reports_by_id,
                cumulative_drop_progress=final_drop_progress,
                claimable_drop_count=final_drop_progress // 10,
            )
        return EndlessRecentSettlementSnapshot(
            settlement_result=settlement_result,
            selected_start_floor=_read_optional_int(run_snapshot_payload.get("selected_start_floor")),
            advanced_floor_count=_read_int(run_snapshot_payload.get("advanced_floor_count")),
            record_floor_before_run=_read_int(run_snapshot_payload.get("record_floor_before_run")),
            last_floor_result=last_floor_result,
        )

    def _build_run_presentation(
        self,
        *,
        character_id: int,
        overview: CharacterPanelOverview,
        run_status: EndlessRunStatusSnapshot,
    ) -> EndlessRunPresentationSnapshot:
        if not run_status.has_active_run:
            return EndlessRunPresentationSnapshot(
                phase="idle",
                phase_label="未运行",
                stopped_floor=None,
                decision_floor=None,
                next_floor=None,
                can_continue=False,
                can_settle_retreat=False,
                can_settle_defeat=False,
                battle_count=0,
                advanced_floor_count=0,
                pending_drop_progress=0,
                claimable_drop_count=0,
                current_scene_kind="idle",
                current_scene_floor=None,
                latest_floor_result=None,
                upcoming_floor_preview=None,
            )
        reward_ledger = run_status.reward_ledger
        latest_node_result = None if reward_ledger is None else _normalize_optional_mapping(reward_ledger.latest_node_result)
        battle_reports_by_id = self._load_battle_reports_by_id(
            character_id=character_id,
            battle_report_ids=() if latest_node_result is None else _collect_battle_report_ids((latest_node_result,)),
        )
        latest_floor_result = None
        if latest_node_result is not None:
            latest_floor_result = self._build_floor_panel_snapshot(
                node_result=latest_node_result,
                overview=overview,
                battle_reports_by_id=battle_reports_by_id,
                cumulative_drop_progress=0 if reward_ledger is None else reward_ledger.pending_drop_progress,
                claimable_drop_count=0 if reward_ledger is None else reward_ledger.drop_count,
            )
        upcoming_floor_preview = self._build_upcoming_floor_preview(
            character_id=character_id,
            overview=overview,
            run_status=run_status,
            current_pending_drop_progress=0 if reward_ledger is None else reward_ledger.pending_drop_progress,
            current_claimable_drop_count=0 if reward_ledger is None else reward_ledger.drop_count,
        )
        decision_floor = self._resolve_decision_floor(run_status=run_status)
        if run_status.status == _STATUS_PENDING_DEFEAT_SETTLEMENT:
            phase = "pending_defeat_settlement"
            phase_label = f"第 {run_status.current_floor} 层战败待结算"
            stopped_floor = run_status.current_floor
            next_floor = None
            can_continue = False
            can_settle_retreat = False
            can_settle_defeat = True
            current_scene_kind = "defeat"
            current_scene_floor = latest_floor_result
        elif decision_floor is not None:
            phase = "decision"
            phase_label = f"第 {decision_floor} 层节点抉择"
            stopped_floor = decision_floor
            next_floor = run_status.current_floor
            can_continue = True
            can_settle_retreat = True
            can_settle_defeat = False
            current_scene_kind = "decision"
            current_scene_floor = latest_floor_result
        else:
            phase = "running"
            stopped_floor = None if latest_floor_result is None else latest_floor_result.floor
            next_floor = run_status.current_floor
            can_continue = True
            can_settle_retreat = False
            can_settle_defeat = False
            if latest_floor_result is not None:
                phase_label = f"第 {latest_floor_result.floor} 层已击破"
                current_scene_kind = "floor_result"
                current_scene_floor = latest_floor_result
            else:
                phase_label = (
                    "待开始挑战"
                    if run_status.current_floor is None
                    else f"第 {run_status.current_floor} 层待开战"
                )
                current_scene_kind = "upcoming_preview"
                current_scene_floor = upcoming_floor_preview
        return EndlessRunPresentationSnapshot(
            phase=phase,
            phase_label=phase_label,
            stopped_floor=stopped_floor,
            decision_floor=decision_floor,
            next_floor=next_floor,
            can_continue=can_continue,
            can_settle_retreat=can_settle_retreat,
            can_settle_defeat=can_settle_defeat,
            battle_count=len(run_status.encounter_history),
            advanced_floor_count=0 if reward_ledger is None else reward_ledger.advanced_floor_count,
            pending_drop_progress=0 if reward_ledger is None else reward_ledger.pending_drop_progress,
            claimable_drop_count=0 if reward_ledger is None else reward_ledger.drop_count,
            current_scene_kind=current_scene_kind,
            current_scene_floor=current_scene_floor,
            latest_floor_result=latest_floor_result,
            upcoming_floor_preview=upcoming_floor_preview,
        )

    def _build_upcoming_floor_preview(
        self,
        *,
        character_id: int,
        overview: CharacterPanelOverview,
        run_status: EndlessRunStatusSnapshot,
        current_pending_drop_progress: int,
        current_claimable_drop_count: int,
    ) -> EndlessFloorPanelSnapshot | None:
        if run_status.status != _STATUS_RUNNING:
            return None
        if run_status.current_floor is None or run_status.run_seed is None:
            return None
        character_repository = getattr(self._endless_dungeon_service, "_character_repository", None)
        encounter_generator = getattr(self._endless_dungeon_service, "_encounter_generator", None)
        if character_repository is None or encounter_generator is None:
            return None
        aggregate = character_repository.get_aggregate(character_id)
        if aggregate is None or aggregate.progress is None:
            raise EndlessPanelQueryServiceError(f"角色缺少成长状态：{character_id}")
        encounter = encounter_generator.generate(
            floor=run_status.current_floor,
            seed=run_status.run_seed,
        )
        enemy_units = self._endless_dungeon_service._build_enemy_battle_snapshots(  # type: ignore[attr-defined]
            progress=aggregate.progress,
            encounter=encounter,
        )
        payload = {
            "floor": encounter.floor,
            "node_type": encounter.node_type.value,
            "region_id": encounter.region_id,
            "region_bias_id": encounter.region_bias_id,
            "enemy_race_id": encounter.race_id,
            "enemy_template_id": encounter.template_id,
            "enemy_count": encounter.enemy_count,
            "encounter": {
                "floor": encounter.floor,
                "region_id": encounter.region_id,
                "region_bias_id": encounter.region_bias_id,
                "node_type": encounter.node_type.value,
                "race_id": encounter.race_id,
                "template_id": encounter.template_id,
                "enemy_count": encounter.enemy_count,
                "seed": encounter.seed,
            },
            "enemy_units": [
                {
                    "unit_id": enemy.unit_id,
                    "unit_name": enemy.unit_name,
                    "realm_id": enemy.realm_id,
                    "stage_id": enemy.stage_id,
                    "max_hp": enemy.max_hp,
                    "attack_power": enemy.attack_power,
                    "guard_power": enemy.guard_power,
                    "speed": enemy.speed,
                    "behavior_template_id": enemy.behavior_template_id,
                }
                for enemy in enemy_units
            ],
            "battle_outcome": None,
            "battle_report_id": None,
            "reward_granted": None,
            "reward_payload": None,
            "current_hp_ratio": None,
            "current_mp_ratio": None,
        }
        return self._build_floor_panel_snapshot(
            node_result=payload,
            overview=overview,
            battle_reports_by_id={},
            cumulative_drop_progress=current_pending_drop_progress,
            claimable_drop_count=current_claimable_drop_count,
        )

    def _build_floor_panel_snapshot(
        self,
        *,
        node_result: Mapping[str, Any],
        overview: CharacterPanelOverview,
        battle_reports_by_id: Mapping[int, BattleReport],
        cumulative_drop_progress: int | None,
        claimable_drop_count: int | None,
    ) -> EndlessFloorPanelSnapshot:
        floor = _read_int(node_result.get("floor"))
        node_type = str(node_result.get("node_type") or "")
        region_id = str(node_result.get("region_id") or "")
        race_id = str(node_result.get("enemy_race_id") or "")
        template_id = str(node_result.get("enemy_template_id") or "")
        reward_payload = _normalize_optional_mapping(node_result.get("reward_payload")) or {}
        stable_reward_summary = _normalize_int_mapping(reward_payload.get("stable"))
        pending_reward_summary = _normalize_int_mapping(reward_payload.get("pending"))
        battle_report_id = _read_optional_int(node_result.get("battle_report_id"))
        region_entry = self._regions_by_id.get(region_id)
        race_entry = self._enemy_races_by_id.get(race_id)
        template_entry = self._enemy_templates_by_id.get(template_id)
        battle_report = None if battle_report_id is None else battle_reports_by_id.get(battle_report_id)
        enemy_units_payload = _normalize_mapping_list(node_result.get("enemy_units"))
        enemy_units = tuple(
            EndlessEnemyUnitDigest(
                unit_name=str(item.get("unit_name") or f"敌人 {index}"),
                realm_id=str(item.get("realm_id") or ""),
                stage_id=str(item.get("stage_id") or ""),
                max_hp=_read_int(item.get("max_hp")),
                attack_power=_read_int(item.get("attack_power")),
                guard_power=_read_int(item.get("guard_power")),
                speed=_read_int(item.get("speed")),
                behavior_template_id=str(item.get("behavior_template_id") or ""),
            )
            for index, item in enumerate(enemy_units_payload, start=1)
        )
        style_tags = tuple(
            item
            for item in (
                _NODE_LABEL_BY_VALUE.get(node_type, node_type or "未知层型"),
                None if race_entry is None else race_entry.name,
                None if template_entry is None else template_entry.name,
            )
            if item
        )
        battle_outcome = None if node_result.get("battle_outcome") is None else str(node_result.get("battle_outcome"))
        battle_outcome_label = None if battle_outcome is None else _BATTLE_OUTCOME_LABEL_BY_VALUE.get(battle_outcome, battle_outcome)
        return EndlessFloorPanelSnapshot(
            floor=floor,
            node_type=node_type,
            node_label=_NODE_LABEL_BY_VALUE.get(node_type, node_type or "未知层型"),
            region_id=region_id,
            region_name=region_entry.name if region_entry is not None else (region_id or "未知区域"),
            region_theme=region_entry.theme_summary if region_entry is not None else "-",
            race_id=race_id,
            race_name=race_entry.name if race_entry is not None else (race_id or "未知敌类"),
            race_profile=race_entry.combat_profile if race_entry is not None else "-",
            template_id=template_id,
            template_name=template_entry.name if template_entry is not None else (template_id or "未知模板"),
            template_profile=template_entry.combat_profile if template_entry is not None else "-",
            enemy_count=_read_int(node_result.get("enemy_count")),
            realm_name=overview.realm_name,
            stage_name=overview.stage_name,
            style_tags=style_tags,
            enemy_units=enemy_units,
            battle_outcome=battle_outcome,
            battle_outcome_label=battle_outcome_label,
            reward_granted=None if node_result.get("reward_granted") is None else bool(node_result.get("reward_granted")),
            battle_report_id=battle_report_id,
            stable_reward_summary=stable_reward_summary,
            pending_reward_summary=pending_reward_summary,
            drop_progress_gained=_read_int(pending_reward_summary.get("drop_progress")),
            cumulative_drop_progress=cumulative_drop_progress,
            claimable_drop_count=claimable_drop_count,
            current_hp_ratio=_read_optional_str(node_result.get("current_hp_ratio")),
            current_mp_ratio=_read_optional_str(node_result.get("current_mp_ratio")),
            battle_report_digest=None if battle_report is None else self._build_battle_report_digest(battle_report),
        )

    def _load_battle_reports_by_id(
        self,
        *,
        character_id: int,
        battle_report_ids: Sequence[int],
    ) -> dict[int, BattleReport]:
        required_ids = {report_id for report_id in battle_report_ids if report_id > 0}
        if not required_ids:
            return {}
        reports = self._battle_record_repository.list_battle_reports(character_id)
        return {
            report.id: report
            for report in reports
            if report.id in required_ids
        }

    def _build_battle_report_digest(self, battle_report: BattleReport) -> EndlessBattleReportDigest:
        summary_payload = _normalize_optional_mapping(battle_report.summary_json) or {}
        detail_payload = _normalize_optional_mapping(battle_report.detail_log_json) or {}
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
            unit_defeated=_read_int(key_trigger_counts.get("unit_defeated")),
            action_highlights=self._extract_action_highlights(
                summary_payload=summary_payload,
                detail_payload=detail_payload,
            ),
            round_highlights=self._extract_round_highlights(detail_payload=detail_payload),
        )

    def _extract_action_highlights(
        self,
        *,
        summary_payload: Mapping[str, Any],
        detail_payload: Mapping[str, Any],
    ) -> tuple[str, ...]:
        focus_unit_name = str(summary_payload.get("focus_unit_name") or "")
        action_label_by_id = self._build_action_label_mapping(detail_payload=detail_payload)
        main_paths = _normalize_mapping_list(summary_payload.get("main_paths"))
        if not main_paths:
            return ()
        focus_path = None
        for item in main_paths:
            if str(item.get("unit_name") or "") == focus_unit_name:
                focus_path = item
                break
        if focus_path is None:
            focus_path = main_paths[0]
        actions_used = _normalize_mapping_list(focus_path.get("actions_used"))
        highlights: list[str] = []
        for item in actions_used[:3]:
            action_id = str(item.get("action_id") or "")
            count = _read_int(item.get("count"))
            if not action_id or count <= 0:
                continue
            highlights.append(f"{action_label_by_id.get(action_id, action_id)}×{count}")
        return tuple(highlights)

    def _extract_round_highlights(self, *, detail_payload: Mapping[str, Any]) -> tuple[str, ...]:
        event_sequence = _normalize_mapping_list(detail_payload.get("event_sequence"))
        if not event_sequence:
            return ()
        unit_name_by_id = self._build_unit_name_mapping(detail_payload=detail_payload)
        action_label_by_id = self._build_action_label_mapping(detail_payload=detail_payload)
        events_by_round: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for event in event_sequence:
            round_index = _read_int(event.get("round_index"))
            if round_index <= 0:
                continue
            events_by_round[round_index].append(event)
        lines: list[str] = []
        for round_index in sorted(events_by_round):
            round_events = sorted(events_by_round[round_index], key=lambda item: _read_int(item.get("sequence")))
            selected_actions: list[str] = []
            for event in round_events:
                event_type = str(event.get("event_type") or "")
                if event_type not in {"action_selected", "action_started"}:
                    continue
                action_id = str(event.get("action_id") or "")
                actor_id = str(event.get("actor_unit_id") or "")
                actor_name = unit_name_by_id.get(actor_id, actor_id or "未知单位")
                action_label = action_label_by_id.get(action_id, action_id or "普通攻击")
                selected_actions.append(f"{actor_name}·{action_label}")
            critical_hits = sum(
                1
                for event in round_events
                if str(event.get("event_type") or "") == "crit_check"
                and bool((_normalize_optional_mapping(event.get("detail")) or {}).get("success"))
            )
            control_skips = sum(
                1
                for event in round_events
                if str(event.get("event_type") or "") == "turn_skipped_by_control"
            )
            status_changes = sum(
                1
                for event in round_events
                if str(event.get("event_type") or "") in _ROUND_STATUS_EVENT_TYPES
            )
            defeated_targets = _deduplicate_preserve_order(
                [
                    unit_name_by_id.get(str(event.get("target_unit_id") or ""), str(event.get("target_unit_id") or "未知目标"))
                    for event in round_events
                    if str(event.get("event_type") or "") == "unit_defeated"
                ]
            )
            parts: list[str] = []
            if selected_actions:
                parts.append("出手 " + "、".join(selected_actions[:2]))
            if critical_hits > 0:
                parts.append(f"暴击 {critical_hits}")
            if control_skips > 0:
                parts.append(f"控场 {control_skips}")
            if defeated_targets:
                parts.append("击破 " + "、".join(defeated_targets[:2]))
            if status_changes > 0:
                parts.append(f"状态变化 {status_changes}")
            if not parts:
                continue
            lines.append(f"第 {round_index} 回合：" + "｜".join(parts))
            if len(lines) >= 3:
                break
        return tuple(lines)

    @staticmethod
    def _build_unit_name_mapping(*, detail_payload: Mapping[str, Any]) -> dict[str, str]:
        mapping: dict[str, str] = {}
        input_snapshot_summary = _normalize_optional_mapping(detail_payload.get("input_snapshot_summary")) or {}
        for group_name in ("allies", "enemies"):
            for item in _normalize_mapping_list(input_snapshot_summary.get(group_name)):
                unit_id = str(item.get("unit_id") or "")
                unit_name = str(item.get("unit_name") or unit_id)
                if unit_id:
                    mapping[unit_id] = unit_name
        terminal_statistics = _normalize_optional_mapping(detail_payload.get("terminal_statistics")) or {}
        for item in _normalize_mapping_list(terminal_statistics.get("final_units")):
            unit_id = str(item.get("unit_id") or "")
            unit_name = str(item.get("unit_name") or unit_id)
            if unit_id:
                mapping[unit_id] = unit_name
        return mapping

    @staticmethod
    def _build_action_label_mapping(*, detail_payload: Mapping[str, Any]) -> dict[str, str]:
        terminal_statistics = _normalize_optional_mapping(detail_payload.get("terminal_statistics")) or {}
        label_by_id: dict[str, str] = {}
        for template in _normalize_mapping_list(terminal_statistics.get("behavior_templates")):
            for action in _normalize_mapping_list(template.get("actions")):
                action_id = str(action.get("action_id") or "")
                if not action_id:
                    continue
                labels = action.get("labels")
                if isinstance(labels, list | tuple):
                    for item in labels:
                        if isinstance(item, str) and item.strip():
                            label_by_id[action_id] = item.strip()
                            break
                if action_id not in label_by_id:
                    label_by_id[action_id] = action_id
        return label_by_id

    @staticmethod
    def _resolve_decision_floor(*, run_status: EndlessRunStatusSnapshot) -> int | None:
        reward_ledger = run_status.reward_ledger
        if reward_ledger is None:
            return None
        last_reward_floor = reward_ledger.last_reward_floor
        if last_reward_floor is None or not EndlessPanelQueryService._is_decision_floor(last_reward_floor):
            return None
        latest_node_result = reward_ledger.latest_node_result
        if latest_node_result is not None and not bool(latest_node_result.get("reward_granted")):
            return None
        if run_status.current_floor != last_reward_floor + 1:
            return None
        return last_reward_floor

    @staticmethod
    def _is_decision_floor(floor: int) -> bool:
        normalized_floor = max(1, floor)
        return normalized_floor % 10 in (5, 0)


def _collect_battle_report_ids(items: Sequence[Mapping[str, Any]]) -> tuple[int, ...]:
    report_ids: list[int] = []
    for item in items:
        report_id = _read_optional_int(item.get("battle_report_id"))
        if report_id is not None and report_id > 0:
            report_ids.append(report_id)
    return tuple(report_ids)


def _normalize_optional_mapping(value: Any) -> dict[str, Any] | None:
    if isinstance(value, Mapping):
        return dict(value)
    return None


def _normalize_mapping_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list | tuple):
        return []
    normalized_items: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, Mapping):
            normalized_items.append(dict(item))
    return normalized_items


def _normalize_int_mapping(value: Any) -> dict[str, int]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): _read_int(item) for key, item in value.items()}


def _deduplicate_preserve_order(items: Sequence[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    ordered_items: list[str] = []
    for item in items:
        normalized_item = item.strip()
        if not normalized_item or normalized_item in seen:
            continue
        seen.add(normalized_item)
        ordered_items.append(normalized_item)
    return tuple(ordered_items)


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
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return None if not stripped else stripped
    return str(value)


__all__ = [
    "EndlessAdvancePresentation",
    "EndlessBattleReportDigest",
    "EndlessEnemyUnitDigest",
    "EndlessFloorPanelSnapshot",
    "EndlessPanelQueryService",
    "EndlessPanelQueryServiceError",
    "EndlessPanelSnapshot",
    "EndlessRecentSettlementSnapshot",
    "EndlessRunPresentationSnapshot",
]
