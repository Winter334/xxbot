"""角色当前属性聚合服务。"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from domain.battle import (
    ActionNumericBonusPatch,
    ActionNumericField,
    ActionMultiplierPatch,
    ActionPatchSelector,
    ActionThresholdField,
    ActionThresholdShiftPatch,
    ActionTriggerCapAdjustment,
    AuxiliarySkillParameterPatch,
    BattleSide,
    BattleUnitSnapshot,
)
from domain.equipment import (
    EquipmentAffixValue,
    EquipmentAttributeValue,
    EquipmentItem as DomainEquipmentItem,
    EquipmentSpecialEffectValue,
    scale_special_effect_payload,
)
from infrastructure.config.static import StaticGameConfig, get_static_config
from infrastructure.db.models import CharacterProgress, EquipmentItem as EquipmentItemModel
from infrastructure.db.repositories import (
    CharacterAggregate,
    CharacterRepository,
    SkillRepository,
    SqlAlchemySkillRepository,
)

from application.character.skill_runtime_support import CharacterSkillLoadoutSnapshot, SkillRuntimeSupport

_DEFAULT_HERO_TEMPLATE_ID = "zhanqing_sword"
_FULL_RESOURCE_VALUE = 100
_SCENE_NEUTRAL = "neutral"
_SCENE_PVE = "pve"
_SCENE_PVP = "pvp"
_ACTIVE_ITEM_STATE = "active"
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


@dataclass(frozen=True, slots=True)
class CurrentAttributeSnapshot:
    """角色当前属性快照。"""

    scene: str
    character_id: int
    character_name: str
    realm_id: str
    stage_id: str
    main_axis_id: str
    main_path_id: str
    behavior_template_id: str
    current_hp_ratio: Decimal
    current_mp_ratio: Decimal
    max_hp: int
    attack_power: int
    guard_power: int
    speed: int
    crit_rate_permille: int
    crit_damage_bonus_permille: int
    hit_rate_permille: int
    dodge_rate_permille: int
    control_bonus_permille: int
    control_resist_permille: int
    healing_power_permille: int
    shield_power_permille: int
    damage_bonus_permille: int
    damage_reduction_permille: int
    counter_rate_permille: int
    special_effect_payloads: tuple[dict[str, Any], ...]
    template_patches: tuple[AuxiliarySkillParameterPatch, ...]
    template_patch_payloads: tuple[dict[str, Any], ...]
    skill_loadout: CharacterSkillLoadoutSnapshot

    def build_battle_unit_snapshot(
        self,
        *,
        unit_id: str,
        unit_name: str,
        side: BattleSide,
        current_hp_ratio: Decimal | None = None,
        current_mp_ratio: Decimal | None = None,
        current_shield: int = 0,
        runtime_template_id: str | None = None,
    ) -> BattleUnitSnapshot:
        """把当前属性快照转换为战斗单位快照。"""
        resolved_hp_ratio = self.current_hp_ratio if current_hp_ratio is None else Decimal(current_hp_ratio)
        resolved_mp_ratio = self.current_mp_ratio if current_mp_ratio is None else Decimal(current_mp_ratio)
        max_resource = _FULL_RESOURCE_VALUE
        return BattleUnitSnapshot(
            unit_id=unit_id,
            unit_name=unit_name,
            side=side,
            behavior_template_id=runtime_template_id or self.behavior_template_id,
            realm_id=self.realm_id,
            stage_id=self.stage_id,
            max_hp=self.max_hp,
            current_hp=_apply_ratio(max_value=self.max_hp, ratio=resolved_hp_ratio),
            current_shield=max(0, current_shield),
            max_resource=max_resource,
            current_resource=_apply_ratio(max_value=max_resource, ratio=resolved_mp_ratio),
            attack_power=self.attack_power,
            guard_power=self.guard_power,
            speed=self.speed,
            crit_rate_permille=self.crit_rate_permille,
            crit_damage_bonus_permille=self.crit_damage_bonus_permille,
            hit_rate_permille=self.hit_rate_permille,
            dodge_rate_permille=self.dodge_rate_permille,
            control_bonus_permille=self.control_bonus_permille,
            control_resist_permille=self.control_resist_permille,
            healing_power_permille=self.healing_power_permille,
            shield_power_permille=self.shield_power_permille,
            damage_bonus_permille=self.damage_bonus_permille,
            damage_reduction_permille=self.damage_reduction_permille,
            counter_rate_permille=self.counter_rate_permille,
            special_effect_payloads=tuple(dict(payload) for payload in self.special_effect_payloads),
        )

    def build_template_patches_by_template_id(
        self,
        *,
        runtime_template_id: str | None = None,
    ) -> dict[str, tuple[AuxiliarySkillParameterPatch, ...]]:
        """返回当前角色需要应用的模板补丁映射。"""
        if not self.template_patches:
            return {}
        return {runtime_template_id or self.behavior_template_id: self.template_patches}

    def build_template_path_id_by_template_id(
        self,
        *,
        runtime_template_id: str | None = None,
    ) -> dict[str, str]:
        """返回运行期模板标识到主修流派标识的映射。"""
        return {runtime_template_id or self.behavior_template_id: self.main_path_id}

    def to_stats_payload(
        self,
        *,
        current_hp_ratio: Decimal | None = None,
        current_mp_ratio: Decimal | None = None,
        current_shield: int = 0,
        runtime_template_id: str | None = None,
    ) -> dict[str, Any]:
        """导出可直接写入快照的属性字典。"""
        resolved_hp_ratio = self.current_hp_ratio if current_hp_ratio is None else Decimal(current_hp_ratio)
        resolved_mp_ratio = self.current_mp_ratio if current_mp_ratio is None else Decimal(current_mp_ratio)
        max_resource = _FULL_RESOURCE_VALUE
        return {
            "scene": self.scene,
            "behavior_template_id": runtime_template_id or self.behavior_template_id,
            "template_path_id": self.main_path_id,
            "realm_id": self.realm_id,
            "stage_id": self.stage_id,
            "max_hp": self.max_hp,
            "current_hp": _apply_ratio(max_value=self.max_hp, ratio=resolved_hp_ratio),
            "current_shield": max(0, current_shield),
            "max_resource": max_resource,
            "current_resource": _apply_ratio(max_value=max_resource, ratio=resolved_mp_ratio),
            "attack_power": self.attack_power,
            "guard_power": self.guard_power,
            "speed": self.speed,
            "crit_rate_permille": self.crit_rate_permille,
            "crit_damage_bonus_permille": self.crit_damage_bonus_permille,
            "hit_rate_permille": self.hit_rate_permille,
            "dodge_rate_permille": self.dodge_rate_permille,
            "control_bonus_permille": self.control_bonus_permille,
            "control_resist_permille": self.control_resist_permille,
            "healing_power_permille": self.healing_power_permille,
            "shield_power_permille": self.shield_power_permille,
            "damage_bonus_permille": self.damage_bonus_permille,
            "damage_reduction_permille": self.damage_reduction_permille,
            "counter_rate_permille": self.counter_rate_permille,
            "special_effect_payloads": [dict(payload) for payload in self.special_effect_payloads],
            "template_patch_payloads": [dict(payload) for payload in self.template_patch_payloads],
        }


class CurrentAttributeServiceError(RuntimeError):
    """当前属性服务基础异常。"""


class CurrentAttributeStateError(CurrentAttributeServiceError):
    """角色当前属性依赖状态不完整。"""


class CurrentAttributeService:
    """汇总成长、功法、装备与法宝，作为当前属性唯一入口。"""

    def __init__(
        self,
        *,
        character_repository: CharacterRepository,
        skill_repository: SkillRepository | None = None,
        static_config: StaticGameConfig | None = None,
        skill_runtime_support: SkillRuntimeSupport | None = None,
    ) -> None:
        self._character_repository = character_repository
        self._skill_repository = skill_repository or self._build_fallback_skill_repository(character_repository)
        self._static_config = static_config or get_static_config()
        self._skill_runtime_support = skill_runtime_support or SkillRuntimeSupport(
            character_repository=character_repository,
            skill_repository=self._skill_repository,
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

    def get_neutral_view(self, *, character_id: int) -> CurrentAttributeSnapshot:
        """返回中性场景当前属性。"""
        return self._build_snapshot(character_id=character_id, scene=_SCENE_NEUTRAL)

    def get_pve_view(self, *, character_id: int) -> CurrentAttributeSnapshot:
        """返回 PVE 场景当前属性。"""
        return self._build_snapshot(character_id=character_id, scene=_SCENE_PVE)

    def get_pvp_view(self, *, character_id: int) -> CurrentAttributeSnapshot:
        """返回 PVP 场景当前属性。"""
        return self._build_snapshot(character_id=character_id, scene=_SCENE_PVP)

    @staticmethod
    def serialize_template_patches(
        patches: tuple[AuxiliarySkillParameterPatch, ...],
    ) -> tuple[dict[str, Any], ...]:
        """把模板补丁对象转换为可落库 JSON 载荷。"""
        return tuple(_serialize_template_patch(patch) for patch in patches)

    @staticmethod
    def deserialize_template_patches(
        payloads: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None,
    ) -> tuple[AuxiliarySkillParameterPatch, ...]:
        """从 JSON 载荷恢复模板补丁对象。"""
        normalized_payloads = [] if payloads is None else [dict(payload) for payload in payloads if isinstance(payload, dict)]
        patches: list[AuxiliarySkillParameterPatch] = []
        for payload in normalized_payloads:
            patch_id = str(payload.get("patch_id") or "").strip()
            patch_name = str(payload.get("patch_name") or patch_id).strip() or patch_id
            if not patch_id:
                continue
            patches.append(
                AuxiliarySkillParameterPatch(
                    patch_id=patch_id,
                    patch_name=patch_name,
                    numeric_bonuses=tuple(
                        ActionNumericBonusPatch(
                            field=ActionNumericField(item["field"]),
                            delta=_read_int(item.get("delta")),
                            selector=_deserialize_selector(item),
                        )
                        for item in _normalize_mapping_list(payload.get("numeric_bonuses"))
                        if str(item.get("field") or "").strip()
                    ),
                    multipliers=tuple(
                        ActionMultiplierPatch(
                            field=ActionNumericField(item["field"]),
                            multiplier_permille=_read_int(item.get("multiplier_permille"), default=1000),
                            selector=_deserialize_selector(item),
                        )
                        for item in _normalize_mapping_list(payload.get("multipliers"))
                        if str(item.get("field") or "").strip()
                    ),
                    threshold_shifts=tuple(
                        ActionThresholdShiftPatch(
                            field=ActionThresholdField(item["field"]),
                            delta=_read_int(item.get("delta")),
                            selector=_deserialize_selector(item),
                        )
                        for item in _normalize_mapping_list(payload.get("threshold_shifts"))
                        if str(item.get("field") or "").strip()
                    ),
                    trigger_cap_adjustments=tuple(
                        ActionTriggerCapAdjustment(
                            delta=_read_int(item.get("delta")),
                            selector=_deserialize_selector(item),
                        )
                        for item in _normalize_mapping_list(payload.get("trigger_cap_adjustments"))
                    ),
                )
            )
        return tuple(patches)

    def _build_snapshot(self, *, character_id: int, scene: str) -> CurrentAttributeSnapshot:
        aggregate = self._require_aggregate(character_id)
        progress = self._require_progress(aggregate)
        skill_state = self._skill_runtime_support.ensure_skill_state(character_id=character_id)
        loadout_snapshot = skill_state.loadout_snapshot
        profile = self._resolve_template_profile(loadout_snapshot.behavior_template_id)
        stat_values = self._build_base_profile_stats(progress=progress, profile=profile)
        self._apply_skill_item_attributes(
            stat_values=stat_values,
            skill_items=(
                skill_state.items_by_id[loadout_snapshot.main_skill.item_id],
                skill_state.items_by_id[loadout_snapshot.guard_skill.item_id],
                skill_state.items_by_id[loadout_snapshot.movement_skill.item_id],
                skill_state.items_by_id[loadout_snapshot.spirit_skill.item_id],
            ),
        )
        self._apply_equipment_attributes(
            stat_values=stat_values,
            equipment_models=aggregate.equipment_items,
            scene=scene,
        )
        clamped_values = self._clamp_stat_values(stat_values)
        template_patches = self._collect_template_patches(skill_state=skill_state, loadout=loadout_snapshot)
        template_patch_payloads = self.serialize_template_patches(template_patches)
        special_effect_payloads = self._collect_special_effect_payloads(
            equipment_models=aggregate.equipment_items,
            scene=scene,
        )
        return CurrentAttributeSnapshot(
            scene=scene,
            character_id=aggregate.character.id,
            character_name=aggregate.character.name,
            realm_id=progress.realm_id,
            stage_id=progress.stage_id,
            main_axis_id=loadout_snapshot.main_axis_id,
            main_path_id=loadout_snapshot.main_path_id,
            behavior_template_id=loadout_snapshot.behavior_template_id,
            current_hp_ratio=Decimal(progress.current_hp_ratio),
            current_mp_ratio=Decimal(progress.current_mp_ratio),
            max_hp=max(1, clamped_values.get("max_hp", 1)),
            attack_power=max(1, clamped_values.get("attack_power", 1)),
            guard_power=max(0, clamped_values.get("guard_power", 0)),
            speed=max(1, clamped_values.get("speed", 1)),
            crit_rate_permille=max(0, clamped_values.get("crit_rate_permille", 0)),
            crit_damage_bonus_permille=max(0, clamped_values.get("crit_damage_bonus_permille", 0)),
            hit_rate_permille=max(0, clamped_values.get("hit_rate_permille", 1000)),
            dodge_rate_permille=max(0, clamped_values.get("dodge_rate_permille", 0)),
            control_bonus_permille=max(0, clamped_values.get("control_bonus_permille", 0)),
            control_resist_permille=max(0, clamped_values.get("control_resist_permille", 0)),
            healing_power_permille=max(0, clamped_values.get("healing_power_permille", 0)),
            shield_power_permille=max(0, clamped_values.get("shield_power_permille", 0)),
            damage_bonus_permille=max(0, clamped_values.get("damage_bonus_permille", 0)),
            damage_reduction_permille=max(0, clamped_values.get("damage_reduction_permille", 0)),
            counter_rate_permille=max(0, clamped_values.get("counter_rate_permille", 0)),
            special_effect_payloads=special_effect_payloads,
            template_patches=template_patches,
            template_patch_payloads=template_patch_payloads,
            skill_loadout=loadout_snapshot,
        )

    def _build_base_profile_stats(
        self,
        *,
        progress: CharacterProgress,
        profile: dict[str, Decimal | int],
    ) -> dict[str, int]:
        max_hp = self._calculate_base_hp(
            realm_id=progress.realm_id,
            stage_id=progress.stage_id,
            factor=_read_decimal(profile.get("hp_factor"), default=Decimal("1.0")),
        )
        return {
            "max_hp": max_hp,
            "attack_power": self._calculate_base_attack(
                realm_id=progress.realm_id,
                stage_id=progress.stage_id,
                factor=_read_decimal(profile.get("attack_factor"), default=Decimal("1.0")),
            ),
            "guard_power": self._calculate_base_guard(
                realm_id=progress.realm_id,
                stage_id=progress.stage_id,
                factor=_read_decimal(profile.get("guard_factor"), default=Decimal("1.0")),
            ),
            "speed": self._calculate_base_speed(
                realm_id=progress.realm_id,
                stage_id=progress.stage_id,
                factor=_read_decimal(profile.get("speed_factor"), default=Decimal("1.0")),
            ),
            "crit_rate_permille": _read_int(profile.get("crit_rate_permille"), default=0),
            "crit_damage_bonus_permille": _read_int(profile.get("crit_damage_bonus_permille"), default=0),
            "hit_rate_permille": _read_int(profile.get("hit_rate_permille"), default=1000),
            "dodge_rate_permille": _read_int(profile.get("dodge_rate_permille"), default=0),
            "control_bonus_permille": _read_int(profile.get("control_bonus_permille"), default=0),
            "control_resist_permille": _read_int(profile.get("control_resist_permille"), default=0),
            "healing_power_permille": _read_int(profile.get("healing_power_permille"), default=0),
            "shield_power_permille": _read_int(profile.get("shield_power_permille"), default=0),
            "damage_bonus_permille": _read_int(profile.get("damage_bonus_permille"), default=0),
            "damage_reduction_permille": _read_int(profile.get("damage_reduction_permille"), default=0),
            "counter_rate_permille": _read_int(profile.get("counter_rate_permille"), default=0),
        }

    def _apply_skill_item_attributes(
        self,
        *,
        stat_values: dict[str, int],
        skill_items: tuple,
    ) -> None:
        for skill_item in skill_items:
            resolved_values = self._skill_runtime_support.collect_resolved_attribute_values(item=skill_item)
            for stat_id, value in resolved_values.items():
                target_field = _CURRENT_ATTRIBUTE_FIELD_BY_STAT_ID.get(stat_id)
                if target_field is None:
                    continue
                stat_values[target_field] = stat_values.get(target_field, 0) + value

    def _apply_equipment_attributes(
        self,
        *,
        stat_values: dict[str, int],
        equipment_models,
        scene: str,
    ) -> None:
        for equipment_model in equipment_models:
            if equipment_model.item_state != _ACTIVE_ITEM_STATE or equipment_model.equipped_slot_id is None:
                continue
            domain_item = self._to_domain_item(equipment_model)
            for attribute in domain_item.base_attributes:
                target_field = _CURRENT_ATTRIBUTE_FIELD_BY_STAT_ID.get(attribute.stat_id)
                if target_field is None:
                    continue
                stat_values[target_field] = stat_values.get(target_field, 0) + attribute.resolved_value(domain_item.base_stat_bonus_ratio)
            for affix in domain_item.affixes:
                if affix.is_pve_specialized and scene != _SCENE_PVE:
                    continue
                if affix.is_pvp_specialized and scene != _SCENE_PVP:
                    continue
                target_field = _CURRENT_ATTRIBUTE_FIELD_BY_STAT_ID.get(affix.stat_id)
                if target_field is None:
                    continue
                stat_values[target_field] = stat_values.get(target_field, 0) + affix.resolved_value(domain_item.affix_bonus_ratio)

    def _collect_special_effect_payloads(
        self,
        *,
        equipment_models,
        scene: str,
    ) -> tuple[dict[str, Any], ...]:
        payloads: list[dict[str, Any]] = []
        for equipment_model in equipment_models:
            if equipment_model.item_state != _ACTIVE_ITEM_STATE or equipment_model.equipped_slot_id is None:
                continue
            domain_item = self._to_domain_item(equipment_model)
            for affix in domain_item.affixes:
                if affix.special_effect is None:
                    continue
                if affix.is_pve_specialized and scene != _SCENE_PVE:
                    continue
                if affix.is_pvp_specialized and scene != _SCENE_PVP:
                    continue
                payloads.append(self._build_special_effect_payload(item=domain_item, affix=affix))
        return tuple(payloads)

    @staticmethod
    def _build_special_effect_payload(*, item: DomainEquipmentItem, affix: EquipmentAffixValue) -> dict[str, Any]:
        assert affix.special_effect is not None
        scaled_payload = dict(scale_special_effect_payload(quality_id=item.quality_id, payload=affix.special_effect.payload))
        return {
            "affix_id": affix.affix_id,
            "affix_name": affix.affix_name,
            "tier_id": affix.tier_id,
            "slot_id": item.slot_id,
            "quality_id": item.quality_id,
            "is_artifact": item.is_artifact,
            "effect_id": affix.special_effect.effect_id,
            "effect_name": affix.special_effect.effect_name,
            "effect_type": affix.special_effect.effect_type,
            "trigger_event": affix.special_effect.trigger_event,
            "payload": scaled_payload,
            "public_score_key": affix.special_effect.public_score_key,
            "hidden_pvp_score_key": affix.special_effect.hidden_pvp_score_key,
        }

    def _collect_template_patches(
        self,
        *,
        skill_state,
        loadout: CharacterSkillLoadoutSnapshot,
    ) -> tuple[AuxiliarySkillParameterPatch, ...]:
        ordered_items = (
            skill_state.items_by_id[loadout.guard_skill.item_id],
            skill_state.items_by_id[loadout.movement_skill.item_id],
            skill_state.items_by_id[loadout.spirit_skill.item_id],
        )
        patches: list[AuxiliarySkillParameterPatch] = []
        for item in ordered_items:
            patches.extend(self._skill_runtime_support.build_template_patches(item=item))
        return tuple(patches)

    def _clamp_stat_values(self, stat_values: dict[str, int]) -> dict[str, int]:
        return {
            "max_hp": max(1, stat_values.get("max_hp", 1)),
            "attack_power": max(1, stat_values.get("attack_power", 1)),
            "guard_power": max(0, stat_values.get("guard_power", 0)),
            "speed": max(1, stat_values.get("speed", 1)),
            "crit_rate_permille": _clamp(
                stat_values.get("crit_rate_permille", 0),
                0,
                _cap_to_permille(self._static_config.base_coefficients.scalar.crit_rate_cap),
            ),
            "crit_damage_bonus_permille": _clamp(stat_values.get("crit_damage_bonus_permille", 0), 0, 5000),
            "hit_rate_permille": _clamp(stat_values.get("hit_rate_permille", 1000), 0, 1000),
            "dodge_rate_permille": _clamp(
                stat_values.get("dodge_rate_permille", 0),
                0,
                _cap_to_permille(self._static_config.base_coefficients.scalar.dodge_rate_cap),
            ),
            "control_bonus_permille": _clamp(
                stat_values.get("control_bonus_permille", 0),
                0,
                _cap_to_permille(self._static_config.base_coefficients.scalar.control_rate_cap),
            ),
            "control_resist_permille": _clamp(stat_values.get("control_resist_permille", 0), 0, 1000),
            "healing_power_permille": _clamp(stat_values.get("healing_power_permille", 0), 0, 5000),
            "shield_power_permille": _clamp(stat_values.get("shield_power_permille", 0), 0, 5000),
            "damage_bonus_permille": _clamp(stat_values.get("damage_bonus_permille", 0), 0, 5000),
            "damage_reduction_permille": _clamp(
                stat_values.get("damage_reduction_permille", 0),
                0,
                _cap_to_permille(self._static_config.base_coefficients.scalar.damage_reduction_cap),
            ),
            "counter_rate_permille": _clamp(stat_values.get("counter_rate_permille", 0), 0, 1000),
        }

    @staticmethod
    def _to_domain_item(equipment_model: EquipmentItemModel) -> DomainEquipmentItem:
        enhancement = equipment_model.enhancement
        nurture_state = equipment_model.artifact_nurture_state
        base_snapshot_json = equipment_model.base_snapshot_json if isinstance(equipment_model.base_snapshot_json, dict) else {}
        base_attributes = tuple(
            EquipmentAttributeValue(
                stat_id=str(attribute_payload.get("stat_id", "")),
                value=int(attribute_payload.get("value", 0)),
            )
            for attribute_payload in base_snapshot_json.get("base_attributes", [])
            if isinstance(attribute_payload, dict)
        )
        affixes = tuple(
            EquipmentAffixValue(
                affix_id=affix.affix_id,
                affix_name=affix.affix_name,
                stat_id=affix.stat_id,
                category=affix.category,
                tier_id=affix.tier_id,
                tier_name=affix.tier_name,
                rolled_multiplier=affix.roll_value,
                value=affix.value,
                is_pve_specialized=affix.is_pve_specialized,
                is_pvp_specialized=affix.is_pvp_specialized,
                affix_kind=affix.affix_kind,
                special_effect=None
                if affix.special_effect_id is None
                else EquipmentSpecialEffectValue(
                    effect_id=affix.special_effect_id,
                    effect_name=affix.special_effect_name or affix.special_effect_id,
                    effect_type=affix.special_effect_type or "unknown",
                    trigger_event=affix.trigger_event or "unknown",
                    payload=dict(affix.special_effect_payload_json),
                    public_score_key=affix.public_score_key,
                    hidden_pvp_score_key=affix.hidden_pvp_score_key,
                ),
            )
            for affix in equipment_model.affixes
        )
        quality_id = equipment_model.quality_id
        quality = get_static_config().equipment.get_quality(quality_id)
        quality_name = quality.name if quality is not None else (equipment_model.quality_name or quality_id)
        return DomainEquipmentItem(
            slot_id=equipment_model.slot_id,
            slot_name=equipment_model.slot_name,
            quality_id=quality_id,
            quality_name=quality_name,
            template_id=equipment_model.template_id,
            template_name=equipment_model.template_name,
            rank_id=equipment_model.rank_id or "mortal",
            rank_name=equipment_model.rank_name or "一阶",
            rank_order=equipment_model.rank_order or 1,
            mapped_realm_id=equipment_model.mapped_realm_id or "mortal",
            is_artifact=equipment_model.is_artifact,
            resonance_name=equipment_model.resonance_name,
            enhancement_level=0 if enhancement is None else max(0, enhancement.enhancement_level),
            artifact_nurture_level=0 if nurture_state is None else max(0, nurture_state.nurture_level),
            base_attributes=base_attributes,
            affixes=affixes,
            enhancement_base_stat_bonus_ratio=Decimal("0") if enhancement is None else enhancement.base_stat_bonus_ratio,
            enhancement_affix_bonus_ratio=Decimal("0") if enhancement is None else enhancement.affix_bonus_ratio,
            nurture_base_stat_bonus_ratio=Decimal("0") if nurture_state is None else nurture_state.base_stat_bonus_ratio,
            nurture_affix_bonus_ratio=Decimal("0") if nurture_state is None else nurture_state.affix_bonus_ratio,
        )

    @staticmethod
    def _build_fallback_skill_repository(character_repository: CharacterRepository) -> SkillRepository:
        session = getattr(character_repository, "_session", None)
        if session is None:
            raise ValueError("CurrentAttributeService 缺少 skill_repository，且无法从 character_repository 推导会话")
        return SqlAlchemySkillRepository(session)

    def _require_aggregate(self, character_id: int) -> CharacterAggregate:
        aggregate = self._character_repository.get_aggregate(character_id)
        if aggregate is None:
            raise CurrentAttributeStateError(f"角色不存在：{character_id}")
        return aggregate

    @staticmethod
    def _require_progress(aggregate: CharacterAggregate) -> CharacterProgress:
        if aggregate.progress is None:
            raise CurrentAttributeStateError(f"角色缺少成长状态：{aggregate.character.id}")
        return aggregate.progress

    def _resolve_template_profile(self, template_id: str) -> dict[str, Decimal | int]:
        if template_id in _PATH_COMBAT_PROFILE_BY_TEMPLATE_ID:
            return _PATH_COMBAT_PROFILE_BY_TEMPLATE_ID[template_id]
        return _PATH_COMBAT_PROFILE_BY_TEMPLATE_ID[_DEFAULT_HERO_TEMPLATE_ID]

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
            raise CurrentAttributeStateError(f"未找到大境界基准系数：{realm_id}") from exc

    def _resolve_stage_multiplier(self, stage_id: str) -> Decimal:
        try:
            return self._stage_multiplier_by_stage_id[stage_id]
        except KeyError as exc:
            raise CurrentAttributeStateError(f"未找到小阶段倍率：{stage_id}") from exc


_CURRENT_ATTRIBUTE_FIELD_BY_STAT_ID: dict[str, str] = {
    "max_hp": "max_hp",
    "attack_power": "attack_power",
    "guard_power": "guard_power",
    "speed": "speed",
    "crit_rate_permille": "crit_rate_permille",
    "crit_damage_bonus_permille": "crit_damage_bonus_permille",
    "hit_rate_permille": "hit_rate_permille",
    "dodge_rate_permille": "dodge_rate_permille",
    "control_hit_permille": "control_bonus_permille",
    "control_resist_permille": "control_resist_permille",
    "heal_power": "healing_power_permille",
    "shield_power_permille": "shield_power_permille",
    "damage_bonus_permille": "damage_bonus_permille",
    "damage_reduction_permille": "damage_reduction_permille",
    "counter_rate_permille": "counter_rate_permille",
}


def _serialize_template_patch(patch: AuxiliarySkillParameterPatch) -> dict[str, Any]:
    return {
        "patch_id": patch.patch_id,
        "patch_name": patch.patch_name,
        "numeric_bonuses": [
            {
                "field": bonus.field.value,
                "delta": bonus.delta,
                "action_ids": list(bonus.selector.action_ids),
                "required_labels": list(bonus.selector.required_labels),
            }
            for bonus in patch.numeric_bonuses
        ],
        "multipliers": [
            {
                "field": multiplier.field.value,
                "multiplier_permille": multiplier.multiplier_permille,
                "action_ids": list(multiplier.selector.action_ids),
                "required_labels": list(multiplier.selector.required_labels),
            }
            for multiplier in patch.multipliers
        ],
        "threshold_shifts": [
            {
                "field": shift.field.value,
                "delta": shift.delta,
                "action_ids": list(shift.selector.action_ids),
                "required_labels": list(shift.selector.required_labels),
            }
            for shift in patch.threshold_shifts
        ],
        "trigger_cap_adjustments": [
            {
                "delta": adjustment.delta,
                "action_ids": list(adjustment.selector.action_ids),
                "required_labels": list(adjustment.selector.required_labels),
            }
            for adjustment in patch.trigger_cap_adjustments
        ],
    }


def _deserialize_selector(payload: dict[str, Any]) -> ActionPatchSelector:
    return ActionPatchSelector(
        action_ids=tuple(str(item) for item in payload.get("action_ids", []) if str(item).strip()),
        required_labels=tuple(str(item) for item in payload.get("required_labels", []) if str(item).strip()),
    )


def _normalize_mapping_list(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


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


def _apply_ratio(*, max_value: int, ratio: Decimal) -> int:
    normalized_ratio = max(Decimal("0.0000"), min(Decimal("1.0000"), Decimal(ratio)))
    current_value = _round_decimal_to_int(Decimal(max_value) * normalized_ratio)
    if normalized_ratio > Decimal("0") and current_value <= 0:
        return 1
    return max(0, min(max_value, current_value))


def _cap_to_permille(value: Decimal | str) -> int:
    decimal_value = value if isinstance(value, Decimal) else Decimal(str(value))
    return int((decimal_value * Decimal("1000")).to_integral_value(rounding=ROUND_HALF_UP))


def _clamp(value: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(maximum, value))


__all__ = [
    "CurrentAttributeService",
    "CurrentAttributeServiceError",
    "CurrentAttributeSnapshot",
    "CurrentAttributeStateError",
    "_DEFAULT_HERO_TEMPLATE_ID",
    "_FULL_RESOURCE_VALUE",
]
