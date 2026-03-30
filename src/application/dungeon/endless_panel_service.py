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
_STABLE_REWARD_NAME_BY_KEY = {
    "cultivation": "修为",
    "insight": "感悟",
    "refining_essence": "炼华精粹",
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
    narration_lines: tuple[str, ...]


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
    current_hp: int = 0
    is_alive: bool = True


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
    enemy_summary_lines: tuple[str, ...]
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
    enemy_health_line: str | None = None
    enemy_scene_lines: tuple[str, ...] = ()
    battle_scene_lines: tuple[str, ...] = ()
    reward_scene_lines: tuple[str, ...] = ()


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
        enemy_units = self._build_enemy_unit_digests(
            enemy_units_payload=enemy_units_payload,
            battle_report=battle_report,
            race_name=race_entry.name if race_entry is not None else (race_id or ""),
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
        reward_granted = None if node_result.get("reward_granted") is None else bool(node_result.get("reward_granted"))
        battle_report_digest = None if battle_report is None else self._build_battle_report_digest(battle_report)
        drop_progress_gained = _read_int(pending_reward_summary.get("drop_progress"))
        enemy_health_line = self._build_enemy_health_line(enemy_units=enemy_units)
        enemy_summary_lines = self._build_enemy_summary_lines(
            floor=floor,
            node_type=node_type,
            region_name=region_entry.name if region_entry is not None else (region_id or "未知区域"),
            region_theme=region_entry.theme_summary if region_entry is not None else "-",
            race_name=race_entry.name if race_entry is not None else (race_id or "未知敌类"),
            template_name=template_entry.name if template_entry is not None else (template_id or "未知模板"),
            enemy_count=_read_int(node_result.get("enemy_count")),
            enemy_units=enemy_units,
            enemy_health_line=enemy_health_line,
        )
        battle_scene_lines = self._build_battle_scene_lines(
            battle_outcome=battle_outcome,
            battle_report_digest=battle_report_digest,
        )
        reward_scene_lines = self._build_reward_scene_lines(
            floor=floor,
            battle_outcome=battle_outcome,
            reward_granted=reward_granted,
            stable_reward_summary=stable_reward_summary,
            drop_progress_gained=drop_progress_gained,
            cumulative_drop_progress=cumulative_drop_progress,
            claimable_drop_count=claimable_drop_count,
        )
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
            enemy_summary_lines=enemy_summary_lines,
            battle_outcome=battle_outcome,
            battle_outcome_label=battle_outcome_label,
            reward_granted=reward_granted,
            battle_report_id=battle_report_id,
            stable_reward_summary=stable_reward_summary,
            pending_reward_summary=pending_reward_summary,
            drop_progress_gained=drop_progress_gained,
            cumulative_drop_progress=cumulative_drop_progress,
            claimable_drop_count=claimable_drop_count,
            current_hp_ratio=_read_optional_str(node_result.get("current_hp_ratio")),
            current_mp_ratio=_read_optional_str(node_result.get("current_mp_ratio")),
            battle_report_digest=battle_report_digest,
            enemy_health_line=enemy_health_line,
            enemy_scene_lines=enemy_summary_lines,
            battle_scene_lines=battle_scene_lines,
            reward_scene_lines=reward_scene_lines,
        )

    def _build_enemy_summary_lines(
        self,
        *,
        floor: int,
        node_type: str,
        region_name: str,
        region_theme: str,
        race_name: str,
        template_name: str,
        enemy_count: int,
        enemy_units: Sequence[EndlessEnemyUnitDigest],
        enemy_health_line: str | None,
    ) -> tuple[str, ...]:
        enemy_title = "·".join(
            item
            for item in (race_name.strip(), template_name.strip())
            if item and item not in {"未知敌类", "未知模板"} and not _looks_like_internal_identifier(item)
        )
        if not enemy_title:
            enemy_title = race_name.strip() if race_name.strip() and race_name.strip() != "未知敌类" else "异样妖影"
        count_text = f"{max(1, enemy_count)} 名" if enemy_count > 1 else "1 名"
        lines = [f"第 {floor} 层有 {count_text}{enemy_title}拦路。"]
        if enemy_health_line is not None:
            lines.append(enemy_health_line)
        pressure_line = self._build_enemy_pressure_line(node_type=node_type, enemy_units=enemy_units)
        if pressure_line is not None:
            lines.append(pressure_line)
        region_line = self._build_region_pressure_line(region_name=region_name, region_theme=region_theme)
        if region_line is not None and len(lines) < 3:
            lines.append(region_line)
        return tuple(lines[:3])

    @staticmethod
    def _build_enemy_pressure_line(
        *,
        node_type: str,
        enemy_units: Sequence[EndlessEnemyUnitDigest],
    ) -> str | None:
        if enemy_units:
            attack_average = sum(unit.attack_power for unit in enemy_units) / len(enemy_units)
            guard_average = sum(unit.guard_power for unit in enemy_units) / len(enemy_units)
            speed_average = sum(unit.speed for unit in enemy_units) / len(enemy_units)
            if speed_average >= attack_average and speed_average >= guard_average:
                detail = "它们身法极快，稍慢一步就会被追着打。"
            elif attack_average >= guard_average:
                detail = "它们杀势很重，正面硬吃几下就会非常难受。"
            else:
                detail = "它们护体很稳，想一口气斩穿并不容易。"
        else:
            detail = "妖气翻得很乱，一时还摸不清深浅。"
        if node_type == "anchor_boss":
            return f"首领妖气压得很沉，{detail}"
        if node_type == "elite":
            return f"这一层比寻常更凶，{detail}"
        return f"这些妖影来势不轻，{detail}"

    @staticmethod
    def _build_region_pressure_line(*, region_name: str, region_theme: str) -> str | None:
        normalized_theme = region_theme.strip().rstrip("。")
        if not normalized_theme or normalized_theme == "-":
            return None
        return f"这段{region_name}气机紊乱，{normalized_theme}。"

    def _build_enemy_unit_digests(
        self,
        *,
        enemy_units_payload: Sequence[Mapping[str, Any]],
        battle_report: BattleReport | None,
        race_name: str,
    ) -> tuple[EndlessEnemyUnitDigest, ...]:
        final_enemy_states = self._extract_final_enemy_unit_states(battle_report=battle_report)
        enemy_count = len(enemy_units_payload)
        digests: list[EndlessEnemyUnitDigest] = []
        for index, item in enumerate(enemy_units_payload, start=1):
            unit_id = str(item.get("unit_id") or "")
            max_hp = _read_int(item.get("max_hp"))
            final_state = final_enemy_states.get(unit_id)
            current_hp = max_hp if final_state is None else max(0, _read_int(final_state.get("current_hp"), default=max_hp))
            is_alive = True if final_state is None else bool(final_state.get("is_alive", current_hp > 0))
            digests.append(
                EndlessEnemyUnitDigest(
                    unit_name=self._build_enemy_display_name(
                        raw_name=str(item.get("unit_name") or ""),
                        unit_id=unit_id,
                        index=index,
                        total_count=enemy_count,
                        race_name=race_name,
                    ),
                    realm_id=str(item.get("realm_id") or ""),
                    stage_id=str(item.get("stage_id") or ""),
                    max_hp=max_hp,
                    attack_power=_read_int(item.get("attack_power")),
                    guard_power=_read_int(item.get("guard_power")),
                    speed=_read_int(item.get("speed")),
                    behavior_template_id=str(item.get("behavior_template_id") or ""),
                    current_hp=current_hp,
                    is_alive=is_alive and current_hp > 0,
                )
            )
        return tuple(digests)

    @staticmethod
    def _extract_final_enemy_unit_states(*, battle_report: BattleReport | None) -> dict[str, dict[str, Any]]:
        if battle_report is None:
            return {}
        detail_payload = _normalize_optional_mapping(battle_report.detail_log_json) or {}
        terminal_statistics = _normalize_optional_mapping(detail_payload.get("terminal_statistics")) or {}
        final_units = _normalize_mapping_list(terminal_statistics.get("final_units"))
        return {
            str(item.get("unit_id") or ""): item
            for item in final_units
            if str(item.get("side") or "") == "enemy" and str(item.get("unit_id") or "")
        }

    @staticmethod
    def _build_enemy_health_line(*, enemy_units: Sequence[EndlessEnemyUnitDigest]) -> str | None:
        if not enemy_units:
            return None
        parts = [
            f"{unit.unit_name} {EndlessPanelQueryService._format_enemy_health_state(unit=unit)}"
            for unit in enemy_units[:3]
        ]
        if len(enemy_units) > 3:
            alive_count = sum(1 for unit in enemy_units if unit.is_alive and unit.current_hp > 0)
            parts.append(f"其余 {len(enemy_units) - 3} 名仍在缠斗 {alive_count}/{len(enemy_units)}")
        return "｜".join(parts)

    def _build_enemy_display_name(
        self,
        *,
        raw_name: str,
        unit_id: str,
        index: int,
        total_count: int,
        race_name: str,
    ) -> str:
        del unit_id
        noun = self._resolve_enemy_name_noun(raw_name=raw_name, race_name=race_name)
        prefix = self._build_positional_enemy_prefix(index=index, total_count=total_count)
        return f"{prefix}{noun}" if prefix else noun

    @staticmethod
    def _resolve_enemy_name_noun(*, raw_name: str, race_name: str) -> str:
        normalized_raw_name = raw_name.strip()
        numbered_stem = _extract_counter_stem(normalized_raw_name)
        if numbered_stem is not None:
            return numbered_stem
        if normalized_raw_name and not _looks_like_internal_identifier(normalized_raw_name):
            return normalized_raw_name
        normalized_race_name = race_name.strip()
        if normalized_race_name and normalized_race_name not in {"未知敌类", "-"}:
            return normalized_race_name
        return "妖影"

    @staticmethod
    def _build_positional_enemy_prefix(*, index: int, total_count: int) -> str:
        if total_count <= 1:
            return "守关"
        if index == 1:
            return "前排"
        if index == 2:
            return "后侧"
        if index == 3:
            return "尾阵"
        return "余势"

    @staticmethod
    def _format_enemy_health_state(*, unit: EndlessEnemyUnitDigest) -> str:
        if not unit.is_alive or unit.current_hp <= 0:
            return "已击破"
        if unit.max_hp <= 0:
            return f"{unit.current_hp}"
        hp_ratio = unit.current_hp / unit.max_hp
        if hp_ratio <= 0.15:
            return f"濒危 {unit.current_hp}/{unit.max_hp}"
        if hp_ratio <= 0.4:
            return f"受创 {unit.current_hp}/{unit.max_hp}"
        return f"{unit.current_hp}/{unit.max_hp}"

    def _build_battle_scene_lines(
        self,
        *,
        battle_outcome: str | None,
        battle_report_digest: EndlessBattleReportDigest | None,
    ) -> tuple[str, ...]:
        if battle_report_digest is not None and battle_report_digest.narration_lines:
            return tuple(
                line.strip()
                for line in battle_report_digest.narration_lines
                if isinstance(line, str) and line.strip()
            )[:3]
        if battle_outcome == "ally_victory":
            return (
                "这一层厮杀结束得极快，你只记得剑光和妖气一起炸开。",
                "最后还是你把这一层硬生生压了过去。",
            )
        if battle_outcome == "enemy_victory":
            return (
                "这一层反扑太狠，你转眼就被逼退了下来。",
                "妖气还压在胸口，一时很难缓过来。",
            )
        if battle_outcome == "draw":
            return ("双方在这一层僵住了，一时谁也没能压垮谁。",)
        return ()

    def _build_reward_scene_lines(
        self,
        *,
        floor: int,
        battle_outcome: str | None,
        reward_granted: bool | None,
        stable_reward_summary: Mapping[str, int],
        drop_progress_gained: int,
        cumulative_drop_progress: int | None,
        claimable_drop_count: int | None,
    ) -> tuple[str, ...]:
        pending_drop_progress = max(0, cumulative_drop_progress or 0)
        available_drop_count = max(0, claimable_drop_count or 0)
        if battle_outcome is None:
            return (
                "这一层还没真正开打。",
                self._build_drop_progress_story(
                    pending_drop_progress=pending_drop_progress,
                    claimable_drop_count=available_drop_count,
                ),
            )
        if reward_granted:
            return (
                f"这一层带回 {self._format_reward_summary_text(reward_mapping=stable_reward_summary)}。",
                self._build_floor_progress_story(
                    gained=max(0, drop_progress_gained),
                    pending_drop_progress=pending_drop_progress,
                    claimable_drop_count=available_drop_count,
                ),
            )
        if battle_outcome == "enemy_victory":
            return (
                "这一次没能再从敌阵里带回新的层内收获。",
                self._build_drop_progress_story(
                    pending_drop_progress=pending_drop_progress,
                    claimable_drop_count=available_drop_count,
                ),
            )
        return (
            f"第 {floor} 层的战果还没有真正落定。",
            self._build_drop_progress_story(
                pending_drop_progress=pending_drop_progress,
                claimable_drop_count=available_drop_count,
            ),
        )

    @staticmethod
    def _format_reward_summary_text(*, reward_mapping: Mapping[str, int]) -> str:
        parts: list[str] = []
        for key in ("cultivation", "insight", "refining_essence"):
            value = max(0, _read_int(reward_mapping.get(key)))
            if value <= 0:
                continue
            parts.append(f"{_STABLE_REWARD_NAME_BY_KEY.get(key, key)} +{value}")
        if not parts:
            return "没有新的稳定收获"
        return "｜".join(parts)

    @staticmethod
    def _build_floor_progress_story(
        *,
        gained: int,
        pending_drop_progress: int,
        claimable_drop_count: int,
    ) -> str:
        if claimable_drop_count > 0:
            return f"掉落进度又涨 {gained}，累计 {pending_drop_progress}，已凝成 {claimable_drop_count} 次掉落。"
        return f"掉落进度又涨 {gained}，累计到了 {pending_drop_progress}。"

    @staticmethod
    def _build_drop_progress_story(*, pending_drop_progress: int, claimable_drop_count: int) -> str:
        if claimable_drop_count > 0:
            return f"累计掉落进度停在 {pending_drop_progress}，但已凝成 {claimable_drop_count} 次掉落。"
        return f"累计掉落进度停在 {pending_drop_progress}，还得继续往下攒。"

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
        result = str(summary_payload.get("result") or battle_report.result)
        completed_rounds = _read_int(summary_payload.get("completed_rounds"))
        focus_unit_name = str(summary_payload.get("focus_unit_name") or f"角色 {battle_report.character_id}")
        ally_damage_dealt = _read_int(damage_summary.get("ally_damage_dealt"))
        ally_damage_taken = _read_int(damage_summary.get("ally_damage_taken"))
        ally_healing_done = _read_int(healing_summary.get("ally_healing_done"))
        successful_hits = _read_int(key_trigger_counts.get("successful_hits"))
        critical_hits = _read_int(key_trigger_counts.get("critical_hits"))
        control_skips = _read_int(key_trigger_counts.get("control_skips"))
        unit_defeated = _read_int(key_trigger_counts.get("unit_defeated"))
        action_highlights = self._extract_action_highlights(
            summary_payload=summary_payload,
            detail_payload=detail_payload,
        )
        round_highlights = self._extract_round_highlights(detail_payload=detail_payload)
        return EndlessBattleReportDigest(
            battle_report_id=battle_report.id,
            result=result,
            completed_rounds=completed_rounds,
            focus_unit_name=focus_unit_name,
            final_hp_ratio=str(summary_payload.get("final_hp_ratio") or "0.0000"),
            final_mp_ratio=str(summary_payload.get("final_mp_ratio") or "0.0000"),
            ally_damage_dealt=ally_damage_dealt,
            ally_damage_taken=ally_damage_taken,
            ally_healing_done=ally_healing_done,
            successful_hits=successful_hits,
            critical_hits=critical_hits,
            control_skips=control_skips,
            unit_defeated=unit_defeated,
            action_highlights=action_highlights,
            round_highlights=round_highlights,
            narration_lines=self._build_battle_narration_lines(
                focus_unit_name=focus_unit_name,
                result=result,
                completed_rounds=completed_rounds,
                action_highlights=action_highlights,
                round_highlights=round_highlights,
                critical_hits=critical_hits,
                control_skips=control_skips,
                unit_defeated=unit_defeated,
                ally_damage_dealt=ally_damage_dealt,
                ally_damage_taken=ally_damage_taken,
                ally_healing_done=ally_healing_done,
            ),
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
        unit_side_by_id = self._build_unit_side_mapping(detail_payload=detail_payload)
        action_label_by_id = self._build_action_label_mapping(detail_payload=detail_payload)
        events_by_round: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for event in event_sequence:
            round_index = _read_int(event.get("round_index"))
            if round_index <= 0:
                continue
            events_by_round[round_index].append(event)
        candidates: list[tuple[int, int, str]] = []
        for round_index in sorted(events_by_round):
            round_events = sorted(events_by_round[round_index], key=lambda item: _read_int(item.get("sequence")))
            defeated_targets = {
                unit_name_by_id.get(str(event.get("target_unit_id") or ""), str(event.get("target_unit_id") or "敌影"))
                for event in round_events
                if str(event.get("event_type") or "") == "unit_defeated"
            }
            current_action_by_actor: dict[str, str] = {}
            pending_critical_hits: dict[tuple[str, str, str], int] = defaultdict(int)
            strongest_candidate: tuple[int, str] | None = None
            for event in round_events:
                event_type = str(event.get("event_type") or "")
                detail = _normalize_optional_mapping(event.get("detail")) or {}
                actor_id = str(event.get("actor_unit_id") or "")
                target_id = str(event.get("target_unit_id") or "")
                action_id = str(event.get("action_id") or "")
                if event_type in {"action_selected", "action_started"}:
                    if actor_id:
                        current_action_by_actor[actor_id] = action_label_by_id.get(action_id, "一式攻势")
                    continue
                if event_type == "crit_check" and actor_id and target_id and bool(detail.get("success")):
                    pending_critical_hits[(actor_id, target_id, action_id)] += 1
                    continue
                if event_type == "damage_resolved":
                    damage = _read_int(detail.get("final_damage"))
                    if damage <= 0 or not actor_id or not target_id:
                        continue
                    actor_side = unit_side_by_id.get(actor_id, "")
                    actor_name = unit_name_by_id.get(actor_id, actor_id or "未知单位")
                    target_name = unit_name_by_id.get(target_id, target_id or "目标")
                    action_label = current_action_by_actor.get(actor_id, action_label_by_id.get(action_id, "一式攻势"))
                    crit_key = (actor_id, target_id, action_id)
                    is_critical = pending_critical_hits.get(crit_key, 0) > 0
                    if is_critical:
                        pending_critical_hits[crit_key] -= 1
                    if actor_side == "ally":
                        if target_name in defeated_targets:
                            crit_text = "暴击" if is_critical else ""
                            line = f"你以{action_label}打出 {damage} 点{crit_text}伤害，{target_name}当场溃散。"
                            score = damage + (4200 if is_critical else 2600)
                        elif is_critical:
                            line = f"你以{action_label}轰出 {damage} 点暴击伤害，逼得{target_name}气息大乱。"
                            score = damage + 2200
                        else:
                            line = f"你以{action_label}斩出 {damage} 点伤害，压得{target_name}连连后退。"
                            score = damage + 1200
                    elif actor_side == "enemy":
                        if is_critical:
                            line = f"{actor_name}猛扑而上，打得你硬吃 {damage} 点暴击伤害。"
                            score = damage + 2100
                        else:
                            line = f"{actor_name}反压一记，你硬吃 {damage} 点伤害。"
                            score = damage + 900
                    else:
                        line = f"{actor_name}打出 {damage} 点伤害，场面骤然一紧。"
                        score = damage
                    if strongest_candidate is None or score > strongest_candidate[0]:
                        strongest_candidate = (score, line)
                    continue
                if event_type == "damage_over_time_tick":
                    damage = max(_read_int(detail.get("hp_damage")), _read_int(detail.get("total_damage")))
                    if damage <= 0 or not target_id:
                        continue
                    target_side = unit_side_by_id.get(target_id, "")
                    target_name = unit_name_by_id.get(target_id, target_id or "目标")
                    if target_side == "enemy":
                        line = f"余劲未散，又蚀掉{target_name} {damage} 点气血。"
                        score = damage + 500
                    else:
                        line = f"残留妖气继续翻涌，又从你身上撕走 {damage} 点气血。"
                        score = damage + 400
                    if strongest_candidate is None or score > strongest_candidate[0]:
                        strongest_candidate = (score, line)
                    continue
                if event_type == "healing_applied":
                    healed = _read_int(detail.get("healed_hp"))
                    if healed <= 0 or not actor_id:
                        continue
                    actor_side = unit_side_by_id.get(actor_id, "")
                    action_label = current_action_by_actor.get(actor_id, action_label_by_id.get(action_id, "调息"))
                    if actor_side == "ally":
                        line = f"你借{action_label}回稳 {healed} 点气血，勉强把伤势压住。"
                        score = healed + 650
                    else:
                        actor_name = unit_name_by_id.get(actor_id, actor_id or "对手")
                        line = f"{actor_name}缓回了 {healed} 点气血，气势又续了上来。"
                        score = healed + 300
                    if strongest_candidate is None or score > strongest_candidate[0]:
                        strongest_candidate = (score, line)
                    continue
                if event_type == "turn_skipped_by_control" and actor_id:
                    actor_name = unit_name_by_id.get(actor_id, actor_id or "对手")
                    actor_side = unit_side_by_id.get(actor_id, "")
                    if actor_side == "enemy":
                        line = f"{actor_name}被压得气机一滞，白白错过了一次出手机会。"
                        score = 780
                    else:
                        line = "你气机一滞，被硬生生压掉了一次出手机会。"
                        score = 720
                    if strongest_candidate is None or score > strongest_candidate[0]:
                        strongest_candidate = (score, line)
            if strongest_candidate is not None:
                score, line = strongest_candidate
                candidates.append((score, round_index, line))
        candidates.sort(key=lambda item: (-item[0], item[1]))
        return tuple(line for _, _, line in candidates[:3])

    def _build_battle_narration_lines(
        self,
        *,
        focus_unit_name: str,
        result: str,
        completed_rounds: int,
        action_highlights: Sequence[str],
        round_highlights: Sequence[str],
        critical_hits: int,
        control_skips: int,
        unit_defeated: int,
        ally_damage_dealt: int,
        ally_damage_taken: int,
        ally_healing_done: int,
    ) -> tuple[str, ...]:
        lines: list[str] = []
        for raw_line in round_highlights:
            narration = self._build_round_narration(
                raw_line=raw_line,
                focus_unit_name=focus_unit_name,
            )
            if narration is None or narration in lines:
                continue
            lines.append(narration)
            break
        closing_line = self._build_battle_closing_line(
            result=result,
            completed_rounds=completed_rounds,
            critical_hits=critical_hits,
            control_skips=control_skips,
            unit_defeated=unit_defeated,
            ally_damage_dealt=ally_damage_dealt,
            ally_damage_taken=ally_damage_taken,
            ally_healing_done=ally_healing_done,
        )
        if closing_line is not None and closing_line not in lines:
            lines.append(closing_line)
        action_line = self._build_action_narration(
            focus_unit_name=focus_unit_name,
            action_highlights=action_highlights,
        )
        if action_line is not None and action_line not in lines and len(lines) < 3:
            lines.append(action_line)
        if not lines:
            lines.append("这一层厮杀来得极快，你只记得妖气和血光一起炸开。")
        return tuple(lines[:3])

    @staticmethod
    def _build_action_narration(
        *,
        focus_unit_name: str,
        action_highlights: Sequence[str],
    ) -> str | None:
        if not action_highlights:
            return None
        action_text = str(action_highlights[0]).strip()
        if not action_text:
            return None
        action_label = action_text
        use_count = 0
        if "×" in action_text:
            action_label, _, count_text = action_text.partition("×")
            use_count = _parse_positive_int(count_text)
        normalized_label = action_label.strip()
        if not normalized_label:
            return None
        del focus_unit_name
        if use_count >= 2:
            return f"你连催{normalized_label}，先把敌势压住了。"
        return f"你起手便祭出{normalized_label}，直接撞进敌阵。"

    @staticmethod
    def _build_round_narration(*, raw_line: str, focus_unit_name: str) -> str | None:
        normalized_line = raw_line.strip()
        if not normalized_line:
            return None
        if "：" not in normalized_line and "｜" not in normalized_line:
            return normalized_line
        content = normalized_line.split("：", 1)[1] if "：" in normalized_line else normalized_line
        parts = [item.strip() for item in content.split("｜") if item.strip()]
        action_part = ""
        critical_hits = 0
        control_skips = 0
        defeated_target = ""
        status_changes = 0
        for part in parts:
            if part.startswith("出手 "):
                action_part = part.removeprefix("出手 ").split("、", 1)[0].strip()
                continue
            if part.startswith("暴击 "):
                critical_hits = _parse_positive_int(part.removeprefix("暴击 "))
                continue
            if part.startswith("控场 "):
                control_skips = _parse_positive_int(part.removeprefix("控场 "))
                continue
            if part.startswith("击破 "):
                defeated_target = part.removeprefix("击破 ").split("、", 1)[0].strip()
                continue
            if part.startswith("状态变化 "):
                status_changes = _parse_positive_int(part.removeprefix("状态变化 "))
        actor_name = "你"
        action_label = ""
        if action_part:
            candidate_actor, separator, candidate_action = action_part.partition("·")
            if separator:
                actor_name = "你" if not candidate_actor or candidate_actor == focus_unit_name else candidate_actor
                action_label = candidate_action.strip()
            else:
                action_label = action_part
        if defeated_target:
            if action_label and actor_name == "你":
                return f"你借{action_label}撕开缺口，{defeated_target}当场溃散。"
            if action_label:
                return f"{actor_name}借{action_label}撕开缺口，{defeated_target}当场溃散。"
            return f"混战正紧时，{defeated_target}当场溃散。"
        if critical_hits > 0:
            if action_label and actor_name == "你":
                return f"你催动{action_label}狠狠干下一记，逼得对面护体乱了一瞬。"
            if action_label:
                return f"{actor_name}催动{action_label}狠狠干下一记，场面顿时一震。"
            return f"{actor_name}突然打出一记重击，场中妖气都被震乱。"
        if control_skips > 0:
            if action_label and actor_name == "你":
                return f"你借{action_label}压住敌阵，对面一时没能缓过气。"
            if action_label:
                return f"{actor_name}借{action_label}压得人一时难以招架。"
            return f"{actor_name}把场面压得很死，对面一时没能缓过气。"
        if status_changes > 0:
            if action_label and actor_name == "你":
                return f"你一动{action_label}，场中气机立刻翻涌起来。"
            if action_label:
                return f"{actor_name}一动{action_label}，场中气机立刻翻涌起来。"
            return "这一轮气机翻得很乱，谁都不敢大意。"
        if action_label and actor_name == "你":
            return f"你先以{action_label}探路，转眼就和对面缠成了一团。"
        if action_label:
            return f"{actor_name}先以{action_label}探路，转眼就把场面搅紧了。"
        return None

    @staticmethod
    def _build_battle_closing_line(
        *,
        result: str,
        completed_rounds: int,
        critical_hits: int,
        control_skips: int,
        unit_defeated: int,
        ally_damage_dealt: int,
        ally_damage_taken: int,
        ally_healing_done: int,
    ) -> str | None:
        if result == "ally_victory":
            if unit_defeated > 1 and ally_damage_dealt > 0:
                return f"这一战你累计打出 {ally_damage_dealt} 点伤害，连斩 {unit_defeated} 名敌人拿下此层。"
            if critical_hits > 0 and ally_damage_dealt > 0:
                return f"几次重击之后，你累计打出 {ally_damage_dealt} 点伤害，终于压垮了最后一名敌人。"
            if ally_healing_done > 0 and ally_damage_dealt > 0:
                return (
                    f"鏖战 {max(1, completed_rounds)} 回合后，你以 {ally_damage_dealt} 点总伤害取胜，"
                    f"并回稳 {ally_healing_done} 点气血。"
                )
            if control_skips > 0 and ally_damage_dealt > 0:
                return f"你稳稳压住节奏，以 {ally_damage_dealt} 点总伤害把这一层硬生生拿下。"
            if ally_damage_dealt > 0:
                return f"鏖战 {max(1, completed_rounds)} 回合后，你累计打出 {ally_damage_dealt} 点伤害，稳住了这一层。"
            return f"鏖战 {max(1, completed_rounds)} 回合后，你还是稳住了这一层。"
        if result == "enemy_victory":
            if ally_damage_taken > 0 and completed_rounds > 0:
                return f"撑到第 {completed_rounds} 回合后，你累计承受 {ally_damage_taken} 点伤害，被这一层的反扑压了下去。"
            if ally_damage_taken > 0:
                return f"这一层的反扑太狠，你累计承受 {ally_damage_taken} 点伤害，被迫止步于此。"
            return "你还没来得及稳住局势，就被这一层逼退了。"
        if result == "draw":
            if ally_damage_dealt > 0 or ally_damage_taken > 0:
                return f"双方僵持不下，你打出 {ally_damage_dealt} 点伤害，也承受了 {ally_damage_taken} 点伤害。"
            return "这一层僵持不下，谁也没能立刻终结对方。"
        return None

    @staticmethod
    def _build_unit_name_mapping(*, detail_payload: Mapping[str, Any]) -> dict[str, str]:
        mapping: dict[str, str] = {}
        input_snapshot_summary = _normalize_optional_mapping(detail_payload.get("input_snapshot_summary")) or {}
        allies = _normalize_mapping_list(input_snapshot_summary.get("allies"))
        enemies = _normalize_mapping_list(input_snapshot_summary.get("enemies"))
        enemy_total_count = len(enemies)
        enemy_index_by_id: dict[str, int] = {}
        for item in allies:
            unit_id = str(item.get("unit_id") or "")
            unit_name = str(item.get("unit_name") or unit_id)
            if unit_id:
                mapping[unit_id] = EndlessPanelQueryService._sanitize_unit_display_name(
                    raw_name=unit_name,
                    side="ally",
                    index=0,
                    total_count=0,
                )
        for index, item in enumerate(enemies, start=1):
            unit_id = str(item.get("unit_id") or "")
            unit_name = str(item.get("unit_name") or unit_id)
            if unit_id:
                enemy_index_by_id[unit_id] = index
                mapping[unit_id] = EndlessPanelQueryService._sanitize_unit_display_name(
                    raw_name=unit_name,
                    side="enemy",
                    index=index,
                    total_count=enemy_total_count,
                )
        terminal_statistics = _normalize_optional_mapping(detail_payload.get("terminal_statistics")) or {}
        final_units = _normalize_mapping_list(terminal_statistics.get("final_units"))
        fallback_enemy_total_count = max(
            enemy_total_count,
            sum(1 for item in final_units if str(item.get("side") or "") == "enemy"),
        )
        for item in final_units:
            unit_id = str(item.get("unit_id") or "")
            unit_name = str(item.get("unit_name") or unit_id)
            side = str(item.get("side") or "")
            if not unit_id:
                continue
            if side == "enemy":
                enemy_index = enemy_index_by_id.get(unit_id, _extract_enemy_index_from_unit_id(unit_id))
                mapping[unit_id] = EndlessPanelQueryService._sanitize_unit_display_name(
                    raw_name=unit_name,
                    side=side,
                    index=enemy_index,
                    total_count=fallback_enemy_total_count,
                )
                continue
            mapping[unit_id] = EndlessPanelQueryService._sanitize_unit_display_name(
                raw_name=unit_name,
                side=side,
                index=0,
                total_count=0,
            )
        return mapping

    @staticmethod
    def _build_unit_side_mapping(*, detail_payload: Mapping[str, Any]) -> dict[str, str]:
        mapping: dict[str, str] = {}
        input_snapshot_summary = _normalize_optional_mapping(detail_payload.get("input_snapshot_summary")) or {}
        for group_name, side in (("allies", "ally"), ("enemies", "enemy")):
            for item in _normalize_mapping_list(input_snapshot_summary.get(group_name)):
                unit_id = str(item.get("unit_id") or "")
                if unit_id:
                    mapping[unit_id] = side
        terminal_statistics = _normalize_optional_mapping(detail_payload.get("terminal_statistics")) or {}
        for item in _normalize_mapping_list(terminal_statistics.get("final_units")):
            unit_id = str(item.get("unit_id") or "")
            side = str(item.get("side") or "")
            if unit_id and side in {"ally", "enemy"}:
                mapping[unit_id] = side
        return mapping

    @staticmethod
    def _sanitize_unit_display_name(*, raw_name: str, side: str, index: int, total_count: int) -> str:
        normalized_raw_name = raw_name.strip()
        if side != "enemy":
            return normalized_raw_name or ("你" if side == "ally" else "未知单位")
        noun = EndlessPanelQueryService._resolve_enemy_name_noun(raw_name=normalized_raw_name, race_name="")
        if index <= 0:
            return noun
        prefix = EndlessPanelQueryService._build_positional_enemy_prefix(
            index=index,
            total_count=max(total_count, index),
        )
        return f"{prefix}{noun}" if prefix else noun

    @staticmethod
    def _build_action_label_mapping(*, detail_payload: Mapping[str, Any]) -> dict[str, str]:
        terminal_statistics = _normalize_optional_mapping(detail_payload.get("terminal_statistics")) or {}
        label_by_id: dict[str, str] = {}
        for template in _normalize_mapping_list(terminal_statistics.get("behavior_templates")):
            for action in _normalize_mapping_list(template.get("actions")):
                action_id = str(action.get("action_id") or "")
                if not action_id:
                    continue
                action_name = str(action.get("name") or "").strip()
                if action_name and not _looks_like_internal_identifier(action_name):
                    label_by_id[action_id] = action_name
                    continue
                labels = action.get("labels")
                if isinstance(labels, list | tuple):
                    for item in labels:
                        if not isinstance(item, str) or not item.strip():
                            continue
                        readable_label = _humanize_action_identifier(item)
                        if readable_label is not None:
                            label_by_id[action_id] = readable_label
                            break
                if action_id not in label_by_id:
                    label_by_id[action_id] = _humanize_action_identifier(action_id) or "一式攻势"
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


def _looks_like_internal_identifier(value: str) -> bool:
    normalized_value = value.strip()
    if not normalized_value:
        return False
    if any(separator in normalized_value for separator in ("_", ":")):
        return True
    return all(ord(char) < 128 for char in normalized_value)


def _extract_counter_stem(value: str) -> str | None:
    normalized_value = value.strip()
    if not normalized_value.endswith("号"):
        return None
    digit_index = next((index for index, char in enumerate(normalized_value) if char.isdigit()), -1)
    if digit_index <= 0:
        return None
    counter_text = normalized_value[digit_index:-1]
    if not counter_text or not all(char.isdigit() for char in counter_text):
        return None
    stem = normalized_value[:digit_index].strip()
    return stem or None


def _extract_enemy_index_from_unit_id(value: str) -> int:
    normalized_value = value.strip()
    if not normalized_value:
        return 0
    tail = normalized_value.rsplit(":", 1)[-1]
    return int(tail) if tail.isdigit() else 0


def _humanize_action_identifier(value: str) -> str | None:
    normalized_value = value.strip().lower()
    if not normalized_value:
        return None
    exact_mapping = {
        "single_target": "单体杀招",
        "opening": "起手式",
        "sword_intent": "剑意一击",
        "burst": "爆发杀招",
        "first_strike": "先手压制",
        "execute": "斩杀一击",
        "combo": "连斩",
        "tempo": "抢势一击",
        "pursuit": "追击",
        "guard_convert": "转守为攻",
        "counter": "反击",
        "retaliation": "回击",
        "sustain": "续战调息",
        "frontline": "正面硬撼",
        "spell_focus": "术法聚势",
        "control": "控场术",
        "interrupt": "断势术",
        "debuff": "压制术",
        "anti_guard": "破甲术",
        "resource_cycle": "调息回转",
        "attrition": "蚀气余劲",
    }
    if normalized_value in exact_mapping:
        return exact_mapping[normalized_value]
    token_mapping = {
        "single": "单体",
        "target": "杀招",
        "opening": "起手",
        "sword": "剑式",
        "intent": "剑意",
        "burst": "爆发",
        "first": "先手",
        "strike": "重击",
        "execute": "斩杀",
        "combo": "连斩",
        "tempo": "抢势",
        "pursuit": "追击",
        "guard": "护体",
        "convert": "转势",
        "counter": "反击",
        "retaliation": "回击",
        "sustain": "续战",
        "frontline": "前压",
        "spell": "术法",
        "focus": "聚势",
        "control": "控场",
        "interrupt": "断势",
        "debuff": "压制",
        "anti": "破",
        "resource": "调息",
        "cycle": "回转",
        "attrition": "蚀气",
    }
    humanized = "".join(token_mapping.get(token, "") for token in normalized_value.split("_") if token)
    return humanized or None


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


def _parse_positive_int(text: str) -> int:
    digits = "".join(character for character in str(text) if character.isdigit())
    if not digits:
        return 0
    return int(digits)


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
