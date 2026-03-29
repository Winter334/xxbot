"""无尽副本运行态应用服务。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from application.battle import AutoBattleRequest, AutoBattleService
from application.character.current_attribute_service import CurrentAttributeService
from application.character.skill_drop_service import SkillDropService
from application.dungeon.endless_drop_service import EndlessSettlementDropOrchestrator, EndlessSettlementDropOrchestratorError
from application.equipment.equipment_service import EquipmentItemSnapshot, EquipmentService, EquipmentServiceError
from application.naming import ItemNamingBatchService
from domain.battle import BattleOutcome, BattleSide, BattleSnapshot, BattleUnitSnapshot
from domain.character.progression import CharacterGrowthProgression
from domain.dungeon import (
    EndlessDungeonProgression,
    EndlessDungeonRuleError,
    EndlessEncounterGenerator,
    EndlessEnemyEncounter,
    EndlessNodeType,
    EndlessRegionSnapshot,
    EndlessRewardBreakdown,
)
from infrastructure.config.static import StaticGameConfig, get_static_config
from infrastructure.db.models import CharacterProgress, DropRecord, EndlessRunState
from infrastructure.db.repositories import (
    BattleRecordRepository,
    CharacterAggregate,
    CharacterRepository,
    StateRepository,
)

_ENDLESS_STATUS_RUNNING = "running"
_ENDLESS_STATUS_COMPLETED = "completed"
_ENDLESS_STATUS_PENDING_DEFEAT_SETTLEMENT = "pending_defeat_settlement"
_ENDLESS_SETTLEMENT_RETREAT = "retreat"
_ENDLESS_SETTLEMENT_DEFEAT = "defeat"
_REWARD_LEDGER_VERSION = 1
_ENDLESS_BATTLE_TYPE = "endless"
_ENDLESS_DROP_ENTRY_EQUIPMENT = "equipment_drop"
_ENDLESS_DROP_ENTRY_ARTIFACT = "artifact_drop"
_DEFAULT_HERO_TEMPLATE_ID = "zhanqing_sword"
_DEFAULT_ROUND_LIMIT = 12
_FULL_RESOURCE_VALUE = 100

_ENEMY_BEHAVIOR_TEMPLATE_BY_TEMPLATE_ID: dict[str, str] = {
    "berserker": "wenxin_sword",
    "guardian": "manhuang_body",
    "swift": "zhanqing_sword",
    "caster": "wangchuan_spell",
    "restorer": "changsheng_body",
}

_ENEMY_COUNT_ROOT_BY_VALUE: dict[int, Decimal] = {
    1: Decimal("1.0000"),
    2: Decimal("1.4142"),
    3: Decimal("1.7321"),
}

_DECIMAL_ONE = Decimal("1")

_PATH_COMBAT_PROFILE_BY_TEMPLATE_ID: dict[str, dict[str, Decimal | int]] = {
    "wenxin_sword": {
        "hp_factor": Decimal("0.90"),
        "attack_factor": Decimal("1.18"),
        "guard_factor": Decimal("0.84"),
        "speed_factor": Decimal("1.10"),
        "crit_rate_permille": 160,
        "crit_damage_bonus_permille": 450,
        "hit_rate_permille": 1000,
        "dodge_rate_permille": 40,
        "control_bonus_permille": 0,
        "control_resist_permille": 0,
        "healing_power_permille": 0,
        "shield_power_permille": 0,
        "damage_bonus_permille": 140,
        "damage_reduction_permille": 0,
        "counter_rate_permille": 0,
    },
    "zhanqing_sword": {
        "hp_factor": Decimal("0.95"),
        "attack_factor": Decimal("1.10"),
        "guard_factor": Decimal("0.92"),
        "speed_factor": Decimal("1.16"),
        "crit_rate_permille": 110,
        "crit_damage_bonus_permille": 260,
        "hit_rate_permille": 1000,
        "dodge_rate_permille": 70,
        "control_bonus_permille": 0,
        "control_resist_permille": 0,
        "healing_power_permille": 0,
        "shield_power_permille": 0,
        "damage_bonus_permille": 90,
        "damage_reduction_permille": 0,
        "counter_rate_permille": 0,
    },
    "manhuang_body": {
        "hp_factor": Decimal("1.28"),
        "attack_factor": Decimal("1.00"),
        "guard_factor": Decimal("1.34"),
        "speed_factor": Decimal("0.88"),
        "crit_rate_permille": 0,
        "crit_damage_bonus_permille": 0,
        "hit_rate_permille": 1000,
        "dodge_rate_permille": 0,
        "control_bonus_permille": 0,
        "control_resist_permille": 0,
        "healing_power_permille": 0,
        "shield_power_permille": 80,
        "damage_bonus_permille": 0,
        "damage_reduction_permille": 120,
        "counter_rate_permille": 280,
    },
    "changsheng_body": {
        "hp_factor": Decimal("1.20"),
        "attack_factor": Decimal("0.92"),
        "guard_factor": Decimal("1.18"),
        "speed_factor": Decimal("0.90"),
        "crit_rate_permille": 0,
        "crit_damage_bonus_permille": 0,
        "hit_rate_permille": 1000,
        "dodge_rate_permille": 0,
        "control_bonus_permille": 0,
        "control_resist_permille": 80,
        "healing_power_permille": 220,
        "shield_power_permille": 240,
        "damage_bonus_permille": 0,
        "damage_reduction_permille": 90,
        "counter_rate_permille": 0,
    },
    "qingyun_spell": {
        "hp_factor": Decimal("0.88"),
        "attack_factor": Decimal("1.16"),
        "guard_factor": Decimal("0.82"),
        "speed_factor": Decimal("1.00"),
        "crit_rate_permille": 60,
        "crit_damage_bonus_permille": 180,
        "hit_rate_permille": 1000,
        "dodge_rate_permille": 0,
        "control_bonus_permille": 40,
        "control_resist_permille": 0,
        "healing_power_permille": 0,
        "shield_power_permille": 0,
        "damage_bonus_permille": 130,
        "damage_reduction_permille": 0,
        "counter_rate_permille": 0,
    },
    "wangchuan_spell": {
        "hp_factor": Decimal("0.90"),
        "attack_factor": Decimal("1.06"),
        "guard_factor": Decimal("0.86"),
        "speed_factor": Decimal("1.02"),
        "crit_rate_permille": 40,
        "crit_damage_bonus_permille": 120,
        "hit_rate_permille": 1000,
        "dodge_rate_permille": 0,
        "control_bonus_permille": 180,
        "control_resist_permille": 0,
        "healing_power_permille": 0,
        "shield_power_permille": 0,
        "damage_bonus_permille": 70,
        "damage_reduction_permille": 0,
        "counter_rate_permille": 0,
    },
}

_ENEMY_TEMPLATE_STAT_PROFILE: dict[str, dict[str, Decimal | int]] = {
    "berserker": {
        "hp_factor": Decimal("0.88"),
        "attack_factor": Decimal("1.22"),
        "guard_factor": Decimal("0.76"),
        "speed_factor": Decimal("1.04"),
        "crit_rate_permille": 120,
        "crit_damage_bonus_permille": 320,
        "hit_rate_permille": 1000,
        "dodge_rate_permille": 20,
        "control_bonus_permille": 0,
        "control_resist_permille": 0,
        "healing_power_permille": 0,
        "shield_power_permille": 0,
        "damage_bonus_permille": 110,
        "damage_reduction_permille": 0,
        "counter_rate_permille": 0,
    },
    "guardian": {
        "hp_factor": Decimal("1.24"),
        "attack_factor": Decimal("0.90"),
        "guard_factor": Decimal("1.28"),
        "speed_factor": Decimal("0.84"),
        "crit_rate_permille": 0,
        "crit_damage_bonus_permille": 0,
        "hit_rate_permille": 1000,
        "dodge_rate_permille": 0,
        "control_bonus_permille": 0,
        "control_resist_permille": 80,
        "healing_power_permille": 0,
        "shield_power_permille": 140,
        "damage_bonus_permille": 0,
        "damage_reduction_permille": 120,
        "counter_rate_permille": 120,
    },
    "swift": {
        "hp_factor": Decimal("0.82"),
        "attack_factor": Decimal("1.00"),
        "guard_factor": Decimal("0.78"),
        "speed_factor": Decimal("1.24"),
        "crit_rate_permille": 80,
        "crit_damage_bonus_permille": 160,
        "hit_rate_permille": 1000,
        "dodge_rate_permille": 180,
        "control_bonus_permille": 0,
        "control_resist_permille": 0,
        "healing_power_permille": 0,
        "shield_power_permille": 0,
        "damage_bonus_permille": 40,
        "damage_reduction_permille": 0,
        "counter_rate_permille": 0,
    },
    "caster": {
        "hp_factor": Decimal("0.84"),
        "attack_factor": Decimal("1.10"),
        "guard_factor": Decimal("0.80"),
        "speed_factor": Decimal("0.98"),
        "crit_rate_permille": 50,
        "crit_damage_bonus_permille": 120,
        "hit_rate_permille": 1000,
        "dodge_rate_permille": 0,
        "control_bonus_permille": 160,
        "control_resist_permille": 0,
        "healing_power_permille": 0,
        "shield_power_permille": 0,
        "damage_bonus_permille": 80,
        "damage_reduction_permille": 0,
        "counter_rate_permille": 0,
    },
    "restorer": {
        "hp_factor": Decimal("1.02"),
        "attack_factor": Decimal("0.86"),
        "guard_factor": Decimal("1.02"),
        "speed_factor": Decimal("0.92"),
        "crit_rate_permille": 0,
        "crit_damage_bonus_permille": 0,
        "hit_rate_permille": 1000,
        "dodge_rate_permille": 0,
        "control_bonus_permille": 0,
        "control_resist_permille": 40,
        "healing_power_permille": 200,
        "shield_power_permille": 160,
        "damage_bonus_permille": 0,
        "damage_reduction_permille": 40,
        "counter_rate_permille": 0,
    },
}


@dataclass(frozen=True, slots=True)
class EndlessRunRewardLedgerSnapshot:
    """无尽副本过程收益账本快照。"""

    stable_cultivation: int
    stable_insight: int
    stable_refining_essence: int
    pending_drop_progress: int
    drop_count: int
    last_reward_floor: int | None
    drop_display: tuple[dict[str, Any], ...]
    latest_node_result: dict[str, Any] | None
    advanced_floor_count: int
    latest_anchor_unlock: dict[str, Any] | None
    encounter_history: tuple[dict[str, Any], ...]

    @property
    def pending_equipment_score(self) -> int:
        """兼容旧字段：统一掉落进度语义下恒为 0。"""
        return 0

    @property
    def pending_artifact_score(self) -> int:
        """兼容旧字段：统一掉落进度语义下恒为 0。"""
        return 0

    @property
    def pending_dao_pattern_score(self) -> int:
        """兼容旧字段：统一掉落进度语义下恒为 0。"""
        return 0


@dataclass(frozen=True, slots=True)
class EndlessRunAnchorSnapshot:
    """无尽副本锚点与起点状态快照。"""

    highest_unlocked_anchor_floor: int
    available_start_floors: tuple[int, ...]
    selected_start_floor: int | None
    selected_start_floor_unlocked: bool
    current_anchor_floor: int | None
    next_anchor_floor: int | None


@dataclass(frozen=True, slots=True)
class EndlessRunStatusSnapshot:
    """无尽副本当前运行状态快照。"""

    character_id: int
    has_active_run: bool
    status: str | None
    selected_start_floor: int | None
    current_floor: int | None
    highest_floor_reached: int | None
    current_node_type: EndlessNodeType | None
    current_region: EndlessRegionSnapshot | None
    anchor_status: EndlessRunAnchorSnapshot
    run_seed: int | None
    reward_ledger: EndlessRunRewardLedgerSnapshot | None
    encounter_history: tuple[dict[str, Any], ...]
    started_at: datetime | None


@dataclass(frozen=True, slots=True)
class EndlessFloorAdvanceResult:
    """无尽副本自动推进结果。"""

    character_id: int
    cleared_floor: int
    next_floor: int | None
    encounter: dict[str, Any]
    battle_outcome: str
    battle_report_id: int | None
    reward_granted: bool
    anchor_unlock_result: dict[str, Any] | None
    latest_node_result: dict[str, Any]
    advanced_results: tuple[dict[str, Any], ...]
    stopped_floor: int
    stopped_reason: str
    decision_floor: int | None
    run_status: EndlessRunStatusSnapshot


@dataclass(frozen=True, slots=True)
class EndlessSettlementRewardSection:
    """结算面板中的单类收益对比。"""

    original: dict[str, int]
    deducted: dict[str, int]
    settled: dict[str, int]


@dataclass(frozen=True, slots=True)
class EndlessRunSettlementResult:
    """无尽副本终结结算结果。"""

    character_id: int
    settlement_type: str
    terminated_floor: int
    current_region: EndlessRegionSnapshot
    stable_rewards: EndlessSettlementRewardSection
    pending_rewards: EndlessSettlementRewardSection
    final_drop_list: tuple[dict[str, Any], ...]
    accounting_completed: bool
    can_repeat_read: bool
    settled_at: datetime


class EndlessDungeonServiceError(RuntimeError):
    """无尽副本应用服务基础异常。"""


class EndlessRunAlreadyRunningError(EndlessDungeonServiceError):
    """角色已经存在进行中的无尽副本运行。"""


class EndlessRunNotFoundError(EndlessDungeonServiceError):
    """角色不存在可恢复的无尽副本运行。"""


class InvalidEndlessStartFloorError(EndlessDungeonServiceError):
    """无尽副本起点层数非法。"""


class EndlessRunStateError(EndlessDungeonServiceError):
    """无尽副本运行态数据不完整或不合法。"""


class EndlessDungeonService:
    """负责编排无尽副本运行态开始、恢复、推进与读取。"""

    def __init__(
        self,
        *,
        state_repository: StateRepository,
        character_repository: CharacterRepository,
        static_config: StaticGameConfig | None = None,
        progression: EndlessDungeonProgression | None = None,
        encounter_generator: EndlessEncounterGenerator | None = None,
        auto_battle_service: AutoBattleService | None = None,
        battle_record_repository: BattleRecordRepository | None = None,
        current_attribute_service: CurrentAttributeService | None = None,
        skill_drop_service: SkillDropService | None = None,
        equipment_service: EquipmentService | None = None,
        naming_batch_service: ItemNamingBatchService | None = None,
    ) -> None:
        self._state_repository = state_repository
        self._character_repository = character_repository
        self._static_config = static_config or get_static_config()
        self._progression = progression or EndlessDungeonProgression(self._static_config)
        self._encounter_generator = encounter_generator or EndlessEncounterGenerator(self._static_config)
        self._auto_battle_service = auto_battle_service
        self._battle_record_repository = battle_record_repository or getattr(
            auto_battle_service,
            "_battle_record_repository",
            None,
        )
        self._current_attribute_service = current_attribute_service or CurrentAttributeService(
            character_repository=character_repository,
        )
        self._skill_drop_service = skill_drop_service or SkillDropService(
            character_repository=character_repository,
        )
        self._equipment_service = equipment_service
        self._naming_batch_service = naming_batch_service
        self._growth_progression = CharacterGrowthProgression(self._static_config)
        self._drop_orchestrator = None if self._equipment_service is None else EndlessSettlementDropOrchestrator(
            equipment_service=self._equipment_service,
            skill_drop_service=self._skill_drop_service,
            static_config=self._static_config,
        )
        self._realm_coefficient_by_realm_id = {
            entry.realm_id: Decimal(entry.coefficient)
            for entry in self._static_config.base_coefficients.realm_curve.entries
        }
        self._stage_multiplier_by_stage_id = {
            stage.stage_id: Decimal(stage.multiplier)
            for stage in self._static_config.realm_progression.stages
        }
        self._enemy_race_name_by_id = {
            race.race_id: race.name
            for race in self._static_config.enemies.races
        }

    def start_run(
        self,
        *,
        character_id: int,
        selected_start_floor: int = 1,
        seed: int | None = None,
        now: datetime | None = None,
    ) -> EndlessRunStatusSnapshot:
        """开始一条新的无尽副本运行。"""
        aggregate = self._require_character_aggregate(character_id)
        progress = self._require_progress(aggregate)
        current_time = now or datetime.utcnow()
        existing_run_state = self._state_repository.get_endless_run_state(character_id)

        if existing_run_state is not None and existing_run_state.status != _ENDLESS_STATUS_COMPLETED:
            raise EndlessRunAlreadyRunningError(
                f"角色已存在未完成的无尽副本运行：{character_id}，当前状态为 {existing_run_state.status}"
            )

        highest_unlocked_anchor_floor = self._resolve_highest_unlocked_anchor_floor(progress)
        available_start_floors = self._progression.get_available_start_floors(highest_unlocked_anchor_floor)
        if selected_start_floor not in available_start_floors:
            raise InvalidEndlessStartFloorError(
                f"无效的无尽副本起点层数：{selected_start_floor}，可选起点为 {available_start_floors}"
            )

        current_floor = self._resolve_entry_floor(selected_start_floor)
        floor_snapshot = self._resolve_floor(
            current_floor=current_floor,
            highest_unlocked_anchor_floor=highest_unlocked_anchor_floor,
        )
        run_seed = self._resolve_run_seed(now=current_time, seed=seed)
        record_floor_before_run = progress.highest_endless_floor
        reward_ledger_payload = self._build_empty_reward_ledger_payload()
        run_snapshot_payload = self._build_run_snapshot_payload(
            status=_ENDLESS_STATUS_RUNNING,
            selected_start_floor=selected_start_floor,
            current_floor=current_floor,
            current_node_type=floor_snapshot.node_type,
            current_region=floor_snapshot.region,
            highest_unlocked_anchor_floor=highest_unlocked_anchor_floor,
            available_start_floors=available_start_floors,
            current_anchor_floor=floor_snapshot.anchor_floor,
            next_anchor_floor=floor_snapshot.next_anchor_floor,
            run_seed=run_seed,
            encounter_history=reward_ledger_payload["encounter_history"],
            record_floor_before_run=record_floor_before_run,
        )

        if existing_run_state is None:
            endless_run_state = EndlessRunState(
                character_id=character_id,
                status=_ENDLESS_STATUS_RUNNING,
                selected_start_floor=selected_start_floor,
                current_floor=current_floor,
                highest_floor_reached=current_floor,
                current_node_type=floor_snapshot.node_type.value,
                last_region_bias_id=floor_snapshot.region.region_bias_id,
                last_enemy_template_id=None,
                run_seed=run_seed,
                started_at=current_time,
                pending_rewards_json=reward_ledger_payload,
                run_snapshot_json=run_snapshot_payload,
            )
        else:
            endless_run_state = existing_run_state
            endless_run_state.status = _ENDLESS_STATUS_RUNNING
            endless_run_state.selected_start_floor = selected_start_floor
            endless_run_state.current_floor = current_floor
            endless_run_state.highest_floor_reached = current_floor
            endless_run_state.current_node_type = floor_snapshot.node_type.value
            endless_run_state.last_region_bias_id = floor_snapshot.region.region_bias_id
            endless_run_state.last_enemy_template_id = None
            endless_run_state.run_seed = run_seed
            endless_run_state.started_at = current_time
            endless_run_state.pending_rewards_json = reward_ledger_payload
            endless_run_state.run_snapshot_json = run_snapshot_payload

        self._state_repository.save_endless_run_state(endless_run_state)
        return self._build_status_snapshot(
            character_id=character_id,
            progress=progress,
            endless_run_state=endless_run_state,
        )

    def resume_run(self, *, character_id: int) -> EndlessRunStatusSnapshot:
        """恢复一条已存在的无尽副本运行。"""
        snapshot = self.get_current_run_state(character_id=character_id)
        if not snapshot.has_active_run:
            raise EndlessRunNotFoundError(f"角色不存在可恢复的无尽副本运行：{character_id}")
        return snapshot

    def advance_next_floor(
        self,
        *,
        character_id: int,
        persist_battle_report: bool = True,
    ) -> EndlessFloorAdvanceResult:
        """自动推进当前运行态，直到抵达决策点或战败。"""
        aggregate = self._require_character_aggregate(character_id)
        progress = self._require_progress(aggregate)
        endless_run_state = self._require_advancable_run_state(character_id)
        if self._auto_battle_service is None:
            raise EndlessRunStateError("无尽副本推进缺少自动战斗服务")

        reward_payload = self._normalize_reward_ledger_payload(endless_run_state.pending_rewards_json)
        existing_run_snapshot_payload = _normalize_optional_mapping(endless_run_state.run_snapshot_json) or {}
        record_floor_before_run = _read_int(
            existing_run_snapshot_payload.get("record_floor_before_run"),
            default=progress.highest_endless_floor,
        )
        advanced_results: list[dict[str, Any]] = []
        final_anchor_unlock_result: dict[str, Any] | None = None
        final_latest_node_result: dict[str, Any] | None = None
        stopped_reason = "decision"
        decision_floor: int | None = None
        next_floor: int | None = None

        while True:
            highest_unlocked_anchor_floor = self._resolve_highest_unlocked_anchor_floor(progress)
            floor_snapshot = self._resolve_floor(
                current_floor=endless_run_state.current_floor,
                highest_unlocked_anchor_floor=highest_unlocked_anchor_floor,
            )
            encounter = self._encounter_generator.generate(
                floor=floor_snapshot.floor,
                seed=endless_run_state.run_seed,
            )
            request = self._build_auto_battle_request(
                aggregate=aggregate,
                progress=progress,
                floor_snapshot=floor_snapshot,
                encounter=encounter,
                run_seed=endless_run_state.run_seed,
            )
            execution_result = self._auto_battle_service.execute(
                request=request,
                persist=persist_battle_report,
            )
            self._apply_progress_writeback(
                progress=progress,
                current_hp_ratio=execution_result.persistence_mapping.progress_writeback.current_hp_ratio,
                current_mp_ratio=execution_result.persistence_mapping.progress_writeback.current_mp_ratio,
            )
            battle_outcome = self._extract_battle_outcome(execution_result)
            reward_granted = battle_outcome is BattleOutcome.ALLY_VICTORY
            reward_breakdown = None

            previous_highest_unlocked_anchor_floor = highest_unlocked_anchor_floor
            new_highest_unlocked_anchor_floor = highest_unlocked_anchor_floor
            if reward_granted:
                reward_breakdown = self._progression.build_reward_breakdown(
                    floor_snapshot.floor,
                    realm_id=progress.realm_id,
                )
                progress.highest_endless_floor = max(progress.highest_endless_floor, floor_snapshot.floor)
                new_highest_unlocked_anchor_floor = self._resolve_highest_unlocked_anchor_floor(progress)

            enemy_units = self._build_enemy_unit_payloads(request.snapshot.enemies)
            battle_summary = execution_result.report_artifacts.summary.to_payload()
            anchor_unlock_result = self._build_anchor_unlock_result_payload(
                cleared_floor=floor_snapshot.floor,
                previous_highest_unlocked_anchor_floor=previous_highest_unlocked_anchor_floor,
                new_highest_unlocked_anchor_floor=new_highest_unlocked_anchor_floor,
            )
            latest_node_result = self._build_latest_node_result_payload(
                floor_snapshot=floor_snapshot,
                encounter=encounter,
                battle_outcome=battle_outcome,
                battle_report_id=execution_result.persisted_battle_report_id,
                reward_breakdown=reward_breakdown,
                reward_granted=reward_granted,
                current_hp_ratio=progress.current_hp_ratio,
                current_mp_ratio=progress.current_mp_ratio,
                enemy_units=enemy_units,
                battle_summary=battle_summary,
            )
            encounter_history_entry = self._build_encounter_history_entry(node_result=latest_node_result)
            self._merge_reward_ledger_payload(
                payload=reward_payload,
                reward_breakdown=reward_breakdown,
                encounter_history_entry=encounter_history_entry,
                latest_node_result=latest_node_result,
                anchor_unlock_result=anchor_unlock_result,
                encounter=encounter,
            )
            advanced_results.append(dict(latest_node_result))
            final_anchor_unlock_result = anchor_unlock_result
            final_latest_node_result = latest_node_result
            endless_run_state.last_region_bias_id = encounter.region_bias_id
            endless_run_state.last_enemy_template_id = encounter.template_id

            if not reward_granted:
                endless_run_state.status = _ENDLESS_STATUS_PENDING_DEFEAT_SETTLEMENT
                endless_run_state.current_floor = floor_snapshot.floor
                endless_run_state.highest_floor_reached = max(
                    _read_int(endless_run_state.highest_floor_reached),
                    floor_snapshot.floor,
                )
                endless_run_state.current_node_type = floor_snapshot.node_type.value
                endless_run_state.pending_rewards_json = reward_payload
                endless_run_state.run_snapshot_json = self._build_run_snapshot_payload(
                    status=_ENDLESS_STATUS_PENDING_DEFEAT_SETTLEMENT,
                    selected_start_floor=endless_run_state.selected_start_floor,
                    current_floor=floor_snapshot.floor,
                    current_node_type=floor_snapshot.node_type,
                    current_region=floor_snapshot.region,
                    highest_unlocked_anchor_floor=new_highest_unlocked_anchor_floor,
                    available_start_floors=self._progression.get_available_start_floors(new_highest_unlocked_anchor_floor),
                    current_anchor_floor=floor_snapshot.anchor_floor,
                    next_anchor_floor=floor_snapshot.next_anchor_floor,
                    run_seed=endless_run_state.run_seed,
                    encounter_history=reward_payload["encounter_history"],
                    record_floor_before_run=record_floor_before_run,
                )
                stopped_reason = "defeat"
                next_floor = None
                break

            next_floor = floor_snapshot.floor + 1
            endless_run_state.highest_floor_reached = max(_read_int(endless_run_state.highest_floor_reached), next_floor)
            if self._is_decision_floor(floor_snapshot.floor):
                next_floor_snapshot = self._resolve_floor(
                    current_floor=next_floor,
                    highest_unlocked_anchor_floor=new_highest_unlocked_anchor_floor,
                )
                endless_run_state.status = _ENDLESS_STATUS_RUNNING
                endless_run_state.current_floor = next_floor
                endless_run_state.current_node_type = next_floor_snapshot.node_type.value
                endless_run_state.pending_rewards_json = reward_payload
                endless_run_state.run_snapshot_json = self._build_run_snapshot_payload(
                    status=_ENDLESS_STATUS_RUNNING,
                    selected_start_floor=endless_run_state.selected_start_floor,
                    current_floor=next_floor,
                    current_node_type=next_floor_snapshot.node_type,
                    current_region=next_floor_snapshot.region,
                    highest_unlocked_anchor_floor=new_highest_unlocked_anchor_floor,
                    available_start_floors=self._progression.get_available_start_floors(new_highest_unlocked_anchor_floor),
                    current_anchor_floor=next_floor_snapshot.anchor_floor,
                    next_anchor_floor=next_floor_snapshot.next_anchor_floor,
                    run_seed=endless_run_state.run_seed,
                    encounter_history=reward_payload["encounter_history"],
                    record_floor_before_run=record_floor_before_run,
                )
                stopped_reason = "decision"
                decision_floor = floor_snapshot.floor
                break

            endless_run_state.current_floor = next_floor

        self._character_repository.save_progress(progress)
        self._state_repository.save_endless_run_state(endless_run_state)
        status_snapshot = self._build_status_snapshot(
            character_id=character_id,
            progress=progress,
            endless_run_state=endless_run_state,
        )
        if not advanced_results or final_latest_node_result is None:
            raise EndlessRunStateError("无尽副本自动推进结果为空")
        last_result = advanced_results[-1]
        return EndlessFloorAdvanceResult(
            character_id=character_id,
            cleared_floor=_read_int(last_result.get("floor"), default=endless_run_state.current_floor),
            next_floor=next_floor,
            encounter=dict(_normalize_optional_mapping(last_result.get("encounter")) or {}),
            battle_outcome=str(last_result.get("battle_outcome") or ""),
            battle_report_id=_read_optional_int(last_result.get("battle_report_id")),
            reward_granted=bool(last_result.get("reward_granted")),
            anchor_unlock_result=final_anchor_unlock_result,
            latest_node_result=final_latest_node_result,
            advanced_results=tuple(advanced_results),
            stopped_floor=_read_int(last_result.get("floor"), default=endless_run_state.current_floor),
            stopped_reason=stopped_reason,
            decision_floor=decision_floor,
            run_status=status_snapshot,
        )

    def settle_retreat(
        self,
        *,
        character_id: int,
        now: datetime | None = None,
    ) -> EndlessRunSettlementResult:
        """主动撤离并完成终结结算。"""
        aggregate = self._require_character_aggregate(character_id)
        progress = self._require_progress(aggregate)
        endless_run_state = self._require_existing_run_state(character_id)
        persisted_result = self._load_persisted_settlement_result(endless_run_state)
        if endless_run_state.status == _ENDLESS_STATUS_COMPLETED:
            if persisted_result is None:
                raise EndlessRunStateError(f"无尽副本已结束但缺少结算结果：{character_id}")
            return self._refresh_settlement_result(character_id=character_id, result=persisted_result)
        if endless_run_state.status != _ENDLESS_STATUS_RUNNING:
            raise EndlessRunStateError(f"当前无尽副本运行不可撤离结算：{character_id}，状态为 {endless_run_state.status}")
        self._require_retreatable_run_state(endless_run_state)
        return self._settle_run(
            character_id=character_id,
            progress=progress,
            endless_run_state=endless_run_state,
            settlement_type=_ENDLESS_SETTLEMENT_RETREAT,
            now=now or datetime.utcnow(),
        )

    def settle_defeat(
        self,
        *,
        character_id: int,
        now: datetime | None = None,
    ) -> EndlessRunSettlementResult:
        """完成待战败状态的终结结算。"""
        aggregate = self._require_character_aggregate(character_id)
        progress = self._require_progress(aggregate)
        endless_run_state = self._require_existing_run_state(character_id)
        persisted_result = self._load_persisted_settlement_result(endless_run_state)
        if endless_run_state.status == _ENDLESS_STATUS_COMPLETED:
            if persisted_result is None:
                raise EndlessRunStateError(f"无尽副本已结束但缺少结算结果：{character_id}")
            return self._refresh_settlement_result(character_id=character_id, result=persisted_result)
        if endless_run_state.status != _ENDLESS_STATUS_PENDING_DEFEAT_SETTLEMENT:
            raise EndlessRunStateError(f"当前无尽副本运行不可战败结算：{character_id}，状态为 {endless_run_state.status}")
        return self._settle_run(
            character_id=character_id,
            progress=progress,
            endless_run_state=endless_run_state,
            settlement_type=_ENDLESS_SETTLEMENT_DEFEAT,
            now=now or datetime.utcnow(),
        )

    def get_settlement_result(self, *, character_id: int) -> EndlessRunSettlementResult:
        """读取最近一次终结结算面板结果。"""
        aggregate = self._require_character_aggregate(character_id)
        self._require_progress(aggregate)
        endless_run_state = self._require_existing_run_state(character_id)
        persisted_result = self._load_persisted_settlement_result(endless_run_state)
        if endless_run_state.status != _ENDLESS_STATUS_COMPLETED or persisted_result is None:
            raise EndlessRunStateError(f"角色当前不存在可读取的无尽副本结算结果：{character_id}")
        return self._refresh_settlement_result(character_id=character_id, result=persisted_result)

    def get_current_run_state(self, *, character_id: int) -> EndlessRunStatusSnapshot:
        """读取角色当前无尽副本运行状态。"""
        aggregate = self._require_character_aggregate(character_id)
        progress = self._require_progress(aggregate)
        endless_run_state = self._state_repository.get_endless_run_state(character_id)
        if endless_run_state is None or endless_run_state.status == _ENDLESS_STATUS_COMPLETED:
            return self._build_empty_status_snapshot(character_id=character_id, progress=progress)
        return self._build_status_snapshot(
            character_id=character_id,
            progress=progress,
            endless_run_state=endless_run_state,
        )

    def _settle_run(
        self,
        *,
        character_id: int,
        progress: CharacterProgress,
        endless_run_state: EndlessRunState,
        settlement_type: str,
        now: datetime,
    ) -> EndlessRunSettlementResult:
        if self._battle_record_repository is None:
            raise EndlessRunStateError("无尽副本结算缺少掉落记录仓储")
        reward_ledger = self._parse_reward_ledger(endless_run_state.pending_rewards_json)
        raw_rewards = self._reward_breakdown_from_ledger(reward_ledger)
        if settlement_type == _ENDLESS_SETTLEMENT_RETREAT:
            settled_rewards = self._progression.settle_retreat_rewards(raw_rewards)
        else:
            settled_rewards = self._progression.settle_failure_pending_rewards(raw_rewards)
        deducted_rewards = self._subtract_reward_breakdown(original=raw_rewards, settled=settled_rewards)
        terminated_floor = self._resolve_terminated_floor(
            endless_run_state=endless_run_state,
            reward_ledger=reward_ledger,
            settlement_type=settlement_type,
        )
        floor_snapshot = self._resolve_floor(
            current_floor=terminated_floor,
            highest_unlocked_anchor_floor=self._resolve_highest_unlocked_anchor_floor(progress),
        )
        applied_rewards = self._apply_settlement_progress_writeback(
            progress=progress,
            reward_ledger=reward_ledger,
            settled_rewards=settled_rewards,
        )
        self._character_repository.save_progress(progress)
        source_ref = f"endless:{settlement_type}:floor_{terminated_floor}"
        final_drop_entries = self._generate_settlement_drop_entries(
            character_id=character_id,
            progress=progress,
            terminated_floor=terminated_floor,
            settled_rewards=applied_rewards,
            run_seed=endless_run_state.run_seed,
            source_ref=source_ref,
        )
        result = self._build_settlement_result(
            character_id=character_id,
            settlement_type=settlement_type,
            terminated_floor=terminated_floor,
            current_region=floor_snapshot.region,
            raw_rewards=raw_rewards,
            deducted_rewards=deducted_rewards,
            settled_rewards=applied_rewards,
            settled_at=now,
            instantiated_drop_entries=final_drop_entries,
        )
        self._create_naming_batch_if_configured(
            character_id=character_id,
            source_ref=source_ref,
            final_drop_list=result.final_drop_list,
        )
        result = self._refresh_settlement_result(character_id=character_id, result=result)
        self._battle_record_repository.add_drop_record(
            DropRecord(
                character_id=character_id,
                battle_report_id=self._extract_latest_battle_report_id(reward_ledger),
                source_type=_ENDLESS_BATTLE_TYPE,
                source_ref=source_ref,
                items_json=[dict(item) for item in result.final_drop_list],
                currencies_json=dict(result.stable_rewards.settled),
            )
        )
        endless_run_state.status = _ENDLESS_STATUS_COMPLETED
        endless_run_state.pending_rewards_json = self._build_empty_reward_ledger_payload()
        endless_run_state.run_snapshot_json = self._build_completed_run_snapshot_payload(
            result=result,
            prior_run_snapshot=endless_run_state.run_snapshot_json,
            reward_ledger=reward_ledger,
        )
        self._state_repository.save_endless_run_state(endless_run_state)
        return result

    def _refresh_settlement_result(
        self,
        *,
        character_id: int,
        result: EndlessRunSettlementResult,
    ) -> EndlessRunSettlementResult:
        if self._naming_batch_service is None:
            return result
        try:
            refreshed_drop_list = self._naming_batch_service.refresh_drop_entries(
                character_id=character_id,
                entries=result.final_drop_list,
            )
        except Exception:  # noqa: BLE001
            return result
        return replace(result, final_drop_list=refreshed_drop_list)

    def _create_naming_batch_if_configured(
        self,
        *,
        character_id: int,
        source_ref: str,
        final_drop_list: tuple[dict[str, Any], ...],
    ) -> None:
        if self._naming_batch_service is None:
            return
        try:
            batch = self._naming_batch_service.create_endless_settlement_batch(
                character_id=character_id,
                source_ref=source_ref,
                final_drop_list=final_drop_list,
            )
            if batch is None:
                return
            self._naming_batch_service.process_batch(batch_id=batch.id)
        except Exception:  # noqa: BLE001
            return

    def _build_settlement_result(
        self,
        *,
        character_id: int,
        settlement_type: str,
        terminated_floor: int,
        current_region: EndlessRegionSnapshot,
        raw_rewards: EndlessRewardBreakdown,
        deducted_rewards: EndlessRewardBreakdown,
        settled_rewards: EndlessRewardBreakdown,
        settled_at: datetime,
        instantiated_drop_entries: tuple[dict[str, Any], ...] = (),
    ) -> EndlessRunSettlementResult:
        stable_rewards = EndlessSettlementRewardSection(
            original=raw_rewards.to_stable_payload(),
            deducted=deducted_rewards.to_stable_payload(),
            settled=settled_rewards.to_stable_payload(),
        )
        pending_rewards = EndlessSettlementRewardSection(
            original=raw_rewards.to_pending_payload(),
            deducted=deducted_rewards.to_pending_payload(),
            settled=settled_rewards.to_pending_payload(),
        )
        return EndlessRunSettlementResult(
            character_id=character_id,
            settlement_type=settlement_type,
            terminated_floor=terminated_floor,
            current_region=current_region,
            stable_rewards=stable_rewards,
            pending_rewards=pending_rewards,
            final_drop_list=self._build_final_drop_list(
                settlement_type=settlement_type,
                stable_rewards=stable_rewards,
                pending_rewards=pending_rewards,
                instantiated_drop_entries=instantiated_drop_entries,
            ),
            accounting_completed=True,
            can_repeat_read=True,
            settled_at=settled_at,
        )

    @staticmethod
    def _build_completed_run_snapshot_payload(
        *,
        result: EndlessRunSettlementResult,
        prior_run_snapshot: Mapping[str, Any] | None,
        reward_ledger: EndlessRunRewardLedgerSnapshot,
    ) -> dict[str, Any]:
        normalized_run_snapshot = _normalize_optional_mapping(prior_run_snapshot) or {}
        return {
            "has_active_run": False,
            "status": _ENDLESS_STATUS_COMPLETED,
            "selected_start_floor": _read_optional_int(normalized_run_snapshot.get("selected_start_floor")),
            "record_floor_before_run": _read_int(normalized_run_snapshot.get("record_floor_before_run")),
            "advanced_floor_count": reward_ledger.advanced_floor_count,
            "latest_anchor_unlock": None
            if reward_ledger.latest_anchor_unlock is None
            else dict(reward_ledger.latest_anchor_unlock),
            "latest_node_result": None if reward_ledger.latest_node_result is None else dict(reward_ledger.latest_node_result),
            "settlement_result": EndlessDungeonService._serialize_settlement_result(result),
        }

    @staticmethod
    def _serialize_settlement_result(result: EndlessRunSettlementResult) -> dict[str, Any]:
        return {
            "character_id": result.character_id,
            "settlement_type": result.settlement_type,
            "terminated_floor": result.terminated_floor,
            "current_region": EndlessDungeonService._region_to_payload(result.current_region),
            "stable_rewards": EndlessDungeonService._serialize_reward_section(result.stable_rewards),
            "pending_rewards": EndlessDungeonService._serialize_reward_section(result.pending_rewards),
            "final_drop_list": [dict(item) for item in result.final_drop_list],
            "accounting_completed": result.accounting_completed,
            "can_repeat_read": result.can_repeat_read,
            "settled_at": result.settled_at.isoformat(),
        }

    @staticmethod
    def _serialize_reward_section(section: EndlessSettlementRewardSection) -> dict[str, dict[str, int]]:
        return {
            "original": dict(section.original),
            "deducted": dict(section.deducted),
            "settled": dict(section.settled),
        }

    def _load_persisted_settlement_result(self, endless_run_state: EndlessRunState) -> EndlessRunSettlementResult | None:
        run_snapshot_payload = _normalize_optional_mapping(endless_run_state.run_snapshot_json)
        if run_snapshot_payload is None:
            return None
        settlement_payload = _normalize_optional_mapping(run_snapshot_payload.get("settlement_result"))
        if settlement_payload is None:
            return None
        return self._parse_settlement_result(settlement_payload)

    def _parse_settlement_result(self, payload: Mapping[str, Any]) -> EndlessRunSettlementResult:
        return EndlessRunSettlementResult(
            character_id=_read_int(payload.get("character_id")),
            settlement_type=str(payload.get("settlement_type") or ""),
            terminated_floor=_read_int(payload.get("terminated_floor")),
            current_region=self._parse_region_snapshot(_normalize_optional_mapping(payload.get("current_region"))),
            stable_rewards=self._parse_reward_section(_normalize_optional_mapping(payload.get("stable_rewards"))),
            pending_rewards=self._parse_reward_section(_normalize_optional_mapping(payload.get("pending_rewards"))),
            final_drop_list=tuple(_normalize_mapping_list(payload.get("final_drop_list"))),
            accounting_completed=bool(payload.get("accounting_completed")),
            can_repeat_read=bool(payload.get("can_repeat_read")),
            settled_at=self._parse_datetime_value(payload.get("settled_at")),
        )

    @staticmethod
    def _parse_reward_section(payload: Mapping[str, Any] | None) -> EndlessSettlementRewardSection:
        if payload is None:
            raise EndlessRunStateError("无尽副本结算结果缺少收益区段")
        return EndlessSettlementRewardSection(
            original=_normalize_int_mapping(payload.get("original")),
            deducted=_normalize_int_mapping(payload.get("deducted")),
            settled=_normalize_int_mapping(payload.get("settled")),
        )

    @staticmethod
    def _parse_region_snapshot(payload: Mapping[str, Any] | None) -> EndlessRegionSnapshot:
        if payload is None:
            raise EndlessRunStateError("无尽副本结算结果缺少区域信息")
        return EndlessRegionSnapshot(
            region_index=_read_int(payload.get("region_index"), default=1),
            region_id=str(payload.get("region_id") or ""),
            region_name=str(payload.get("region_name") or ""),
            region_bias_id=str(payload.get("region_bias_id") or ""),
            start_floor=_read_int(payload.get("start_floor"), default=1),
            end_floor=_read_int(payload.get("end_floor"), default=1),
            theme_summary=str(payload.get("theme_summary") or ""),
        )

    @staticmethod
    def _parse_datetime_value(value: Any) -> datetime:
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value)
            except ValueError as exc:
                raise EndlessRunStateError(f"无效的结算时间：{value}") from exc
        raise EndlessRunStateError(f"无效的结算时间：{value}")

    @staticmethod
    def _build_final_drop_list(
        *,
        settlement_type: str,
        stable_rewards: EndlessSettlementRewardSection,
        pending_rewards: EndlessSettlementRewardSection,
        instantiated_drop_entries: tuple[dict[str, Any], ...] = (),
    ) -> tuple[dict[str, Any], ...]:
        drop_entries = [
            {
                "entry_type": "stable_reward_bundle",
                "settlement_type": settlement_type,
                "original": dict(stable_rewards.original),
                "deducted": dict(stable_rewards.deducted),
                "settled": dict(stable_rewards.settled),
            },
            {
                "entry_type": "pending_reward_bundle",
                "settlement_type": settlement_type,
                "original": dict(pending_rewards.original),
                "deducted": dict(pending_rewards.deducted),
                "settled": dict(pending_rewards.settled),
            },
        ]
        drop_entries.extend(dict(item) for item in instantiated_drop_entries)
        return tuple(drop_entries)

    def _generate_settlement_drop_entries(
        self,
        *,
        character_id: int,
        progress: CharacterProgress,
        terminated_floor: int,
        settled_rewards: EndlessRewardBreakdown,
        run_seed: int,
        source_ref: str,
    ) -> tuple[dict[str, Any], ...]:
        if settled_rewards.pending_drop_progress <= 0:
            return ()
        if self._drop_orchestrator is None:
            raise EndlessRunStateError("无尽副本结算缺少统一掉落编排服务")
        try:
            return self._drop_orchestrator.generate_settlement_drops(
                character_id=character_id,
                realm_id=progress.realm_id,
                pending_drop_progress=settled_rewards.pending_drop_progress,
                run_seed=run_seed,
                terminated_floor=terminated_floor,
                source_ref=source_ref,
            )
        except EndlessSettlementDropOrchestratorError as exc:
            raise EndlessRunStateError(str(exc)) from exc

    def _apply_settlement_progress_writeback(
        self,
        *,
        progress: CharacterProgress,
        reward_ledger: EndlessRunRewardLedgerSnapshot,
        settled_rewards: EndlessRewardBreakdown,
    ) -> EndlessRewardBreakdown:
        applied_cultivation = 0
        if settled_rewards.stable_cultivation > 0:
            realm_rule = self._growth_progression.get_realm_rule(progress.realm_id)
            target_cultivation = min(
                realm_rule.total_cultivation,
                progress.cultivation_value + settled_rewards.stable_cultivation,
            )
            applied_cultivation = target_cultivation - progress.cultivation_value
            progress.cultivation_value = target_cultivation
            progress.stage_id = self._growth_progression.resolve_stage(progress.realm_id, progress.cultivation_value).stage_id
        if settled_rewards.stable_insight > 0:
            progress.comprehension_value += settled_rewards.stable_insight
        progress.highest_endless_floor = max(progress.highest_endless_floor, reward_ledger.last_reward_floor or 0)
        return EndlessRewardBreakdown(
            stable_cultivation=applied_cultivation,
            stable_insight=settled_rewards.stable_insight,
            stable_refining_essence=settled_rewards.stable_refining_essence,
            pending_drop_progress=settled_rewards.pending_drop_progress,
        )

    @staticmethod
    def _reward_breakdown_from_ledger(reward_ledger: EndlessRunRewardLedgerSnapshot) -> EndlessRewardBreakdown:
        return EndlessRewardBreakdown(
            stable_cultivation=reward_ledger.stable_cultivation,
            stable_insight=reward_ledger.stable_insight,
            stable_refining_essence=reward_ledger.stable_refining_essence,
            pending_drop_progress=reward_ledger.pending_drop_progress,
        )

    @staticmethod
    def _subtract_reward_breakdown(
        *,
        original: EndlessRewardBreakdown,
        settled: EndlessRewardBreakdown,
    ) -> EndlessRewardBreakdown:
        return EndlessRewardBreakdown(
            stable_cultivation=max(0, original.stable_cultivation - settled.stable_cultivation),
            stable_insight=max(0, original.stable_insight - settled.stable_insight),
            stable_refining_essence=max(0, original.stable_refining_essence - settled.stable_refining_essence),
            pending_drop_progress=max(0, original.pending_drop_progress - settled.pending_drop_progress),
        )

    @staticmethod
    def _extract_latest_battle_report_id(reward_ledger: EndlessRunRewardLedgerSnapshot) -> int | None:
        if reward_ledger.latest_node_result is not None:
            battle_report_id = _read_optional_int(reward_ledger.latest_node_result.get("battle_report_id"))
            if battle_report_id is not None:
                return battle_report_id
        for encounter_history_entry in reversed(reward_ledger.encounter_history):
            battle_report_id = _read_optional_int(encounter_history_entry.get("battle_report_id"))
            if battle_report_id is not None:
                return battle_report_id
        return None

    @staticmethod
    def _resolve_terminated_floor(
        *,
        endless_run_state: EndlessRunState,
        reward_ledger: EndlessRunRewardLedgerSnapshot,
        settlement_type: str,
    ) -> int:
        if settlement_type == _ENDLESS_SETTLEMENT_DEFEAT:
            return max(1, _read_int(endless_run_state.current_floor, default=1))
        if reward_ledger.last_reward_floor is not None:
            return reward_ledger.last_reward_floor
        return max(1, _read_int(endless_run_state.selected_start_floor, default=1))

    def _require_existing_run_state(self, character_id: int) -> EndlessRunState:
        endless_run_state = self._state_repository.get_endless_run_state(character_id)
        if endless_run_state is None:
            raise EndlessRunNotFoundError(f"角色不存在可结算的无尽副本运行：{character_id}")
        return endless_run_state

    def _require_retreatable_run_state(self, endless_run_state: EndlessRunState) -> None:
        decision_floor = self._resolve_last_decision_floor(endless_run_state.pending_rewards_json)
        if decision_floor is None:
            raise EndlessRunStateError("当前无尽副本未停在可撤离的决策层")
        if _read_int(endless_run_state.current_floor, default=0) != decision_floor + 1:
            raise EndlessRunStateError(
                f"当前无尽副本未停在第 {decision_floor} 层决策点后的待选状态，无法撤离"
            )

    def _build_empty_status_snapshot(
        self,
        *,
        character_id: int,
        progress: CharacterProgress,
    ) -> EndlessRunStatusSnapshot:
        highest_unlocked_anchor_floor = self._resolve_highest_unlocked_anchor_floor(progress)
        available_start_floors = self._progression.get_available_start_floors(highest_unlocked_anchor_floor)
        return EndlessRunStatusSnapshot(
            character_id=character_id,
            has_active_run=False,
            status=None,
            selected_start_floor=None,
            current_floor=None,
            highest_floor_reached=None,
            current_node_type=None,
            current_region=None,
            anchor_status=EndlessRunAnchorSnapshot(
                highest_unlocked_anchor_floor=highest_unlocked_anchor_floor,
                available_start_floors=available_start_floors,
                selected_start_floor=None,
                selected_start_floor_unlocked=False,
                current_anchor_floor=None,
                next_anchor_floor=None,
            ),
            run_seed=None,
            reward_ledger=None,
            encounter_history=(),
            started_at=None,
        )

    def _build_status_snapshot(
        self,
        *,
        character_id: int,
        progress: CharacterProgress,
        endless_run_state: EndlessRunState,
    ) -> EndlessRunStatusSnapshot:
        highest_unlocked_anchor_floor = self._resolve_highest_unlocked_anchor_floor(progress)
        available_start_floors = self._progression.get_available_start_floors(highest_unlocked_anchor_floor)
        floor_snapshot = self._resolve_floor(
            current_floor=endless_run_state.current_floor,
            highest_unlocked_anchor_floor=highest_unlocked_anchor_floor,
        )
        current_node_type = self._parse_node_type(endless_run_state.current_node_type)
        reward_ledger = self._parse_reward_ledger(endless_run_state.pending_rewards_json)
        encounter_history = reward_ledger.encounter_history
        return EndlessRunStatusSnapshot(
            character_id=character_id,
            has_active_run=True,
            status=endless_run_state.status,
            selected_start_floor=endless_run_state.selected_start_floor,
            current_floor=endless_run_state.current_floor,
            highest_floor_reached=endless_run_state.highest_floor_reached,
            current_node_type=current_node_type,
            current_region=floor_snapshot.region,
            anchor_status=EndlessRunAnchorSnapshot(
                highest_unlocked_anchor_floor=highest_unlocked_anchor_floor,
                available_start_floors=available_start_floors,
                selected_start_floor=endless_run_state.selected_start_floor,
                selected_start_floor_unlocked=endless_run_state.selected_start_floor in available_start_floors,
                current_anchor_floor=floor_snapshot.anchor_floor,
                next_anchor_floor=floor_snapshot.next_anchor_floor,
            ),
            run_seed=endless_run_state.run_seed,
            reward_ledger=reward_ledger,
            encounter_history=encounter_history,
            started_at=endless_run_state.started_at,
        )

    def _build_auto_battle_request(
        self,
        *,
        aggregate: CharacterAggregate,
        progress: CharacterProgress,
        floor_snapshot,
        encounter: EndlessEnemyEncounter,
        run_seed: int,
    ) -> AutoBattleRequest:
        ally_snapshot = self._build_ally_battle_snapshot(aggregate=aggregate, progress=progress)
        enemy_snapshots = self._build_enemy_battle_snapshots(
            progress=progress,
            encounter=encounter,
        )
        environment_tags = (
            _ENDLESS_BATTLE_TYPE,
            f"floor_{floor_snapshot.floor}",
            f"node_{floor_snapshot.node_type.value}",
            f"region_{floor_snapshot.region.region_id}",
        )
        template_patches_by_template_id = self._build_ally_template_patches_by_template_id(
            character_id=aggregate.character.id,
        )
        template_path_id_by_template_id = self._build_ally_template_path_id_by_template_id(
            character_id=aggregate.character.id,
        )
        return AutoBattleRequest(
            character_id=aggregate.character.id,
            battle_type=_ENDLESS_BATTLE_TYPE,
            snapshot=BattleSnapshot(
                seed=self._compose_battle_seed(run_seed=run_seed, floor=floor_snapshot.floor),
                allies=(ally_snapshot,),
                enemies=enemy_snapshots,
                round_limit=_DEFAULT_ROUND_LIMIT,
                environment_tags=environment_tags,
            ),
            opponent_ref=(
                f"endless:{floor_snapshot.floor}:{encounter.region_id}:{encounter.race_id}:{encounter.template_id}"
            ),
            focus_unit_id=ally_snapshot.unit_id,
            environment_snapshot={
                "floor": floor_snapshot.floor,
                "node_type": floor_snapshot.node_type.value,
                "region_id": floor_snapshot.region.region_id,
                "region_bias_id": floor_snapshot.region.region_bias_id,
                "enemy_race_id": encounter.race_id,
                "enemy_template_id": encounter.template_id,
                "enemy_count": encounter.enemy_count,
            },
            template_patches_by_template_id=None if not template_patches_by_template_id else template_patches_by_template_id,
            template_path_id_by_template_id=None if not template_path_id_by_template_id else template_path_id_by_template_id,
        )

    def _build_ally_battle_snapshot(
        self,
        *,
        aggregate: CharacterAggregate,
        progress: CharacterProgress,
    ) -> BattleUnitSnapshot:
        current_attributes = self._current_attribute_service.get_pve_view(character_id=aggregate.character.id)
        return current_attributes.build_battle_unit_snapshot(
            unit_id=f"character:{aggregate.character.id}",
            unit_name=aggregate.character.name,
            side=BattleSide.ALLY,
        )

    def _build_ally_template_patches_by_template_id(
        self,
        *,
        character_id: int,
    ) -> dict[str, tuple[object, ...]]:
        current_attributes = self._current_attribute_service.get_pve_view(character_id=character_id)
        return current_attributes.build_template_patches_by_template_id()

    def _build_ally_template_path_id_by_template_id(
        self,
        *,
        character_id: int,
    ) -> dict[str, str]:
        current_attributes = self._current_attribute_service.get_pve_view(character_id=character_id)
        return current_attributes.build_template_path_id_by_template_id()

    def _build_enemy_battle_snapshots(
        self,
        *,
        progress: CharacterProgress,
        encounter: EndlessEnemyEncounter,
    ) -> tuple[BattleUnitSnapshot, ...]:
        per_unit_scale = self._resolve_enemy_per_unit_scale(encounter=encounter)
        template_profile = self._resolve_enemy_template_profile(encounter.template_id)
        behavior_template_id = self._resolve_enemy_behavior_template_id(encounter.template_id)
        race_name = self._enemy_race_name_by_id.get(encounter.race_id, encounter.race_id)
        enemy_units: list[BattleUnitSnapshot] = []
        for index in range(1, encounter.enemy_count + 1):
            unit_scale = per_unit_scale * self._resolve_enemy_slot_scale(index=index)
            max_hp = self._calculate_base_hp(
                realm_id=progress.realm_id,
                stage_id=progress.stage_id,
                factor=unit_scale * _read_decimal(template_profile.get("hp_factor"), default=Decimal("1.0")),
            )
            max_resource = _FULL_RESOURCE_VALUE
            enemy_units.append(
                BattleUnitSnapshot(
                    unit_id=f"enemy:{encounter.floor}:{index}",
                    unit_name=f"{race_name}{index}号",
                    side=BattleSide.ENEMY,
                    behavior_template_id=behavior_template_id,
                    realm_id=progress.realm_id,
                    stage_id=progress.stage_id,
                    max_hp=max_hp,
                    current_hp=max_hp,
                    current_shield=0,
                    max_resource=max_resource,
                    current_resource=max_resource,
                    attack_power=self._calculate_base_attack(
                        realm_id=progress.realm_id,
                        stage_id=progress.stage_id,
                        factor=unit_scale * _read_decimal(template_profile.get("attack_factor"), default=Decimal("1.0")),
                    ),
                    guard_power=self._calculate_base_guard(
                        realm_id=progress.realm_id,
                        stage_id=progress.stage_id,
                        factor=unit_scale * _read_decimal(template_profile.get("guard_factor"), default=Decimal("1.0")),
                    ),
                    speed=self._calculate_base_speed(
                        realm_id=progress.realm_id,
                        stage_id=progress.stage_id,
                        factor=unit_scale * _read_decimal(template_profile.get("speed_factor"), default=Decimal("1.0")),
                    ),
                    crit_rate_permille=_read_int(template_profile.get("crit_rate_permille")),
                    crit_damage_bonus_permille=_read_int(template_profile.get("crit_damage_bonus_permille")),
                    hit_rate_permille=_read_int(template_profile.get("hit_rate_permille"), default=1000),
                    dodge_rate_permille=_read_int(template_profile.get("dodge_rate_permille")),
                    control_bonus_permille=_read_int(template_profile.get("control_bonus_permille")),
                    control_resist_permille=_read_int(template_profile.get("control_resist_permille")),
                    healing_power_permille=_read_int(template_profile.get("healing_power_permille")),
                    shield_power_permille=_read_int(template_profile.get("shield_power_permille")),
                    damage_bonus_permille=_read_int(template_profile.get("damage_bonus_permille")),
                    damage_reduction_permille=_read_int(template_profile.get("damage_reduction_permille")),
                    counter_rate_permille=_read_int(template_profile.get("counter_rate_permille")),
                )
            )
        return tuple(enemy_units)

    def _merge_reward_ledger_payload(
        self,
        *,
        payload: dict[str, Any],
        reward_breakdown: EndlessRewardBreakdown | None,
        encounter_history_entry: dict[str, Any],
        latest_node_result: dict[str, Any],
        anchor_unlock_result: dict[str, Any],
        encounter: EndlessEnemyEncounter,
    ) -> None:
        stable_totals = payload["stable_totals"]
        pending_totals = payload["pending_totals"]
        if reward_breakdown is not None:
            stable_totals["cultivation"] = _read_int(stable_totals.get("cultivation")) + reward_breakdown.stable_cultivation
            stable_totals["insight"] = _read_int(stable_totals.get("insight")) + reward_breakdown.stable_insight
            stable_totals["refining_essence"] = (
                _read_int(stable_totals.get("refining_essence")) + reward_breakdown.stable_refining_essence
            )
            pending_totals["drop_progress"] = (
                _read_int(pending_totals.get("drop_progress")) + reward_breakdown.pending_drop_progress
            )
            payload["last_reward_floor"] = encounter.floor
            payload["advanced_floor_count"] = _read_int(payload.get("advanced_floor_count")) + 1
            drop_display = _normalize_mapping_list(payload.get("drop_display"))
            current_progress = _read_int(pending_totals.get("drop_progress"))
            drop_display.append(
                {
                    "floor": encounter.floor,
                    "node_type": encounter.node_type.value,
                    "region_id": encounter.region_id,
                    "region_bias_id": encounter.region_bias_id,
                    "drop_progress_gained": reward_breakdown.pending_drop_progress,
                    "pending_drop_progress": current_progress,
                    "drop_count": current_progress // 10,
                }
            )
            payload["drop_display"] = drop_display
        payload["latest_node_result"] = latest_node_result
        payload["latest_anchor_unlock"] = anchor_unlock_result
        encounter_history = _normalize_mapping_list(payload.get("encounter_history"))
        encounter_history.append(encounter_history_entry)
        payload["encounter_history"] = encounter_history

    @staticmethod
    def _build_latest_node_result_payload(
        *,
        floor_snapshot,
        encounter: EndlessEnemyEncounter,
        battle_outcome: BattleOutcome,
        battle_report_id: int | None,
        reward_breakdown: EndlessRewardBreakdown | None,
        reward_granted: bool,
        current_hp_ratio: Decimal,
        current_mp_ratio: Decimal,
        enemy_units: tuple[dict[str, Any], ...],
        battle_summary: dict[str, Any],
    ) -> dict[str, Any]:
        reward_payload = None
        if reward_breakdown is not None:
            reward_payload = {
                "stable": reward_breakdown.to_stable_payload(),
                "pending": reward_breakdown.to_pending_payload(),
            }
        return {
            "floor": floor_snapshot.floor,
            "node_type": floor_snapshot.node_type.value,
            "region_id": floor_snapshot.region.region_id,
            "region_bias_id": encounter.region_bias_id,
            "enemy_race_id": encounter.race_id,
            "enemy_template_id": encounter.template_id,
            "enemy_count": encounter.enemy_count,
            "encounter": EndlessDungeonService._encounter_to_payload(encounter),
            "enemy_units": [dict(item) for item in enemy_units],
            "battle_outcome": battle_outcome.value,
            "battle_report_id": battle_report_id,
            "reward_granted": reward_granted,
            "reward_payload": reward_payload,
            "battle_summary": dict(battle_summary),
            "current_hp_ratio": format(current_hp_ratio, ".4f"),
            "current_mp_ratio": format(current_mp_ratio, ".4f"),
        }

    @staticmethod
    def _build_encounter_history_entry(*, node_result: Mapping[str, Any]) -> dict[str, Any]:
        return dict(node_result)

    @staticmethod
    def _build_anchor_unlock_result_payload(
        *,
        cleared_floor: int,
        previous_highest_unlocked_anchor_floor: int,
        new_highest_unlocked_anchor_floor: int,
    ) -> dict[str, Any]:
        unlocked_anchor_floor: int | None = None
        if new_highest_unlocked_anchor_floor > previous_highest_unlocked_anchor_floor:
            unlocked_anchor_floor = new_highest_unlocked_anchor_floor
        return {
            "cleared_floor": cleared_floor,
            "previous_highest_unlocked_anchor_floor": previous_highest_unlocked_anchor_floor,
            "new_highest_unlocked_anchor_floor": new_highest_unlocked_anchor_floor,
            "anchor_floor": unlocked_anchor_floor,
            "unlocked": unlocked_anchor_floor is not None,
        }

    def _require_character_aggregate(self, character_id: int) -> CharacterAggregate:
        aggregate = self._character_repository.get_aggregate(character_id)
        if aggregate is None:
            raise EndlessRunStateError(f"角色不存在：{character_id}")
        return aggregate

    def _require_advancable_run_state(self, character_id: int) -> EndlessRunState:
        endless_run_state = self._state_repository.get_endless_run_state(character_id)
        if endless_run_state is None or endless_run_state.status == _ENDLESS_STATUS_COMPLETED:
            raise EndlessRunNotFoundError(f"角色不存在可推进的无尽副本运行：{character_id}")
        if endless_run_state.status != _ENDLESS_STATUS_RUNNING:
            raise EndlessRunStateError(f"当前无尽副本运行不可推进：{character_id}，状态为 {endless_run_state.status}")
        return endless_run_state

    @staticmethod
    def _require_progress(aggregate: CharacterAggregate) -> CharacterProgress:
        if aggregate.progress is None:
            raise EndlessRunStateError(f"角色缺少成长状态：{aggregate.character.id}")
        return aggregate.progress

    def _resolve_floor(
        self,
        *,
        current_floor: int,
        highest_unlocked_anchor_floor: int,
    ):
        try:
            return self._progression.resolve_floor(
                current_floor,
                highest_unlocked_anchor_floor=highest_unlocked_anchor_floor,
            )
        except EndlessDungeonRuleError as exc:
            raise EndlessRunStateError(str(exc)) from exc

    def _resolve_highest_unlocked_anchor_floor(self, progress: CharacterProgress) -> int:
        if progress.highest_endless_floor < 0:
            raise EndlessRunStateError(f"角色最高无尽层数非法：{progress.highest_endless_floor}")
        return (progress.highest_endless_floor // self._progression.anchor_interval) * self._progression.anchor_interval

    @staticmethod
    def _resolve_entry_floor(selected_start_floor: int) -> int:
        if selected_start_floor <= 1:
            return 1
        return selected_start_floor + 1

    @staticmethod
    def _resolve_run_seed(*, now: datetime, seed: int | None) -> int:
        if seed is not None:
            return seed
        return int(now.timestamp())

    @staticmethod
    def _compose_battle_seed(*, run_seed: int, floor: int) -> int:
        return run_seed * 1009 + floor * 37

    @staticmethod
    def _compose_equipment_drop_seed(*, run_seed: int, floor: int, is_artifact: bool) -> int:
        seed = run_seed * 2029 + floor * 97
        return seed + (53 if is_artifact else 11)

    @staticmethod
    def _build_empty_reward_ledger_payload() -> dict[str, Any]:
        return {
            "version": _REWARD_LEDGER_VERSION,
            "stable_totals": {
                "cultivation": 0,
                "insight": 0,
                "refining_essence": 0,
            },
            "pending_totals": {
                "drop_progress": 0,
            },
            "last_reward_floor": None,
            "drop_display": [],
            "latest_node_result": None,
            "advanced_floor_count": 0,
            "latest_anchor_unlock": None,
            "encounter_history": [],
        }

    def _build_run_snapshot_payload(
        self,
        *,
        status: str,
        selected_start_floor: int,
        current_floor: int,
        current_node_type: EndlessNodeType,
        current_region: EndlessRegionSnapshot,
        highest_unlocked_anchor_floor: int,
        available_start_floors: tuple[int, ...],
        current_anchor_floor: int,
        next_anchor_floor: int,
        run_seed: int,
        encounter_history: list[dict[str, Any]] | tuple[dict[str, Any], ...],
        record_floor_before_run: int,
    ) -> dict[str, Any]:
        return {
            "has_active_run": True,
            "status": status,
            "selected_start_floor": selected_start_floor,
            "current_floor": current_floor,
            "current_node_type": current_node_type.value,
            "run_seed": run_seed,
            "record_floor_before_run": record_floor_before_run,
            "current_region": self._region_to_payload(current_region),
            "anchor_status": {
                "highest_unlocked_anchor_floor": highest_unlocked_anchor_floor,
                "available_start_floors": list(available_start_floors),
                "selected_start_floor": selected_start_floor,
                "selected_start_floor_unlocked": selected_start_floor in available_start_floors,
                "current_anchor_floor": current_anchor_floor,
                "next_anchor_floor": next_anchor_floor,
            },
            "encounter_history": [dict(item) for item in encounter_history],
        }

    @staticmethod
    def _region_to_payload(region: EndlessRegionSnapshot) -> dict[str, Any]:
        return {
            "region_index": region.region_index,
            "region_id": region.region_id,
            "region_name": region.region_name,
            "region_bias_id": region.region_bias_id,
            "start_floor": region.start_floor,
            "end_floor": region.end_floor,
            "theme_summary": region.theme_summary,
        }

    @staticmethod
    def _encounter_to_payload(encounter: EndlessEnemyEncounter) -> dict[str, Any]:
        return {
            "floor": encounter.floor,
            "region_id": encounter.region_id,
            "region_bias_id": encounter.region_bias_id,
            "node_type": encounter.node_type.value,
            "race_id": encounter.race_id,
            "template_id": encounter.template_id,
            "enemy_count": encounter.enemy_count,
            "seed": encounter.seed,
        }

    @staticmethod
    def _normalize_reward_ledger_payload(payload: Mapping[str, Any] | None) -> dict[str, Any]:
        normalized_payload = EndlessDungeonService._build_empty_reward_ledger_payload()
        if payload is None:
            return normalized_payload
        stable_totals = payload.get("stable_totals")
        pending_totals = payload.get("pending_totals")
        if isinstance(stable_totals, Mapping):
            normalized_payload["stable_totals"] = {
                "cultivation": _read_int(stable_totals.get("cultivation")),
                "insight": _read_int(stable_totals.get("insight")),
                "refining_essence": _read_int(stable_totals.get("refining_essence")),
            }
        pending_drop_progress = 0
        if isinstance(pending_totals, Mapping):
            pending_drop_progress = _read_int(pending_totals.get("drop_progress"))
            if pending_drop_progress <= 0:
                pending_drop_progress = (
                    _read_int(pending_totals.get("equipment_score"))
                    + _read_int(pending_totals.get("artifact_score"))
                    + _read_int(pending_totals.get("dao_pattern_score"))
                )
            normalized_payload["pending_totals"] = {
                "drop_progress": pending_drop_progress,
            }
        normalized_payload["last_reward_floor"] = _read_optional_int(payload.get("last_reward_floor"))
        normalized_payload["drop_display"] = _normalize_mapping_list(payload.get("drop_display"))
        normalized_payload["latest_node_result"] = _normalize_optional_mapping(payload.get("latest_node_result"))
        normalized_payload["advanced_floor_count"] = _read_int(payload.get("advanced_floor_count"))
        normalized_payload["latest_anchor_unlock"] = _normalize_optional_mapping(payload.get("latest_anchor_unlock"))
        normalized_payload["encounter_history"] = _normalize_mapping_list(payload.get("encounter_history"))
        return normalized_payload

    @staticmethod
    def _parse_reward_ledger(payload: Mapping[str, Any] | None) -> EndlessRunRewardLedgerSnapshot:
        normalized_payload = EndlessDungeonService._normalize_reward_ledger_payload(payload)
        stable_totals = normalized_payload["stable_totals"]
        pending_totals = normalized_payload["pending_totals"]
        pending_drop_progress = _read_int(pending_totals.get("drop_progress"))
        return EndlessRunRewardLedgerSnapshot(
            stable_cultivation=_read_int(stable_totals.get("cultivation")),
            stable_insight=_read_int(stable_totals.get("insight")),
            stable_refining_essence=_read_int(stable_totals.get("refining_essence")),
            pending_drop_progress=pending_drop_progress,
            drop_count=pending_drop_progress // 10,
            last_reward_floor=_read_optional_int(normalized_payload.get("last_reward_floor")),
            drop_display=tuple(_normalize_mapping_list(normalized_payload.get("drop_display"))),
            latest_node_result=_normalize_optional_mapping(normalized_payload.get("latest_node_result")),
            advanced_floor_count=_read_int(normalized_payload.get("advanced_floor_count")),
            latest_anchor_unlock=_normalize_optional_mapping(normalized_payload.get("latest_anchor_unlock")),
            encounter_history=tuple(_normalize_mapping_list(normalized_payload.get("encounter_history"))),
        )

    @staticmethod
    def _parse_node_type(raw_value: str) -> EndlessNodeType:
        try:
            return EndlessNodeType(raw_value)
        except ValueError as exc:
            raise EndlessRunStateError(f"无效的当前节点类型：{raw_value}") from exc

    def _resolve_hero_template_id(self, *, aggregate: CharacterAggregate) -> str:
        template_id = _DEFAULT_HERO_TEMPLATE_ID
        if aggregate.skill_loadout is not None and aggregate.skill_loadout.behavior_template_id.strip():
            template_id = aggregate.skill_loadout.behavior_template_id
        if template_id not in _PATH_COMBAT_PROFILE_BY_TEMPLATE_ID:
            raise EndlessRunStateError(f"未支持的角色行为模板：{template_id}")
        return template_id

    @staticmethod
    def _resolve_template_profile(template_id: str) -> dict[str, Decimal | int]:
        try:
            return _PATH_COMBAT_PROFILE_BY_TEMPLATE_ID[template_id]
        except KeyError as exc:
            raise EndlessRunStateError(f"未支持的行为模板画像：{template_id}") from exc

    @staticmethod
    def _resolve_enemy_template_profile(template_id: str) -> dict[str, Decimal | int]:
        try:
            return _ENEMY_TEMPLATE_STAT_PROFILE[template_id]
        except KeyError as exc:
            raise EndlessRunStateError(f"未支持的敌人模板画像：{template_id}") from exc

    @staticmethod
    def _resolve_enemy_behavior_template_id(template_id: str) -> str:
        try:
            return _ENEMY_BEHAVIOR_TEMPLATE_BY_TEMPLATE_ID[template_id]
        except KeyError as exc:
            raise EndlessRunStateError(f"未配置敌人行为模板映射：{template_id}") from exc

    def _calculate_base_hp(self, *, realm_id: str, stage_id: str, factor: Decimal) -> int:
        return self._calculate_scaled_stat(
            base_value=self._static_config.base_coefficients.scalar.base_hp,
            realm_id=realm_id,
            stage_id=stage_id,
            divisor=Decimal("12"),
            factor=factor,
            minimum=1,
        )

    def _calculate_base_attack(self, *, realm_id: str, stage_id: str, factor: Decimal) -> int:
        return self._calculate_scaled_stat(
            base_value=self._static_config.base_coefficients.scalar.base_attack,
            realm_id=realm_id,
            stage_id=stage_id,
            divisor=Decimal("2"),
            factor=factor,
            minimum=1,
        )

    def _calculate_base_guard(self, *, realm_id: str, stage_id: str, factor: Decimal) -> int:
        return self._calculate_scaled_stat(
            base_value=self._static_config.base_coefficients.scalar.base_defense,
            realm_id=realm_id,
            stage_id=stage_id,
            divisor=Decimal("4"),
            factor=factor,
            minimum=0,
        )

    def _calculate_base_speed(self, *, realm_id: str, stage_id: str, factor: Decimal) -> int:
        base_speed = Decimal(self._static_config.base_coefficients.scalar.base_speed)
        realm_coefficient = self._resolve_realm_coefficient(realm_id)
        stage_multiplier = self._resolve_stage_multiplier(stage_id)
        scaled_value = (base_speed + realm_coefficient * Decimal("2")) * stage_multiplier * factor
        return max(1, _round_decimal_to_int(scaled_value))

    def _calculate_scaled_stat(
        self,
        *,
        base_value: int,
        realm_id: str,
        stage_id: str,
        divisor: Decimal,
        factor: Decimal,
        minimum: int,
    ) -> int:
        realm_coefficient = self._resolve_realm_coefficient(realm_id)
        stage_multiplier = self._resolve_stage_multiplier(stage_id)
        scaled_value = Decimal(base_value) * realm_coefficient * stage_multiplier * factor / divisor
        return max(minimum, _round_decimal_to_int(scaled_value))

    def _resolve_realm_coefficient(self, realm_id: str) -> Decimal:
        try:
            return self._realm_coefficient_by_realm_id[realm_id]
        except KeyError as exc:
            raise EndlessRunStateError(f"未找到大境界基准系数：{realm_id}") from exc

    def _resolve_stage_multiplier(self, stage_id: str) -> Decimal:
        try:
            return self._stage_multiplier_by_stage_id[stage_id]
        except KeyError as exc:
            raise EndlessRunStateError(f"未找到小阶段倍率：{stage_id}") from exc

    def _resolve_enemy_per_unit_scale(self, *, encounter: EndlessEnemyEncounter) -> Decimal:
        base_scale = Decimal("0.52") + Decimal(encounter.floor) * Decimal("0.02")
        if encounter.node_type is EndlessNodeType.ELITE:
            base_scale += Decimal("0.10")
        elif encounter.node_type is EndlessNodeType.ANCHOR_BOSS:
            base_scale += Decimal("0.18")
        enemy_divisor = Decimal(max(1, encounter.enemy_count))
        return base_scale / enemy_divisor

    @staticmethod
    def _resolve_enemy_slot_scale(*, index: int) -> Decimal:
        if index == 1:
            return Decimal("1.06")
        if index == 2:
            return Decimal("0.98")
        return Decimal("0.92")

    @staticmethod
    def _apply_ratio(*, max_hp: int, ratio: Decimal) -> int:
        normalized_ratio = max(Decimal("0.0000"), min(Decimal("1.0000"), Decimal(ratio)))
        current_value = _round_decimal_to_int(Decimal(max_hp) * normalized_ratio)
        if normalized_ratio > Decimal("0") and current_value <= 0:
            return 1
        return max(0, min(max_hp, current_value))

    @staticmethod
    def _extract_battle_outcome(execution_result) -> BattleOutcome:
        outcome = execution_result.domain_result.outcome
        if not isinstance(outcome, BattleOutcome):
            raise EndlessRunStateError(f"自动战斗返回了无效战斗结果：{outcome}")
        return outcome

    @staticmethod
    def _apply_progress_writeback(
        *,
        progress: CharacterProgress,
        current_hp_ratio: Decimal,
        current_mp_ratio: Decimal,
    ) -> None:
        progress.current_hp_ratio = current_hp_ratio
        progress.current_mp_ratio = current_mp_ratio

    @staticmethod
    def _build_enemy_unit_payloads(enemy_units: tuple[BattleUnitSnapshot, ...]) -> tuple[dict[str, Any], ...]:
        return tuple(
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
        )

    @staticmethod
    def _is_decision_floor(floor: int) -> bool:
        normalized_floor = max(1, floor)
        return normalized_floor % 10 in (5, 0)

    @staticmethod
    def _resolve_last_decision_floor(payload: Mapping[str, Any] | None) -> int | None:
        normalized_payload = EndlessDungeonService._normalize_reward_ledger_payload(payload)
        last_reward_floor = _read_optional_int(normalized_payload.get("last_reward_floor"))
        if last_reward_floor is None or not EndlessDungeonService._is_decision_floor(last_reward_floor):
            return None
        latest_node_result = _normalize_optional_mapping(normalized_payload.get("latest_node_result"))
        if latest_node_result is not None and not bool(latest_node_result.get("reward_granted")):
            return None
        return last_reward_floor



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



def _read_decimal(value: Any, *, default: Decimal) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str):
        try:
            return Decimal(value)
        except Exception:
            return default
    return default



def _round_decimal_to_int(value: Decimal) -> int:
    return int(value.quantize(_DECIMAL_ONE, rounding=ROUND_HALF_UP))



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
    "EndlessDungeonService",
    "EndlessDungeonServiceError",
    "EndlessFloorAdvanceResult",
    "EndlessRunAlreadyRunningError",
    "EndlessRunAnchorSnapshot",
    "EndlessRunNotFoundError",
    "EndlessRunRewardLedgerSnapshot",
    "EndlessRunSettlementResult",
    "EndlessRunStateError",
    "EndlessRunStatusSnapshot",
    "EndlessSettlementRewardSection",
    "InvalidEndlessStartFloorError",
]
