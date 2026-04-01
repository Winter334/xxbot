"""突破材料秘境应用服务。"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP

from application.battle import (
    AutoBattleRequest,
    AutoBattleService,
    BattleReplayDisplayContext,
    BattleReplayPresentation,
    BattleReplayService,
)
from application.breakthrough.difficulty_service import (
    BreakthroughDynamicDifficultyService,
    BreakthroughDynamicDifficultySnapshot,
)
from application.character.current_attribute_service import CurrentAttributeService
from application.character.growth_service import CharacterGrowthStateError, CharacterNotFoundError
from application.dungeon.endless_service import (
    _ENEMY_BEHAVIOR_TEMPLATE_BY_TEMPLATE_ID,
    _ENEMY_TEMPLATE_STAT_PROFILE,
    _FULL_RESOURCE_VALUE,
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
from infrastructure.config.static import StaticGameConfig, get_static_config
from infrastructure.config.static.models.breakthrough import (
    ActionPatchSelectorDefinition,
    BreakthroughMaterialRequirement,
    BreakthroughTrialDefinition,
    EnvironmentRuleDefinition,
    EnvironmentStatModifierDefinition,
    EnvironmentTemplatePatchDefinition,
)
from infrastructure.db.models import CharacterProgress, DropRecord, InventoryItem
from infrastructure.db.repositories import (
    BattleRecordRepository,
    CharacterAggregate,
    CharacterRepository,
    InventoryRepository,
    StateRepository,
)

_BREAKTHROUGH_MATERIAL_BATTLE_TYPE = "breakthrough_material_trial"
_BREAKTHROUGH_MATERIAL_SOURCE_TYPE = "breakthrough_material_trial"
_BREAKTHROUGH_MATERIAL_ENVIRONMENT_TAG = "breakthrough_material_trial"
_ENDLESS_STATUS_COMPLETED = "completed"
_RUNNING_STATUS = "running"
_DEFAULT_ROUND_LIMIT = 12
_DECIMAL_ONE = Decimal("1")
_DECIMAL_THOUSAND = Decimal("1000")
_PERMILLE_STANDARD_MAX = 1000
_PERMILLE_EXTENDED_MAX = 5000
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
class BreakthroughMaterialDropItem:
    """单条材料秘境掉落结果。"""

    item_type: str
    item_id: str
    item_name: str
    quantity: int
    total_quantity: int
    remaining_missing_quantity: int


@dataclass(frozen=True, slots=True)
class BreakthroughMaterialTrialChallengeResult:
    """单次材料秘境挑战后的稳定返回结构。"""

    character_id: int
    mapping_id: str
    trial_name: str
    battle_outcome: str
    battle_report_id: int | None
    replay_presentation: BattleReplayPresentation | None
    victory: bool
    drop_items: tuple[BreakthroughMaterialDropItem, ...]
    all_satisfied_after: bool
    remaining_gap_summary: str
    environment_snapshot: dict[str, object]
    difficulty_snapshot: BreakthroughDynamicDifficultySnapshot
    drop_record_id: int | None
    source_ref: str | None


class BreakthroughMaterialTrialServiceError(RuntimeError):
    """突破材料秘境应用服务基础异常。"""


class BreakthroughMaterialTrialStateError(BreakthroughMaterialTrialServiceError):
    """突破材料秘境所需角色状态不完整。"""


class BreakthroughMaterialTrialUnavailableError(BreakthroughMaterialTrialServiceError):
    """当前材料秘境不可挑战。"""


class BreakthroughMaterialTrialConflictError(BreakthroughMaterialTrialServiceError):
    """角色当前存在与材料秘境冲突的运行状态。"""


class BreakthroughMaterialTrialService:
    """负责材料秘境战斗、材料配给与战后演出。"""

    def __init__(
        self,
        *,
        state_repository: StateRepository,
        character_repository: CharacterRepository,
        inventory_repository: InventoryRepository,
        battle_record_repository: BattleRecordRepository,
        auto_battle_service: AutoBattleService,
        current_attribute_service: CurrentAttributeService,
        difficulty_service: BreakthroughDynamicDifficultyService | None = None,
        static_config: StaticGameConfig | None = None,
        battle_replay_service: BattleReplayService | None = None,
    ) -> None:
        self._state_repository = state_repository
        self._character_repository = character_repository
        self._inventory_repository = inventory_repository
        self._battle_record_repository = battle_record_repository
        self._auto_battle_service = auto_battle_service
        self._current_attribute_service = current_attribute_service
        self._static_config = static_config or get_static_config()
        self._battle_replay_service = battle_replay_service or BattleReplayService()
        self._difficulty_service = difficulty_service or BreakthroughDynamicDifficultyService(
            current_attribute_service=current_attribute_service,
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
        self._trial_by_mapping_id = {
            trial.mapping_id: trial for trial in self._static_config.breakthrough_trials.trials
        }
        self._group_name_by_id = {
            group.group_id: group.name for group in self._static_config.breakthrough_trials.trial_groups
        }

    def challenge_material_trial(
        self,
        *,
        character_id: int,
        mapping_id: str | None = None,
        seed: int | None = None,
        now: datetime | None = None,
        persist_battle_report: bool = True,
    ) -> BreakthroughMaterialTrialChallengeResult:
        """执行一次突破材料秘境挑战，并在胜利时发放突破材料。"""
        current_time = now or datetime.utcnow()
        aggregate = self._require_character_aggregate(character_id)
        progress = self._require_progress(aggregate)
        trial = self._resolve_trial(current_realm_id=progress.realm_id, mapping_id=mapping_id)
        self._ensure_no_conflict_states(character_id)

        missing_before = self._collect_missing_requirements(character_id=character_id, requirements=trial.required_items)
        if not any(item["missing_quantity"] > 0 for item in missing_before):
            raise BreakthroughMaterialTrialUnavailableError("当前突破材料已齐，无需再入采材秘境。")

        request, difficulty_snapshot = self._build_auto_battle_request(
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
        drop_items: tuple[BreakthroughMaterialDropItem, ...] = ()
        drop_record_id: int | None = None
        source_ref: str | None = None
        if battle_outcome is BattleOutcome.ALLY_VICTORY:
            victory_count_before = self._count_victories(character_id=character_id, mapping_id=trial.mapping_id)
            drop_items = self._apply_material_rewards(
                character_id=character_id,
                trial=trial,
                missing_before=missing_before,
                victory_count_before=victory_count_before,
            )
            source_ref = self._build_source_ref(mapping_id=trial.mapping_id, victory_count_after=victory_count_before + 1)
            drop_record = self._battle_record_repository.add_drop_record(
                self._build_drop_record(
                    character_id=character_id,
                    battle_report_id=execution_result.persisted_battle_report_id,
                    source_ref=source_ref,
                    drop_items=drop_items,
                )
            )
            drop_record_id = drop_record.id

        gap_summary, all_satisfied_after = self._build_gap_summary(
            character_id=character_id,
            requirements=trial.required_items,
        )
        self._character_repository.save_progress(progress)
        replay_presentation = self._build_replay_presentation(
            trial=trial,
            battle_report_id=execution_result.persisted_battle_report_id,
            result=battle_outcome.value,
            summary_payload=execution_result.report_artifacts.summary.to_payload(),
            detail_payload=execution_result.report_artifacts.detail.to_payload(),
        )
        return BreakthroughMaterialTrialChallengeResult(
            character_id=character_id,
            mapping_id=trial.mapping_id,
            trial_name=trial.material_trial_name,
            battle_outcome=battle_outcome.value,
            battle_report_id=execution_result.persisted_battle_report_id,
            replay_presentation=replay_presentation,
            victory=battle_outcome is BattleOutcome.ALLY_VICTORY,
            drop_items=drop_items,
            all_satisfied_after=all_satisfied_after,
            remaining_gap_summary=gap_summary,
            environment_snapshot=dict(request.environment_snapshot or {}),
            difficulty_snapshot=difficulty_snapshot,
            drop_record_id=drop_record_id,
            source_ref=source_ref,
        )

    def _build_auto_battle_request(
        self,
        *,
        aggregate: CharacterAggregate,
        progress: CharacterProgress,
        trial: BreakthroughTrialDefinition,
        seed: int,
    ) -> tuple[AutoBattleRequest, BreakthroughDynamicDifficultySnapshot]:
        environment_rule = self._require_environment_rule(trial.environment_rule_id)
        difficulty_snapshot = self._difficulty_service.resolve_enemy_scale(
            character_id=aggregate.character.id,
            base_scale_permille=trial.material_boss_scale_permille,
        )
        ally_snapshot = self._build_ally_battle_snapshot(aggregate=aggregate, progress=progress)
        enemy_snapshot = self._build_enemy_battle_snapshot(
            trial=trial,
            scale_permille=difficulty_snapshot.adjusted_scale_permille,
        )
        ally_snapshot = self._apply_environment_stat_modifiers(
            snapshot=ally_snapshot,
            modifiers=environment_rule.ally_stat_modifiers,
        )
        enemy_snapshot = self._apply_environment_stat_modifiers(
            snapshot=enemy_snapshot,
            modifiers=environment_rule.enemy_stat_modifiers,
        )
        environment_tags = (
            _BREAKTHROUGH_MATERIAL_ENVIRONMENT_TAG,
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
        request = AutoBattleRequest(
            character_id=aggregate.character.id,
            battle_type=_BREAKTHROUGH_MATERIAL_BATTLE_TYPE,
            snapshot=BattleSnapshot(
                seed=seed,
                allies=(ally_snapshot,),
                enemies=(enemy_snapshot,),
                round_limit=_DEFAULT_ROUND_LIMIT,
                environment_tags=environment_tags,
            ),
            opponent_ref=f"breakthrough_material:{trial.mapping_id}:{trial.boss_template_id}:{trial.boss_stage_id}",
            focus_unit_id=ally_snapshot.unit_id,
            environment_snapshot={
                "mapping_id": trial.mapping_id,
                "group_id": trial.group_id,
                "from_realm_id": trial.from_realm_id,
                "to_realm_id": trial.to_realm_id,
                "material_trial_name": trial.material_trial_name,
                "boss_template_id": trial.boss_template_id,
                "boss_stage_id": trial.boss_stage_id,
                "boss_scale_permille": difficulty_snapshot.adjusted_scale_permille,
                "base_boss_scale_permille": difficulty_snapshot.base_scale_permille,
                "difficulty_adjustment_permille": difficulty_snapshot.adjustment_permille,
                "player_power_ratio_permille": difficulty_snapshot.player_power_ratio_permille,
                "environment_rule_id": environment_rule.rule_id,
                "environment_rule_summary": environment_rule.summary,
                "environment_tags": ",".join(environment_rule.environment_tags),
            },
            template_patches_by_template_id=None if not template_patches_by_template_id else template_patches_by_template_id,
            template_path_id_by_template_id=None if not template_path_id_by_template_id else template_path_id_by_template_id,
        )
        return request, difficulty_snapshot

    def _build_ally_battle_snapshot(
        self,
        *,
        aggregate: CharacterAggregate,
        progress: CharacterProgress,
    ) -> BattleUnitSnapshot:
        del progress
        current_attributes = self._current_attribute_service.get_pve_view(character_id=aggregate.character.id)
        return current_attributes.build_battle_unit_snapshot(
            unit_id=f"character:{aggregate.character.id}",
            unit_name=aggregate.character.name,
            side=BattleSide.ALLY,
        )

    def _build_enemy_battle_snapshot(
        self,
        *,
        trial: BreakthroughTrialDefinition,
        scale_permille: int,
    ) -> BattleUnitSnapshot:
        template_profile = self._resolve_enemy_template_profile(trial.boss_template_id)
        behavior_template_id = self._resolve_enemy_behavior_template_id(trial.boss_template_id)
        scale_factor = Decimal(scale_permille) / _DECIMAL_THOUSAND
        max_hp = self._calculate_base_hp(
            realm_id=trial.from_realm_id,
            stage_id=trial.boss_stage_id,
            factor=scale_factor * _read_decimal(template_profile.get("hp_factor"), default=Decimal("1.0")),
        )
        max_resource = _FULL_RESOURCE_VALUE
        return BattleUnitSnapshot(
            unit_id=f"breakthrough_material_boss:{trial.mapping_id}",
            unit_name=f"{trial.material_trial_name}守境灵影",
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

    def _apply_material_rewards(
        self,
        *,
        character_id: int,
        trial: BreakthroughTrialDefinition,
        missing_before: tuple[dict[str, object], ...],
        victory_count_before: int,
    ) -> tuple[BreakthroughMaterialDropItem, ...]:
        remaining_target_wins = max(1, int(trial.material_target_victory_count) - max(0, victory_count_before))
        drop_items: list[BreakthroughMaterialDropItem] = []
        for entry in missing_before:
            missing_quantity = int(entry["missing_quantity"])
            if missing_quantity <= 0:
                continue
            reward_quantity = min(missing_quantity, _round_up_division(missing_quantity, remaining_target_wins))
            if reward_quantity <= 0:
                continue
            item_type = str(entry["item_type"])
            item_id = str(entry["item_id"])
            existing = self._inventory_repository.get_item(character_id, item_type, item_id)
            next_quantity = reward_quantity if existing is None else max(0, int(existing.quantity)) + reward_quantity
            payload: dict[str, object] = {"bound": True}
            if existing is not None and isinstance(existing.item_payload_json, dict):
                payload = dict(existing.item_payload_json)
                payload["bound"] = True
            saved_item = self._inventory_repository.upsert_item(
                InventoryItem(
                    character_id=character_id,
                    item_type=item_type,
                    item_id=item_id,
                    quantity=next_quantity,
                    item_payload_json=payload,
                )
            )
            remaining_missing_quantity = max(0, int(entry["required_quantity"]) - saved_item.quantity)
            drop_items.append(
                BreakthroughMaterialDropItem(
                    item_type=item_type,
                    item_id=item_id,
                    item_name=str(entry["item_name"]),
                    quantity=reward_quantity,
                    total_quantity=saved_item.quantity,
                    remaining_missing_quantity=remaining_missing_quantity,
                )
            )
        return tuple(drop_items)

    def _collect_missing_requirements(
        self,
        *,
        character_id: int,
        requirements: tuple[BreakthroughMaterialRequirement, ...],
    ) -> tuple[dict[str, object], ...]:
        entries: list[dict[str, object]] = []
        for requirement in requirements:
            owned_item = self._inventory_repository.get_item(
                character_id,
                requirement.item_type,
                requirement.item_id,
            )
            owned_quantity = 0 if owned_item is None else max(0, int(owned_item.quantity))
            required_quantity = max(0, int(requirement.quantity))
            entries.append(
                {
                    "item_type": requirement.item_type,
                    "item_id": requirement.item_id,
                    "item_name": _RESOURCE_NAME_BY_ID.get(requirement.item_id, requirement.item_id),
                    "required_quantity": required_quantity,
                    "owned_quantity": owned_quantity,
                    "missing_quantity": max(0, required_quantity - owned_quantity),
                }
            )
        return tuple(entries)

    def _build_gap_summary(
        self,
        *,
        character_id: int,
        requirements: tuple[BreakthroughMaterialRequirement, ...],
    ) -> tuple[str, bool]:
        entries = self._collect_missing_requirements(character_id=character_id, requirements=requirements)
        missing_lines = [
            f"{entry['item_name']} ×{entry['missing_quantity']}"
            for entry in entries
            if int(entry["missing_quantity"]) > 0
        ]
        if not missing_lines:
            return "已无缺漏", True
        return "；".join(missing_lines), False

    def _build_replay_presentation(
        self,
        *,
        trial: BreakthroughTrialDefinition,
        battle_report_id: int | None,
        result: str,
        summary_payload: Mapping[str, object],
        detail_payload: Mapping[str, object],
    ) -> BattleReplayPresentation | None:
        if battle_report_id is None:
            return None
        return self._battle_replay_service.build_presentation(
            battle_report_id=battle_report_id,
            result=result,
            summary_payload=summary_payload,
            detail_payload=detail_payload,
            context=BattleReplayDisplayContext(
                source_name="采材行记",
                scene_name=trial.material_trial_name,
                group_name=self._group_name_by_id.get(trial.group_id),
                environment_name=trial.environment_rule,
                focus_unit_name=None,
            ),
        )

    def _count_victories(self, *, character_id: int, mapping_id: str) -> int:
        prefix = f"breakthrough_material:{mapping_id}:victory:"
        count = 0
        for record in self._battle_record_repository.list_drop_records(character_id):
            if record.source_type != _BREAKTHROUGH_MATERIAL_SOURCE_TYPE:
                continue
            if isinstance(record.source_ref, str) and record.source_ref.startswith(prefix):
                count += 1
        return count

    @staticmethod
    def _build_source_ref(*, mapping_id: str, victory_count_after: int) -> str:
        return f"breakthrough_material:{mapping_id}:victory:{victory_count_after}"

    def _build_drop_record(
        self,
        *,
        character_id: int,
        battle_report_id: int | None,
        source_ref: str,
        drop_items: tuple[BreakthroughMaterialDropItem, ...],
    ) -> DropRecord:
        return DropRecord(
            character_id=character_id,
            battle_report_id=battle_report_id,
            source_type=_BREAKTHROUGH_MATERIAL_SOURCE_TYPE,
            source_ref=source_ref,
            items_json=[
                {
                    "reward_kind": "material",
                    "item_type": item.item_type,
                    "item_id": item.item_id,
                    "quantity": item.quantity,
                    "bound": True,
                }
                for item in drop_items
            ],
            currencies_json={},
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
        patch_map: dict[str, tuple[AuxiliarySkillParameterPatch, ...]] = {}
        ally_patches = tuple(self._build_template_patch(item) for item in environment_rule.ally_template_patches)
        enemy_patches = tuple(self._build_template_patch(item) for item in environment_rule.enemy_template_patches)
        if ally_patches:
            patch_map[ally_template_id] = ally_patches
        if enemy_patches:
            patch_map[enemy_template_id] = patch_map.get(enemy_template_id, ()) + enemy_patches
        return patch_map

    def _build_template_patch(self, definition: EnvironmentTemplatePatchDefinition) -> AuxiliarySkillParameterPatch:
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
        updated = snapshot
        for modifier in modifiers:
            if not hasattr(updated, modifier.stat_field):
                raise BreakthroughMaterialTrialStateError(f"环境修正引用了未知战斗属性：{modifier.stat_field}")
            current_value = getattr(updated, modifier.stat_field)
            if not isinstance(current_value, int):
                raise BreakthroughMaterialTrialStateError(f"环境修正目标不是整数字段：{modifier.stat_field}")
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
            updated = replace(updated, **{modifier.stat_field: next_value})
        return updated

    def _ensure_no_conflict_states(self, character_id: int) -> None:
        endless_state = self._state_repository.get_endless_run_state(character_id)
        if endless_state is not None and endless_state.status != _ENDLESS_STATUS_COMPLETED:
            raise BreakthroughMaterialTrialConflictError(f"角色存在未结束的无尽副本运行：{character_id}")
        retreat_state = self._state_repository.get_retreat_state(character_id)
        if retreat_state is not None and retreat_state.status == _RUNNING_STATUS and retreat_state.settled_at is None:
            raise BreakthroughMaterialTrialConflictError(f"角色当前处于闭关中：{character_id}")
        healing_state = self._state_repository.get_healing_state(character_id)
        if healing_state is not None and healing_state.status == _RUNNING_STATUS and healing_state.settled_at is None:
            raise BreakthroughMaterialTrialConflictError(f"角色当前处于疗伤中：{character_id}")

    def _resolve_trial(self, *, current_realm_id: str, mapping_id: str | None) -> BreakthroughTrialDefinition:
        if mapping_id is None:
            trial = self._static_config.breakthrough_trials.get_trial_by_from_realm_id(current_realm_id)
            if trial is None:
                raise BreakthroughMaterialTrialUnavailableError(f"当前大境界不存在可用的材料秘境：{current_realm_id}")
            return trial
        trial = self._trial_by_mapping_id.get(mapping_id)
        if trial is None:
            raise BreakthroughMaterialTrialUnavailableError(f"未定义的突破材料秘境映射：{mapping_id}")
        if trial.from_realm_id != current_realm_id:
            raise BreakthroughMaterialTrialUnavailableError(f"当前不可进入该材料秘境：{mapping_id}")
        return trial

    def _require_environment_rule(self, rule_id: str) -> EnvironmentRuleDefinition:
        rule = self._static_config.breakthrough_trials.get_environment_rule(rule_id)
        if rule is None:
            raise BreakthroughMaterialTrialStateError(f"材料秘境缺少环境规则：{rule_id}")
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
    def _resolve_enemy_template_profile(template_id: str) -> dict[str, Decimal | int]:
        try:
            return _ENEMY_TEMPLATE_STAT_PROFILE[template_id]
        except KeyError as exc:
            raise BreakthroughMaterialTrialStateError(f"未支持的材料秘境守境模板画像：{template_id}") from exc

    @staticmethod
    def _resolve_enemy_behavior_template_id(template_id: str) -> str:
        try:
            return _ENEMY_BEHAVIOR_TEMPLATE_BY_TEMPLATE_ID[template_id]
        except KeyError as exc:
            raise BreakthroughMaterialTrialStateError(f"未配置材料秘境守境行为模板映射：{template_id}") from exc

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
            raise BreakthroughMaterialTrialStateError(f"未找到大境界基准系数：{realm_id}") from exc

    def _resolve_stage_multiplier(self, stage_id: str) -> Decimal:
        try:
            return self._stage_multiplier_by_stage_id[stage_id]
        except KeyError as exc:
            raise BreakthroughMaterialTrialStateError(f"未找到小阶段倍率：{stage_id}") from exc

    @staticmethod
    def _resolve_battle_seed(*, now: datetime, seed: int | None, trial: BreakthroughTrialDefinition) -> int:
        if seed is not None:
            return seed
        return int(now.timestamp()) * 1013 + trial.order * 53

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
            raise BreakthroughMaterialTrialStateError(f"自动战斗返回了无效战斗结果：{outcome}")
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



def _round_up_division(value: int, divisor: int) -> int:
    return (max(0, int(value)) + max(1, int(divisor)) - 1) // max(1, int(divisor))


__all__ = [
    "BreakthroughMaterialDropItem",
    "BreakthroughMaterialTrialChallengeResult",
    "BreakthroughMaterialTrialConflictError",
    "BreakthroughMaterialTrialService",
    "BreakthroughMaterialTrialServiceError",
    "BreakthroughMaterialTrialStateError",
    "BreakthroughMaterialTrialUnavailableError",
]
