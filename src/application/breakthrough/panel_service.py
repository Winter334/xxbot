"""突破三问面板查询服务。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from application.battle import BattleReplayDisplayContext, BattleReplayPresentation, BattleReplayService
from application.breakthrough.trial_service import BreakthroughTrialHubSnapshot, BreakthroughTrialService
from application.character.panel_query_service import CharacterPanelOverview, CharacterPanelQueryService
from application.character.progression_service import BreakthroughPrecheckResult, CharacterProgressionService
from infrastructure.config.static import StaticGameConfig, get_static_config
from infrastructure.config.static.models.breakthrough import BreakthroughTrialDefinition
from infrastructure.db.models import BreakthroughTrialProgress
from infrastructure.db.repositories import (
    BattleRecordRepository,
    BreakthroughRepository,
    InventoryRepository,
)

_RESOURCE_NAME_BY_ID = {
    "qi_condensation_grass": "凝气草",
    "foundation_pill": "筑基丹",
    "spirit_pattern_stone": "灵纹石",
    "core_congealing_pellet": "凝丹丸",
    "fire_essence_sand": "离火砂",
    "nascent_soul_flower": "元婴花",
    "soul_binding_jade": "缚魂玉",
    "deity_heart_seed": "化神心种",
    "thunder_pattern_branch": "雷纹枝",
    "void_break_crystal": "破虚晶",
    "star_soul_dust": "星魂尘",
    "body_integration_bone": "合体骨",
    "myriad_gold_paste": "万金膏",
    "great_vehicle_core": "大乘核",
    "heaven_pattern_silk": "天纹丝",
    "tribulation_lightning_talisman": "劫雷符",
    "immortal_marrow_liquid": "仙髓液",
}


@dataclass(frozen=True, slots=True)
class BreakthroughMaterialRequirementSnapshot:
    """单条突破材料校验结果。"""

    item_type: str
    item_id: str
    item_name: str
    required_quantity: int
    owned_quantity: int
    missing_quantity: int


@dataclass(frozen=True, slots=True)
class BreakthroughMaterialPageSnapshot:
    """检灵材页面快照。"""

    requirements: tuple[BreakthroughMaterialRequirementSnapshot, ...]
    all_satisfied: bool
    gap_summary: str


@dataclass(frozen=True, slots=True)
class BreakthroughQualificationPageSnapshot:
    """问玄关页面快照。"""

    mapping_id: str | None
    trial_name: str | None
    environment_rule: str | None
    atmosphere_text: str
    passed: bool
    material_gap_text: str
    start_trial_enabled: bool


@dataclass(frozen=True, slots=True)
class BreakthroughRootStatus:
    """根页核心状态。"""

    current_realm_display: str
    next_realm_name: str
    qualification_obtained: bool
    material_ready: bool
    can_breakthrough: bool


@dataclass(frozen=True, slots=True)
class BreakthroughRecentTrialSnapshot:
    """最近一次叩关记录。"""

    mapping_id: str
    trial_name: str
    occurred_at: datetime
    battle_report_id: int | None
    battle_replay_presentation: BattleReplayPresentation | None


@dataclass(frozen=True, slots=True)
class BreakthroughPanelSnapshot:
    """突破三问前台交互所需聚合快照。"""

    overview: CharacterPanelOverview
    precheck: BreakthroughPrecheckResult
    root_status: BreakthroughRootStatus
    qualification_page: BreakthroughQualificationPageSnapshot
    material_page: BreakthroughMaterialPageSnapshot
    recent_trial: BreakthroughRecentTrialSnapshot | None


class BreakthroughPanelServiceError(RuntimeError):
    """突破三问查询服务基础异常。"""


class BreakthroughPanelService:
    """聚合突破根页、问玄关、检灵材与叩关回放所需上下文。"""

    def __init__(
        self,
        *,
        character_panel_query_service: CharacterPanelQueryService,
        progression_service: CharacterProgressionService,
        trial_service: BreakthroughTrialService,
        breakthrough_repository: BreakthroughRepository,
        battle_record_repository: BattleRecordRepository,
        inventory_repository: InventoryRepository,
        static_config: StaticGameConfig | None = None,
        battle_replay_service: BattleReplayService | None = None,
    ) -> None:
        self._character_panel_query_service = character_panel_query_service
        self._progression_service = progression_service
        self._trial_service = trial_service
        self._breakthrough_repository = breakthrough_repository
        self._battle_record_repository = battle_record_repository
        self._inventory_repository = inventory_repository
        self._static_config = static_config or get_static_config()
        self._battle_replay_service = battle_replay_service or BattleReplayService()
        self._trial_by_mapping_id = {
            trial.mapping_id: trial for trial in self._static_config.breakthrough_trials.trials
        }
        self._group_name_by_id = {
            group.group_id: group.name for group in self._static_config.breakthrough_trials.trial_groups
        }

    def get_panel_snapshot(self, *, character_id: int) -> BreakthroughPanelSnapshot:
        """读取突破三问所需聚合数据。"""
        overview = self._character_panel_query_service.get_overview(character_id=character_id)
        precheck = self._progression_service.get_breakthrough_precheck(character_id=character_id)
        hub = self._trial_service.get_trial_hub(character_id=character_id)
        trial_definition = self._resolve_trial_definition(precheck=precheck, hub=hub)
        material_page = self._build_material_page_snapshot(
            character_id=character_id,
            trial_definition=trial_definition,
        )
        qualification_page = self._build_qualification_page_snapshot(
            precheck=precheck,
            hub=hub,
            trial_definition=trial_definition,
            material_page=material_page,
        )
        root_status = BreakthroughRootStatus(
            current_realm_display=f"{overview.realm_name}·{overview.stage_name}",
            next_realm_name=precheck.target_realm_name or "当前已至开放上限",
            qualification_obtained=precheck.qualification_obtained,
            material_ready=material_page.all_satisfied,
            can_breakthrough=precheck.passed,
        )
        return BreakthroughPanelSnapshot(
            overview=overview,
            precheck=precheck,
            root_status=root_status,
            qualification_page=qualification_page,
            material_page=material_page,
            recent_trial=self._build_recent_trial_snapshot(character_id=character_id),
        )

    def _resolve_trial_definition(
        self,
        *,
        precheck: BreakthroughPrecheckResult,
        hub: BreakthroughTrialHubSnapshot,
    ) -> BreakthroughTrialDefinition | None:
        mapping_id = None
        if hub.current_trial is not None:
            mapping_id = hub.current_trial.mapping_id
        elif precheck.mapping_id is not None:
            mapping_id = precheck.mapping_id
        if mapping_id is None:
            return None
        return self._trial_by_mapping_id.get(mapping_id)

    def _build_material_page_snapshot(
        self,
        *,
        character_id: int,
        trial_definition: BreakthroughTrialDefinition | None,
    ) -> BreakthroughMaterialPageSnapshot:
        if trial_definition is None:
            return BreakthroughMaterialPageSnapshot(
                requirements=(),
                all_satisfied=True,
                gap_summary="已无缺漏",
            )
        requirements: list[BreakthroughMaterialRequirementSnapshot] = []
        for requirement in trial_definition.required_items:
            owned_item = self._inventory_repository.get_item(
                character_id,
                requirement.item_type,
                requirement.item_id,
            )
            owned_quantity = 0 if owned_item is None else max(0, int(owned_item.quantity))
            required_quantity = max(0, int(requirement.quantity))
            missing_quantity = max(0, required_quantity - owned_quantity)
            requirements.append(
                BreakthroughMaterialRequirementSnapshot(
                    item_type=requirement.item_type,
                    item_id=requirement.item_id,
                    item_name=_RESOURCE_NAME_BY_ID.get(requirement.item_id, requirement.item_id),
                    required_quantity=required_quantity,
                    owned_quantity=owned_quantity,
                    missing_quantity=missing_quantity,
                )
            )
        missing_lines = [
            f"{item.item_name} ×{item.missing_quantity}"
            for item in requirements
            if item.missing_quantity > 0
        ]
        return BreakthroughMaterialPageSnapshot(
            requirements=tuple(requirements),
            all_satisfied=not missing_lines,
            gap_summary="已无缺漏" if not missing_lines else "；".join(missing_lines),
        )

    def _build_qualification_page_snapshot(
        self,
        *,
        precheck: BreakthroughPrecheckResult,
        hub: BreakthroughTrialHubSnapshot,
        trial_definition: BreakthroughTrialDefinition | None,
        material_page: BreakthroughMaterialPageSnapshot,
    ) -> BreakthroughQualificationPageSnapshot:
        mapping_id = None
        trial_name = None
        environment_rule = None
        if hub.current_trial is not None:
            mapping_id = hub.current_trial.mapping_id
            trial_name = hub.current_trial.trial_name
            environment_rule = hub.current_trial.environment_rule
        elif trial_definition is not None:
            mapping_id = trial_definition.mapping_id
            trial_name = trial_definition.name
            environment_rule = trial_definition.environment_rule
        passed = precheck.qualification_obtained
        return BreakthroughQualificationPageSnapshot(
            mapping_id=mapping_id,
            trial_name=trial_name,
            environment_rule=environment_rule,
            atmosphere_text=self._build_atmosphere_text(
                trial_name=trial_name,
                environment_rule=environment_rule,
            ),
            passed=passed,
            material_gap_text=material_page.gap_summary,
            start_trial_enabled=mapping_id is not None and not passed,
        )

    @staticmethod
    def _build_atmosphere_text(*, trial_name: str | None, environment_rule: str | None) -> str:
        if trial_name is None:
            return "前路暂时无新关可问，山风也在门前缓了下来。"
        if environment_rule:
            return f"门前灵压早已成势，{environment_rule}，只等你亲自上前叩这一关。"
        return "门前风声不言成败，只等你把这一口心气真正送到关前。"

    def _build_recent_trial_snapshot(self, *, character_id: int) -> BreakthroughRecentTrialSnapshot | None:
        progress_entries = self._breakthrough_repository.list_by_character_id(character_id)
        progress_entry, payload, occurred_at = self._resolve_latest_result(progress_entries=progress_entries)
        if progress_entry is None or payload is None or occurred_at is None:
            return None
        trial = self._trial_by_mapping_id.get(progress_entry.mapping_id)
        trial_name = progress_entry.mapping_id if trial is None else trial.name
        battle_report_id = _read_optional_int(payload.get("battle_report_id"))
        return BreakthroughRecentTrialSnapshot(
            mapping_id=progress_entry.mapping_id,
            trial_name=trial_name,
            occurred_at=occurred_at,
            battle_report_id=battle_report_id,
            battle_replay_presentation=self._load_battle_replay_presentation(
                character_id=character_id,
                battle_report_id=battle_report_id,
                trial_definition=trial,
            ),
        )

    def _resolve_latest_result(
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

    def _load_battle_replay_presentation(
        self,
        *,
        character_id: int,
        battle_report_id: int | None,
        trial_definition: BreakthroughTrialDefinition | None,
    ) -> BattleReplayPresentation | None:
        if battle_report_id is None:
            return None
        for battle_report in self._battle_record_repository.list_battle_reports(character_id):
            if battle_report.id != battle_report_id:
                continue
            scene_name = battle_report.battle_type
            environment_name = None
            group_name = None
            if trial_definition is not None:
                scene_name = trial_definition.name
                environment_name = trial_definition.environment_rule
                group_name = self._group_name_by_id.get(trial_definition.group_id)
            return self._battle_replay_service.build_presentation(
                battle_report_id=battle_report.id,
                result=battle_report.result,
                summary_payload=_normalize_mapping(battle_report.summary_json),
                detail_payload=_normalize_mapping(battle_report.detail_log_json),
                context=BattleReplayDisplayContext(
                    source_name="叩关行记",
                    scene_name=scene_name,
                    group_name=group_name,
                    environment_name=environment_name,
                    focus_unit_name=None,
                ),
            )
        return None


def _normalize_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    return {}


def _read_optional_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
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
    "BreakthroughMaterialPageSnapshot",
    "BreakthroughMaterialRequirementSnapshot",
    "BreakthroughPanelService",
    "BreakthroughPanelServiceError",
    "BreakthroughPanelSnapshot",
    "BreakthroughQualificationPageSnapshot",
    "BreakthroughRecentTrialSnapshot",
    "BreakthroughRootStatus",
]
