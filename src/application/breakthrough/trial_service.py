"""突破秘境入口应用服务。"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP

from application.battle import AutoBattleRequest, AutoBattleService
from application.character.current_attribute_service import CurrentAttributeService
from application.breakthrough.reward_service import (
    BreakthroughRewardApplicationResult,
    BreakthroughRewardService,
)
from application.character.growth_service import CharacterGrowthStateError, CharacterNotFoundError
from application.dungeon.endless_service import (
    _DEFAULT_HERO_TEMPLATE_ID,
    _ENEMY_BEHAVIOR_TEMPLATE_BY_TEMPLATE_ID,
    _ENEMY_TEMPLATE_STAT_PROFILE,
    _FULL_RESOURCE_VALUE,
    _PATH_COMBAT_PROFILE_BY_TEMPLATE_ID,
)
from domain.battle import (
    ActionNumericBonusPatch,
    ActionNumericField,
    ActionMultiplierPatch,
    ActionPatchSelector,
    ActionThresholdField,
    ActionThresholdShiftPatch,
    ActionTriggerCapAdjustment,
    AuxiliarySkillParameterPatch,
    BattleOutcome,
    BattleSide,
    BattleSnapshot,
    BattleUnitSnapshot,
)
from domain.breakthrough import BreakthroughRuleService, BreakthroughTrialProgressStatus
from infrastructure.config.static import StaticGameConfig, get_static_config
from infrastructure.config.static.models.breakthrough import (
    ActionPatchSelectorDefinition,
    BreakthroughTrialDefinition,
    EnvironmentRuleDefinition,
    EnvironmentStatModifierDefinition,
    EnvironmentTemplatePatchDefinition,
)
from infrastructure.db.models import BreakthroughTrialProgress, CharacterProgress
from infrastructure.db.repositories import (
    BreakthroughRepository,
    CharacterAggregate,
    CharacterRepository,
    StateRepository,
    build_breakthrough_progress_snapshot,
)

_BREAKTHROUGH_BATTLE_TYPE = "breakthrough_trial"
_BREAKTHROUGH_ENVIRONMENT_TAG = "breakthrough_trial"
_ENDLESS_STATUS_COMPLETED = "completed"
_RUNNING_STATUS = "running"
_DEFAULT_ROUND_LIMIT = 12
_DECIMAL_ONE = Decimal("1")
_DECIMAL_THOUSAND = Decimal("1000")
_PERMILLE_STANDARD_MAX = 1000
_PERMILLE_EXTENDED_MAX = 5000


@dataclass(frozen=True, slots=True)
class BreakthroughTrialEntrySnapshot:
    """突破关卡在入口面板中的只读摘要。"""

    mapping_id: str
    trial_name: str
    group_id: str
    from_realm_id: str
    to_realm_id: str
    environment_rule: str
    environment_rule_id: str
    repeat_reward_direction: str
    boss_template_id: str
    boss_stage_id: str
    boss_scale_permille: int
    first_clear_grants_qualification: bool
    can_challenge: bool
    is_cleared: bool
    is_current_trial: bool
    attempt_count: int
    cleared_count: int
    best_clear_at: str | None
    first_cleared_at: str | None
    last_cleared_at: str | None
    qualification_granted_at: str | None
    last_reward_direction: str | None


@dataclass(frozen=True, slots=True)
class BreakthroughTrialGroupSnapshot:
    """突破秘境分组概览。"""

    group_id: str
    group_name: str
    theme_summary: str
    reward_focus_summary: str
    trials: tuple[BreakthroughTrialEntrySnapshot, ...]


@dataclass(frozen=True, slots=True)
class BreakthroughTrialHubSnapshot:
    """破境天关入口稳定返回结构。"""

    character_id: int
    current_realm_id: str
    current_stage_id: str
    qualification_obtained: bool
    current_hp_ratio: str
    current_mp_ratio: str
    current_trial_mapping_id: str | None
    current_trial: BreakthroughTrialEntrySnapshot | None
    repeatable_trials: tuple[BreakthroughTrialEntrySnapshot, ...]
    cleared_mapping_ids: tuple[str, ...]
    groups: tuple[BreakthroughTrialGroupSnapshot, ...]


@dataclass(frozen=True, slots=True)
class BreakthroughTrialChallengeResult:
    """单次突破秘境挑战后的稳定返回结构。"""

    character_id: int
    mapping_id: str
    trial_name: str
    group_id: str
    battle_outcome: str
    battle_report_id: int | None
    environment_snapshot: dict[str, object]
    settlement: BreakthroughRewardApplicationResult
    current_hp_ratio: str
    current_mp_ratio: str
    qualification_obtained: bool
    trial_snapshot: BreakthroughTrialEntrySnapshot
    hub_snapshot: BreakthroughTrialHubSnapshot


class BreakthroughTrialServiceError(RuntimeError):
    """突破秘境应用服务基础异常。"""


class BreakthroughTrialStateError(BreakthroughTrialServiceError):
    """突破秘境所需角色状态不完整。"""


class BreakthroughTrialConflictError(BreakthroughTrialServiceError):
    """角色当前存在与突破秘境冲突的运行状态。"""


class BreakthroughTrialUnavailableError(BreakthroughTrialServiceError):
    """当前关卡不满足挑战条件。"""


class BreakthroughTrialNotFoundError(BreakthroughTrialServiceError):
    """请求的突破关卡不存在。"""


class BreakthroughTrialService:
    """负责编排突破关卡入口、战斗接入与阶段 7 结算链路。"""

    def __init__(
        self,
        *,
        state_repository: StateRepository,
        character_repository: CharacterRepository,
        breakthrough_repository: BreakthroughRepository,
        auto_battle_service: AutoBattleService,
        reward_service: BreakthroughRewardService,
        current_attribute_service: CurrentAttributeService,
        static_config: StaticGameConfig | None = None,
        rule_service: BreakthroughRuleService | None = None,
    ) -> None:
        self._state_repository = state_repository
        self._character_repository = character_repository
        self._breakthrough_repository = breakthrough_repository
        self._auto_battle_service = auto_battle_service
        self._reward_service = reward_service
        self._current_attribute_service = current_attribute_service
        self._static_config = static_config or get_static_config()
        self._rule_service = rule_service or BreakthroughRuleService(self._static_config)
        self._rule_service.validate_trial_configuration()
        self._realm_coefficient_by_realm_id = {
            entry.realm_id: Decimal(entry.coefficient)
            for entry in self._static_config.base_coefficients.realm_curve.entries
        }
        self._stage_multiplier_by_stage_id = {
            stage.stage_id: Decimal(stage.multiplier)
            for stage in self._static_config.realm_progression.stages
        }

    def get_trial_hub(self, *, character_id: int) -> BreakthroughTrialHubSnapshot:
        """读取角色当前可见的突破秘境入口面板。"""
        aggregate = self._require_character_aggregate(character_id)
        progress = self._require_progress(aggregate)
        progress_entries = self._breakthrough_repository.list_by_character_id(character_id)
        progress_snapshot_map = {
            entry.mapping_id: build_breakthrough_progress_snapshot(entry)
            for entry in progress_entries
        }
        cleared_mapping_ids = {
            mapping_id
            for mapping_id, snapshot in progress_snapshot_map.items()
            if snapshot.status is BreakthroughTrialProgressStatus.CLEARED
        }
        current_trial = self._rule_service.get_current_trial(current_realm_id=progress.realm_id)
        current_trial_mapping_id = None if current_trial is None else current_trial.mapping_id

        group_snapshots: list[BreakthroughTrialGroupSnapshot] = []
        repeatable_trials: list[BreakthroughTrialEntrySnapshot] = []
        current_trial_snapshot: BreakthroughTrialEntrySnapshot | None = None
        for group in self._static_config.breakthrough_trials.ordered_trial_groups:
            group_trials: list[BreakthroughTrialEntrySnapshot] = []
            for trial in self._iter_trials_by_group(group.group_id):
                trial_snapshot = self._build_trial_entry_snapshot(
                    trial=trial,
                    current_realm_id=progress.realm_id,
                    current_trial_mapping_id=current_trial_mapping_id,
                    cleared_mapping_ids=cleared_mapping_ids,
                    progress_snapshot=progress_snapshot_map.get(trial.mapping_id),
                )
                group_trials.append(trial_snapshot)
                if trial_snapshot.is_current_trial:
                    current_trial_snapshot = trial_snapshot
                if trial_snapshot.is_cleared:
                    repeatable_trials.append(trial_snapshot)
            group_snapshots.append(
                BreakthroughTrialGroupSnapshot(
                    group_id=group.group_id,
                    group_name=group.name,
                    theme_summary=group.theme_summary,
                    reward_focus_summary=group.reward_focus_summary,
                    trials=tuple(group_trials),
                )
            )

        return BreakthroughTrialHubSnapshot(
            character_id=character_id,
            current_realm_id=progress.realm_id,
            current_stage_id=progress.stage_id,
            qualification_obtained=progress.breakthrough_qualification_obtained,
            current_hp_ratio=format(progress.current_hp_ratio, ".4f"),
            current_mp_ratio=format(progress.current_mp_ratio, ".4f"),
            current_trial_mapping_id=current_trial_mapping_id,
            current_trial=current_trial_snapshot,
            repeatable_trials=tuple(repeatable_trials),
            cleared_mapping_ids=tuple(sorted(cleared_mapping_ids)),
            groups=tuple(group_snapshots),
        )

    def challenge_trial(
        self,
        *,
        character_id: int,
        mapping_id: str | None = None,
        seed: int | None = None,
        now: datetime | None = None,
        persist_battle_report: bool = True,
    ) -> BreakthroughTrialChallengeResult:
        """执行一次突破秘境挑战，并串联阶段 7 的奖励与进度写入。"""
        current_time = now or datetime.utcnow()
        aggregate = self._require_character_aggregate(character_id)
        progress = self._require_progress(aggregate)
        trial = self._resolve_trial(current_realm_id=progress.realm_id, mapping_id=mapping_id)
        self._ensure_no_conflict_states(character_id)

        cleared_mapping_ids = {
            entry.mapping_id
            for entry in self._breakthrough_repository.list_cleared_by_character_id(character_id)
        }
        if not self._rule_service.can_challenge_trial(
            current_realm_id=progress.realm_id,
            target_mapping_id=trial.mapping_id,
            cleared_mapping_ids=cleared_mapping_ids,
        ):
            raise BreakthroughTrialUnavailableError(
                f"当前不可挑战突破关卡：{trial.mapping_id}"
            )
        progress_entry = self._breakthrough_repository.get_or_create_progress(
            character_id,
            trial.mapping_id,
            group_id=trial.group_id,
        )

        request = self._build_auto_battle_request(
            aggregate=aggregate,
            progress=progress,
            trial=trial,
            seed=self._resolve_battle_seed(now=current_time, seed=seed, trial=trial),
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

        battle_outcome = self._extract_battle_outcome(execution_result.domain_result.outcome)
        settlement = self._reward_service.apply_battle_result(
            aggregate=aggregate,
            trial=trial,
            trial_progress=progress_entry,
            battle_outcome=battle_outcome,
            battle_report_id=execution_result.persisted_battle_report_id,
            occurred_at=current_time,
        )
        hub_snapshot = self.get_trial_hub(character_id=character_id)
        trial_snapshot = self._require_trial_snapshot(hub_snapshot=hub_snapshot, mapping_id=trial.mapping_id)
        return BreakthroughTrialChallengeResult(
            character_id=character_id,
            mapping_id=trial.mapping_id,
            trial_name=trial.name,
            group_id=trial.group_id,
            battle_outcome=battle_outcome.value,
            battle_report_id=execution_result.persisted_battle_report_id,
            environment_snapshot=dict(request.environment_snapshot or {}),
            settlement=settlement,
            current_hp_ratio=format(progress.current_hp_ratio, ".4f"),
            current_mp_ratio=format(progress.current_mp_ratio, ".4f"),
            qualification_obtained=progress.breakthrough_qualification_obtained,
            trial_snapshot=trial_snapshot,
            hub_snapshot=hub_snapshot,
        )

    def _build_trial_entry_snapshot(
        self,
        *,
        trial: BreakthroughTrialDefinition,
        current_realm_id: str,
        current_trial_mapping_id: str | None,
        cleared_mapping_ids: set[str],
        progress_snapshot,
    ) -> BreakthroughTrialEntrySnapshot:
        """把静态关卡与角色进度合成为入口可读摘要。"""
        status = None if progress_snapshot is None else progress_snapshot.status
        is_cleared = status is BreakthroughTrialProgressStatus.CLEARED
        return BreakthroughTrialEntrySnapshot(
            mapping_id=trial.mapping_id,
            trial_name=trial.name,
            group_id=trial.group_id,
            from_realm_id=trial.from_realm_id,
            to_realm_id=trial.to_realm_id,
            environment_rule=trial.environment_rule,
            environment_rule_id=trial.environment_rule_id,
            repeat_reward_direction=trial.repeat_reward_direction,
            boss_template_id=trial.boss_template_id,
            boss_stage_id=trial.boss_stage_id,
            boss_scale_permille=trial.boss_scale_permille,
            first_clear_grants_qualification=trial.first_clear_grants_qualification,
            can_challenge=self._rule_service.can_challenge_trial(
                current_realm_id=current_realm_id,
                target_mapping_id=trial.mapping_id,
                cleared_mapping_ids=cleared_mapping_ids,
            ),
            is_cleared=is_cleared,
            is_current_trial=trial.mapping_id == current_trial_mapping_id,
            attempt_count=0 if progress_snapshot is None else progress_snapshot.attempt_count,
            cleared_count=0 if progress_snapshot is None else progress_snapshot.cleared_count,
            best_clear_at=None if progress_snapshot is None else progress_snapshot.best_clear_at,
            first_cleared_at=None if progress_snapshot is None else progress_snapshot.first_cleared_at,
            last_cleared_at=None if progress_snapshot is None else progress_snapshot.last_cleared_at,
            qualification_granted_at=None if progress_snapshot is None else progress_snapshot.qualification_granted_at,
            last_reward_direction=None if progress_snapshot is None else progress_snapshot.last_reward_direction,
        )

    def _build_auto_battle_request(
        self,
        *,
        aggregate: CharacterAggregate,
        progress: CharacterProgress,
        trial: BreakthroughTrialDefinition,
        seed: int,
    ) -> AutoBattleRequest:
        """把突破关卡、环境规则与角色状态装配为自动战斗请求。"""
        environment_rule = self._require_environment_rule(trial.environment_rule_id)
        ally_snapshot = self._build_ally_battle_snapshot(aggregate=aggregate, progress=progress)
        enemy_snapshot = self._build_enemy_battle_snapshot(trial=trial)
        ally_snapshot = self._apply_environment_stat_modifiers(
            snapshot=ally_snapshot,
            modifiers=environment_rule.ally_stat_modifiers,
        )
        enemy_snapshot = self._apply_environment_stat_modifiers(
            snapshot=enemy_snapshot,
            modifiers=environment_rule.enemy_stat_modifiers,
        )
        environment_tags = (
            _BREAKTHROUGH_ENVIRONMENT_TAG,
            trial.group_id,
            trial.mapping_id,
            environment_rule.rule_id,
            *environment_rule.environment_tags,
        )
        current_attributes = self._current_attribute_service.get_pve_view(character_id=aggregate.character.id)
        template_patches_by_template_id = self._merge_template_patch_maps(
            current_attributes.build_template_patches_by_template_id(),
            self._build_template_patches(
                ally_template_id=ally_snapshot.behavior_template_id,
                enemy_template_id=enemy_snapshot.behavior_template_id,
                environment_rule=environment_rule,
            ),
        )
        template_path_id_by_template_id = current_attributes.build_template_path_id_by_template_id()
        return AutoBattleRequest(
            character_id=aggregate.character.id,
            battle_type=_BREAKTHROUGH_BATTLE_TYPE,
            snapshot=BattleSnapshot(
                seed=seed,
                allies=(ally_snapshot,),
                enemies=(enemy_snapshot,),
                round_limit=_DEFAULT_ROUND_LIMIT,
                environment_tags=environment_tags,
            ),
            opponent_ref=f"breakthrough:{trial.mapping_id}:{trial.boss_template_id}:{trial.boss_stage_id}",
            focus_unit_id=ally_snapshot.unit_id,
            environment_snapshot={
                "mapping_id": trial.mapping_id,
                "group_id": trial.group_id,
                "from_realm_id": trial.from_realm_id,
                "to_realm_id": trial.to_realm_id,
                "boss_template_id": trial.boss_template_id,
                "boss_stage_id": trial.boss_stage_id,
                "boss_scale_permille": trial.boss_scale_permille,
                "environment_rule_id": environment_rule.rule_id,
                "environment_rule_summary": environment_rule.summary,
                "environment_tags": ",".join(environment_rule.environment_tags),
                "reward_direction": trial.repeat_reward_direction,
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
        """基于统一当前属性构造突破战斗主角快照。"""
        current_attributes = self._current_attribute_service.get_pve_view(character_id=aggregate.character.id)
        return current_attributes.build_battle_unit_snapshot(
            unit_id=f"character:{aggregate.character.id}",
            unit_name=aggregate.character.name,
            side=BattleSide.ALLY,
        )

    def _build_enemy_battle_snapshot(self, *, trial: BreakthroughTrialDefinition) -> BattleUnitSnapshot:
        """按固定大境界门槛构造单体突破首领快照。"""
        template_profile = self._resolve_enemy_template_profile(trial.boss_template_id)
        behavior_template_id = self._resolve_enemy_behavior_template_id(trial.boss_template_id)
        scale_factor = Decimal(trial.boss_scale_permille) / _DECIMAL_THOUSAND
        max_hp = self._calculate_base_hp(
            realm_id=trial.from_realm_id,
            stage_id=trial.boss_stage_id,
            factor=scale_factor * _read_decimal(template_profile.get("hp_factor"), default=Decimal("1.0")),
        )
        max_resource = _FULL_RESOURCE_VALUE
        return BattleUnitSnapshot(
            unit_id=f"breakthrough_boss:{trial.mapping_id}",
            unit_name=f"{trial.name}守关者",
            side=BattleSide.ENEMY,
            behavior_template_id=behavior_template_id,
            realm_id=trial.from_realm_id,
            stage_id=trial.boss_stage_id,
            max_hp=max_hp,
            current_hp=max_hp,
            current_shield=0,
            max_resource=max_resource,
            current_resource=max_resource,
            attack_power=self._calculate_base_attack(
                realm_id=trial.from_realm_id,
                stage_id=trial.boss_stage_id,
                factor=scale_factor * _read_decimal(template_profile.get("attack_factor"), default=Decimal("1.0")),
            ),
            guard_power=self._calculate_base_guard(
                realm_id=trial.from_realm_id,
                stage_id=trial.boss_stage_id,
                factor=scale_factor * _read_decimal(template_profile.get("guard_factor"), default=Decimal("1.0")),
            ),
            speed=self._calculate_base_speed(
                realm_id=trial.from_realm_id,
                stage_id=trial.boss_stage_id,
                factor=scale_factor * _read_decimal(template_profile.get("speed_factor"), default=Decimal("1.0")),
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

    @staticmethod
    def _merge_template_patch_maps(
        *mappings: dict[str, tuple[AuxiliarySkillParameterPatch, ...]],
    ) -> dict[str, tuple[AuxiliarySkillParameterPatch, ...]]:
        merged: dict[str, tuple[AuxiliarySkillParameterPatch, ...]] = {}
        for mapping in mappings:
            for template_id, patches in mapping.items():
                merged[template_id] = merged.get(template_id, ()) + tuple(patches)
        return merged

    def _build_template_patches(
        self,
        *,
        ally_template_id: str,
        enemy_template_id: str,
        environment_rule: EnvironmentRuleDefinition,
    ) -> dict[str, tuple[AuxiliarySkillParameterPatch, ...]]:
        """把环境规则转译为战斗模板补丁映射。"""
        patch_map: dict[str, tuple[AuxiliarySkillParameterPatch, ...]] = {}
        ally_patches = tuple(self._build_template_patch(item) for item in environment_rule.ally_template_patches)
        enemy_patches = tuple(self._build_template_patch(item) for item in environment_rule.enemy_template_patches)
        if ally_patches:
            patch_map[ally_template_id] = ally_patches
        if enemy_patches:
            patch_map[enemy_template_id] = patch_map.get(enemy_template_id, ()) + enemy_patches
        return patch_map

    def _build_template_patch(self, definition: EnvironmentTemplatePatchDefinition) -> AuxiliarySkillParameterPatch:
        """把环境补丁定义转成现有战斗补丁对象。"""
        return AuxiliarySkillParameterPatch(
            patch_id=definition.patch_id,
            patch_name=definition.patch_name,
            numeric_bonuses=tuple(
                ActionNumericBonusPatch(
                    field=ActionNumericField(item.field),
                    delta=item.delta,
                    selector=self._build_action_selector(item.selector),
                )
                for item in definition.numeric_bonuses
            ),
            multipliers=tuple(
                ActionMultiplierPatch(
                    field=ActionNumericField(item.field),
                    multiplier_permille=item.multiplier_permille,
                    selector=self._build_action_selector(item.selector),
                )
                for item in definition.multipliers
            ),
            threshold_shifts=tuple(
                ActionThresholdShiftPatch(
                    field=ActionThresholdField(item.field),
                    delta=item.delta,
                    selector=self._build_action_selector(item.selector),
                )
                for item in definition.threshold_shifts
            ),
            trigger_cap_adjustments=tuple(
                ActionTriggerCapAdjustment(
                    delta=item.delta,
                    selector=self._build_action_selector(item.selector),
                )
                for item in definition.trigger_cap_adjustments
            ),
        )

    @staticmethod
    def _build_action_selector(definition: ActionPatchSelectorDefinition) -> ActionPatchSelector:
        """环境补丁选择器直接复用战斗领域同名结构。"""
        return ActionPatchSelector(
            action_ids=definition.action_ids,
            required_labels=definition.required_labels,
        )

    def _apply_environment_stat_modifiers(
        self,
        *,
        snapshot: BattleUnitSnapshot,
        modifiers: Iterable[EnvironmentStatModifierDefinition],
    ) -> BattleUnitSnapshot:
        """把环境规则中的静态属性修正投影到战斗单位快照。"""
        updated = snapshot
        for modifier in modifiers:
            if not hasattr(updated, modifier.stat_field):
                raise BreakthroughTrialStateError(f"环境修正引用了未知战斗属性：{modifier.stat_field}")
            current_value = getattr(updated, modifier.stat_field)
            if not isinstance(current_value, int):
                raise BreakthroughTrialStateError(f"环境修正目标不是整数字段：{modifier.stat_field}")
            next_value = self._resolve_modified_stat_value(
                field_name=modifier.stat_field,
                current_value=current_value,
                delta=modifier.delta,
                multiplier_permille=modifier.multiplier_permille,
            )
            if modifier.stat_field == "max_hp":
                ratio = Decimal(updated.current_hp) / Decimal(updated.max_hp)
                updated = replace(
                    updated,
                    max_hp=next_value,
                    current_hp=self._apply_ratio(max_value=next_value, ratio=ratio),
                )
                continue
            updated = replace(
                updated,
                **{modifier.stat_field: next_value},
            )
        return updated

    def _ensure_no_conflict_states(self, character_id: int) -> None:
        """突破秘境与其他持续运行态互斥。"""
        endless_state = self._state_repository.get_endless_run_state(character_id)
        if endless_state is not None and endless_state.status != _ENDLESS_STATUS_COMPLETED:
            raise BreakthroughTrialConflictError(
                f"角色存在未结束的无尽副本运行：{character_id}"
            )
        retreat_state = self._state_repository.get_retreat_state(character_id)
        if retreat_state is not None and retreat_state.status == _RUNNING_STATUS and retreat_state.settled_at is None:
            raise BreakthroughTrialConflictError(f"角色当前处于闭关中：{character_id}")
        healing_state = self._state_repository.get_healing_state(character_id)
        if healing_state is not None and healing_state.status == _RUNNING_STATUS and healing_state.settled_at is None:
            raise BreakthroughTrialConflictError(f"角色当前处于疗伤中：{character_id}")

    def _resolve_trial(self, *, current_realm_id: str, mapping_id: str | None) -> BreakthroughTrialDefinition:
        """支持按当前境界默认映射或显式映射标识读取关卡。"""
        if mapping_id is None:
            trial = self._rule_service.get_current_trial(current_realm_id=current_realm_id)
            if trial is None:
                raise BreakthroughTrialUnavailableError(f"当前大境界不存在可首通的突破关卡：{current_realm_id}")
            return trial
        trial = self._static_config.breakthrough_trials.get_trial(mapping_id)
        if trial is None:
            raise BreakthroughTrialNotFoundError(f"未定义的突破关卡：{mapping_id}")
        return trial

    def _require_environment_rule(self, rule_id: str) -> EnvironmentRuleDefinition:
        """读取突破关卡关联的可执行环境规则。"""
        rule = self._static_config.breakthrough_trials.get_environment_rule(rule_id)
        if rule is None:
            raise BreakthroughTrialStateError(f"突破关卡缺少环境规则：{rule_id}")
        return rule

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
    def _require_trial_snapshot(
        *,
        hub_snapshot: BreakthroughTrialHubSnapshot,
        mapping_id: str,
    ) -> BreakthroughTrialEntrySnapshot:
        for group in hub_snapshot.groups:
            for trial in group.trials:
                if trial.mapping_id == mapping_id:
                    return trial
        raise BreakthroughTrialStateError(f"突破入口快照缺少关卡摘要：{mapping_id}")

    def _iter_trials_by_group(self, group_id: str) -> Iterable[BreakthroughTrialDefinition]:
        for trial in self._static_config.breakthrough_trials.ordered_trials:
            if trial.group_id == group_id:
                yield trial

    def _resolve_hero_template_id(self, *, aggregate: CharacterAggregate) -> str:
        template_id = _DEFAULT_HERO_TEMPLATE_ID
        if aggregate.skill_loadout is not None and aggregate.skill_loadout.behavior_template_id.strip():
            template_id = aggregate.skill_loadout.behavior_template_id
        if template_id not in _PATH_COMBAT_PROFILE_BY_TEMPLATE_ID:
            raise BreakthroughTrialStateError(f"未支持的角色行为模板：{template_id}")
        return template_id

    @staticmethod
    def _resolve_template_profile(template_id: str) -> dict[str, Decimal | int]:
        try:
            return _PATH_COMBAT_PROFILE_BY_TEMPLATE_ID[template_id]
        except KeyError as exc:
            raise BreakthroughTrialStateError(f"未支持的行为模板画像：{template_id}") from exc

    @staticmethod
    def _resolve_enemy_template_profile(template_id: str) -> dict[str, Decimal | int]:
        try:
            return _ENEMY_TEMPLATE_STAT_PROFILE[template_id]
        except KeyError as exc:
            raise BreakthroughTrialStateError(f"未支持的突破首领模板画像：{template_id}") from exc

    @staticmethod
    def _resolve_enemy_behavior_template_id(template_id: str) -> str:
        try:
            return _ENEMY_BEHAVIOR_TEMPLATE_BY_TEMPLATE_ID[template_id]
        except KeyError as exc:
            raise BreakthroughTrialStateError(f"未配置突破首领行为模板映射：{template_id}") from exc

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
            raise BreakthroughTrialStateError(f"未找到大境界基准系数：{realm_id}") from exc

    def _resolve_stage_multiplier(self, stage_id: str) -> Decimal:
        try:
            return self._stage_multiplier_by_stage_id[stage_id]
        except KeyError as exc:
            raise BreakthroughTrialStateError(f"未找到小阶段倍率：{stage_id}") from exc

    @staticmethod
    def _resolve_battle_seed(*, now: datetime, seed: int | None, trial: BreakthroughTrialDefinition) -> int:
        if seed is not None:
            return seed
        return int(now.timestamp()) * 1009 + trial.order * 37

    @staticmethod
    def _apply_ratio(*, max_value: int, ratio: Decimal) -> int:
        normalized_ratio = max(Decimal("0.0000"), min(Decimal("1.0000"), Decimal(ratio)))
        current_value = _round_decimal_to_int(Decimal(max_value) * normalized_ratio)
        if normalized_ratio > Decimal("0") and current_value <= 0:
            return 1
        return max(0, min(max_value, current_value))

    @staticmethod
    def _extract_battle_outcome(outcome: object) -> BattleOutcome:
        if not isinstance(outcome, BattleOutcome):
            raise BreakthroughTrialStateError(f"自动战斗返回了无效战斗结果：{outcome}")
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

    def _resolve_modified_stat_value(
        self,
        *,
        field_name: str,
        current_value: int,
        delta: int | None,
        multiplier_permille: int | None,
    ) -> int:
        if multiplier_permille is not None:
            scaled_value = (current_value * multiplier_permille + 500) // 1000
            return self._clamp_stat_value(field_name, scaled_value)
        assert delta is not None
        return self._clamp_stat_value(field_name, current_value + delta)

    @staticmethod
    def _clamp_stat_value(field_name: str, value: int) -> int:
        if field_name in {"attack_power", "speed"}:
            return max(1, value)
        if field_name in {"guard_power"}:
            return max(0, value)
        if field_name in {
            "crit_rate_permille",
            "hit_rate_permille",
            "dodge_rate_permille",
            "control_bonus_permille",
            "control_resist_permille",
            "healing_power_permille",
            "shield_power_permille",
            "counter_rate_permille",
            "damage_reduction_permille",
        }:
            return max(0, min(_PERMILLE_STANDARD_MAX, value))
        if field_name in {"crit_damage_bonus_permille", "damage_bonus_permille"}:
            return max(0, min(_PERMILLE_EXTENDED_MAX, value))
        return max(0, value)



def _read_decimal(value: object, *, default: Decimal) -> Decimal:
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



def _read_int(value: object, *, default: int = 0) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    return default



def _round_decimal_to_int(value: Decimal) -> int:
    return int(value.quantize(_DECIMAL_ONE, rounding=ROUND_HALF_UP))


__all__ = [
    "BreakthroughTrialChallengeResult",
    "BreakthroughTrialConflictError",
    "BreakthroughTrialEntrySnapshot",
    "BreakthroughTrialGroupSnapshot",
    "BreakthroughTrialHubSnapshot",
    "BreakthroughTrialNotFoundError",
    "BreakthroughTrialService",
    "BreakthroughTrialServiceError",
    "BreakthroughTrialStateError",
    "BreakthroughTrialUnavailableError",
]
