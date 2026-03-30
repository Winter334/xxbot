"""突破秘境面板查询适配服务。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from application.breakthrough.reward_service import BreakthroughRewardApplicationResult
from application.breakthrough.trial_service import (
    BreakthroughTrialEntrySnapshot,
    BreakthroughTrialHubSnapshot,
    BreakthroughTrialService,
)
from application.character.panel_query_service import CharacterPanelOverview, CharacterPanelQueryService
from application.character.progression_service import BreakthroughPrecheckResult, CharacterProgressionService
from infrastructure.config.static import StaticGameConfig, get_static_config
from infrastructure.config.static.models.breakthrough import BreakthroughTrialDefinition
from infrastructure.db.models import BattleReport, BreakthroughTrialProgress
from infrastructure.db.repositories import BattleRecordRepository, BreakthroughRepository

_RESOURCE_NAME_BY_ID = {
    "spirit_stone": "灵石",
    "enhancement_stone": "强化石",
    "enhancement_shard": "强化碎晶",
    "wash_dust": "洗炼尘",
    "spirit_sand": "灵砂",
    "spirit_pattern_stone": "灵纹石",
    "soul_binding_jade": "缚魂玉",
    "artifact_essence": "法宝精粹",
    "qi_condensation_grass": "凝气草",
    "foundation_pill": "筑基丹",
    "core_crystal": "金丹晶核",
    "nascent_soul_lotus": "元婴莲",
    "deity_heart_incense": "化神心香",
    "void_breaking_stone": "破虚石",
    "body_refining_marble": "合体玄玉",
    "great_vehicle_golden_leaf": "大乘金叶",
    "tribulation_guiding_talisman": "引劫符",
}
_SETTLEMENT_LABEL_BY_VALUE = {
    "defeat": "试炼失败",
    "first_clear": "首次通关",
    "repeat_clear": "重复通关",
}


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
class BreakthroughGoalCard:
    """突破目标卡。"""

    current_realm_name: str
    current_stage_name: str
    target_realm_name: str
    qualification_obtained: bool
    can_breakthrough: bool


@dataclass(frozen=True, slots=True)
class BreakthroughTrialCard:
    """当前试炼卡。"""

    mapping_id: str
    trial_name: str
    group_name: str
    target_realm_name: str
    environment_rule: str
    can_challenge: bool
    is_cleared: bool
    attempt_count: int
    cleared_count: int


@dataclass(frozen=True, slots=True)
class BreakthroughStatusCard:
    """我方状态卡。"""

    current_realm_name: str
    current_stage_name: str
    current_hp_ratio: str
    current_mp_ratio: str
    qualification_obtained: bool
    current_cultivation_value: int
    required_cultivation_value: int | None
    current_comprehension_value: int
    required_comprehension_value: int | None


@dataclass(frozen=True, slots=True)
class BreakthroughGapCard:
    """突破缺口卡。"""

    passed: bool
    lines: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BreakthroughRecentResultCard:
    """最近结果卡。"""

    trial_name: str
    result_label: str
    qualification_changed: bool
    reward_summary: str
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class BreakthroughRecentSettlementSnapshot:
    """突破秘境最近一次可复读的结算快照。"""

    mapping_id: str
    trial_name: str
    group_id: str
    group_name: str
    occurred_at: datetime
    settlement: BreakthroughRewardApplicationResult
    goal_card: BreakthroughGoalCard
    trial_card: BreakthroughTrialCard
    status_card: BreakthroughStatusCard
    gap_card: BreakthroughGapCard
    recent_result_card: BreakthroughRecentResultCard
    battle_report_digest: BreakthroughBattleReportDigest | None


@dataclass(frozen=True, slots=True)
class BreakthroughPanelSnapshot:
    """突破秘境私有面板所需聚合快照。"""

    overview: CharacterPanelOverview
    precheck: BreakthroughPrecheckResult
    hub: BreakthroughTrialHubSnapshot
    goal_card: BreakthroughGoalCard
    current_trial_card: BreakthroughTrialCard | None
    status_card: BreakthroughStatusCard
    gap_card: BreakthroughGapCard
    recent_result_card: BreakthroughRecentResultCard | None
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
        overview = self._character_panel_query_service.get_overview(character_id=character_id)
        precheck = self._progression_service.get_breakthrough_precheck(character_id=character_id)
        hub = self._trial_service.get_trial_hub(character_id=character_id)
        recent_settlement = self.get_recent_settlement_snapshot(character_id=character_id)
        return BreakthroughPanelSnapshot(
            overview=overview,
            precheck=precheck,
            hub=hub,
            goal_card=self._build_goal_card(overview=overview, precheck=precheck, hub=hub),
            current_trial_card=self._build_trial_card_from_entry(
                trial=hub.current_trial,
                target_realm_name=precheck.target_realm_name,
            ),
            status_card=self._build_status_card(overview=overview, precheck=precheck, hub=hub),
            gap_card=self._build_gap_card(precheck=precheck),
            recent_result_card=None if recent_settlement is None else recent_settlement.recent_result_card,
            recent_settlement=recent_settlement,
        )

    def get_recent_settlement_snapshot(self, *, character_id: int) -> BreakthroughRecentSettlementSnapshot | None:
        """读取最近一次突破秘境结算及其展示上下文。"""
        overview = self._character_panel_query_service.get_overview(character_id=character_id)
        precheck = self._progression_service.get_breakthrough_precheck(character_id=character_id)
        hub = self._trial_service.get_trial_hub(character_id=character_id)
        progress_entries = self._breakthrough_repository.list_by_character_id(character_id)
        progress_entry, payload, occurred_at = self._resolve_latest_settlement(progress_entries=progress_entries)
        if progress_entry is None or payload is None or occurred_at is None:
            return None
        trial = self._require_trial(progress_entry.mapping_id)
        battle_report_id = _read_optional_int(payload.get("battle_report_id"))
        settlement = self._build_application_result(progress_entry=progress_entry, payload=payload)
        reward_summary = self._build_compact_reward_summary(settlement=settlement)
        goal_card = self._build_goal_card(overview=overview, precheck=precheck, hub=hub)
        trial_card = self._build_trial_card_from_definition(
            trial=trial,
            progress_entry=progress_entry,
            target_realm_name=precheck.target_realm_name,
        )
        status_card = self._build_status_card(overview=overview, precheck=precheck, hub=hub)
        gap_card = self._build_gap_card(precheck=precheck)
        recent_result_card = BreakthroughRecentResultCard(
            trial_name=trial.name,
            result_label=_SETTLEMENT_LABEL_BY_VALUE.get(settlement.settlement_type, settlement.settlement_type),
            qualification_changed=settlement.qualification_granted,
            reward_summary=reward_summary,
            occurred_at=occurred_at,
        )
        return BreakthroughRecentSettlementSnapshot(
            mapping_id=trial.mapping_id,
            trial_name=trial.name,
            group_id=trial.group_id,
            group_name=self._group_name_by_id.get(trial.group_id, trial.group_id),
            occurred_at=occurred_at,
            settlement=settlement,
            goal_card=goal_card,
            trial_card=trial_card,
            status_card=status_card,
            gap_card=gap_card,
            recent_result_card=recent_result_card,
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

    def _build_goal_card(
        self,
        *,
        overview: CharacterPanelOverview,
        precheck: BreakthroughPrecheckResult,
        hub: BreakthroughTrialHubSnapshot,
    ) -> BreakthroughGoalCard:
        target_realm_name = precheck.target_realm_name or "当前已到开放上限"
        return BreakthroughGoalCard(
            current_realm_name=overview.realm_name,
            current_stage_name=overview.stage_name,
            target_realm_name=target_realm_name,
            qualification_obtained=hub.qualification_obtained,
            can_breakthrough=precheck.passed,
        )

    def _build_trial_card_from_entry(
        self,
        *,
        trial: BreakthroughTrialEntrySnapshot | None,
        target_realm_name: str | None,
    ) -> BreakthroughTrialCard | None:
        if trial is None:
            return None
        return BreakthroughTrialCard(
            mapping_id=trial.mapping_id,
            trial_name=trial.trial_name,
            group_name=self._group_name_by_id.get(trial.group_id, trial.group_id),
            target_realm_name=target_realm_name or trial.to_realm_id,
            environment_rule=trial.environment_rule,
            can_challenge=trial.can_challenge,
            is_cleared=trial.is_cleared,
            attempt_count=trial.attempt_count,
            cleared_count=trial.cleared_count,
        )

    def _build_trial_card_from_definition(
        self,
        *,
        trial: BreakthroughTrialDefinition,
        progress_entry: BreakthroughTrialProgress,
        target_realm_name: str | None,
    ) -> BreakthroughTrialCard:
        return BreakthroughTrialCard(
            mapping_id=trial.mapping_id,
            trial_name=trial.name,
            group_name=self._group_name_by_id.get(trial.group_id, trial.group_id),
            target_realm_name=target_realm_name or trial.to_realm_id,
            environment_rule=trial.environment_rule_id,
            can_challenge=True,
            is_cleared=max(0, progress_entry.cleared_count) > 0,
            attempt_count=max(0, progress_entry.attempt_count),
            cleared_count=max(0, progress_entry.cleared_count),
        )

    @staticmethod
    def _build_status_card(
        *,
        overview: CharacterPanelOverview,
        precheck: BreakthroughPrecheckResult,
        hub: BreakthroughTrialHubSnapshot,
    ) -> BreakthroughStatusCard:
        return BreakthroughStatusCard(
            current_realm_name=overview.realm_name,
            current_stage_name=overview.stage_name,
            current_hp_ratio=hub.current_hp_ratio,
            current_mp_ratio=hub.current_mp_ratio,
            qualification_obtained=hub.qualification_obtained,
            current_cultivation_value=precheck.current_cultivation_value,
            required_cultivation_value=precheck.required_cultivation_value,
            current_comprehension_value=precheck.current_comprehension_value,
            required_comprehension_value=precheck.required_comprehension_value,
        )

    def _build_gap_card(self, *, precheck: BreakthroughPrecheckResult) -> BreakthroughGapCard:
        lines = self._build_gap_lines(precheck=precheck)
        return BreakthroughGapCard(
            passed=precheck.passed,
            lines=tuple(lines) if lines else ("已满足",),
        )

    def _build_gap_lines(self, *, precheck: BreakthroughPrecheckResult) -> list[str]:
        gap_lines: list[str] = []
        for gap in precheck.gaps:
            if gap.gap_type == "open_limit":
                gap_lines.append("当前已到开放上限")
            elif gap.gap_type == "cultivation_insufficient":
                gap_lines.append(f"修为还差 {gap.missing_value}")
            elif gap.gap_type == "comprehension_insufficient":
                gap_lines.append(f"感悟还差 {gap.missing_value}")
            elif gap.gap_type == "qualification_missing":
                gap_lines.append("缺少突破资格")
            elif gap.gap_type == "material_insufficient":
                item_name = _RESOURCE_NAME_BY_ID.get(gap.item_id or "", gap.item_id or "材料")
                gap_lines.append(f"{item_name} 还差 {gap.missing_value}")
        return gap_lines

    @staticmethod
    def _build_compact_reward_summary(*, settlement: BreakthroughRewardApplicationResult) -> str:
        parts: list[str] = []
        if settlement.qualification_granted:
            parts.append("突破资格")
        for resource_id, quantity in settlement.currency_changes.items():
            if quantity > 0:
                parts.append(f"{_RESOURCE_NAME_BY_ID.get(resource_id, resource_id)} +{quantity}")
        for item in settlement.item_changes:
            item_id = str(item.get("item_id") or "")
            quantity = _read_int(item.get("quantity"))
            if quantity > 0:
                parts.append(f"{_RESOURCE_NAME_BY_ID.get(item_id, item_id or '未知物品')} +{quantity}")
        if not parts:
            return "无"
        return "｜".join(parts)

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
    "BreakthroughGapCard",
    "BreakthroughGoalCard",
    "BreakthroughPanelService",
    "BreakthroughPanelServiceError",
    "BreakthroughPanelSnapshot",
    "BreakthroughRecentResultCard",
    "BreakthroughRecentSettlementSnapshot",
    "BreakthroughStatusCard",
    "BreakthroughTrialCard",
]
