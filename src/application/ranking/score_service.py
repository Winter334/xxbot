"""角色评分应用服务。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
import hashlib
import json
from typing import Any

from application.character.skill_runtime_support import SkillInventoryItemSnapshot, SkillRuntimeSupport
from domain.equipment import (
    EquipmentAffixValue,
    EquipmentAttributeValue,
    EquipmentItem as DomainEquipmentItem,
    EquipmentSpecialEffectValue,
    scale_special_effect_payload,
    special_effect_strength_multiplier_for_quality,
)
from domain.ranking import (
    CharacterScoreRuleService,
    CharacterScoringInput,
    ScoreAffixInput,
    ScoreEquipmentItemInput,
    ScoreGrowthInput,
    ScoreSkillItemInput,
    ScoreSkillLoadoutInput,
    ScoreStatInput,
)
from domain.ranking.models import ScoreSpecialEffectInput
from infrastructure.config.static import StaticGameConfig, get_static_config
from infrastructure.db.models import CharacterScoreSnapshot, EquipmentItem as EquipmentItemModel
from infrastructure.db.repositories import (
    CharacterAggregate,
    CharacterRepository,
    CharacterScoreSnapshotRepository,
    SkillRepository,
    SqlAlchemySkillRepository,
)

_ACTIVE_ITEM_STATE = "active"


@dataclass(frozen=True, slots=True)
class CharacterScoreSnapshotDTO:
    """单角色评分刷新结果。"""

    character_id: int
    score_version: str
    total_power_score: int
    public_power_score: int
    hidden_pvp_score: int
    growth_score: int
    equipment_score: int
    skill_score: int
    artifact_score: int
    pvp_adjustment_score: int
    main_path_id: str
    main_path_name: str
    preferred_scene: str
    source_digest: str
    computed_at: datetime
    breakdown: dict[str, Any]


class CharacterScoreServiceError(RuntimeError):
    """角色评分服务基础异常。"""


class CharacterScoreNotFoundError(CharacterScoreServiceError):
    """角色不存在。"""


class CharacterScoreStateError(CharacterScoreServiceError):
    """角色评分输入不完整。"""


class CharacterScoreService:
    """负责单角色评分计算、缓存写回与评分明细快照落库。"""

    def __init__(
        self,
        *,
        character_repository: CharacterRepository,
        score_snapshot_repository: CharacterScoreSnapshotRepository,
        skill_repository: SkillRepository | None = None,
        static_config: StaticGameConfig | None = None,
        rule_service: CharacterScoreRuleService | None = None,
        skill_runtime_support: SkillRuntimeSupport | None = None,
    ) -> None:
        self._character_repository = character_repository
        self._score_snapshot_repository = score_snapshot_repository
        self._skill_repository = skill_repository or self._build_fallback_skill_repository(character_repository)
        self._static_config = static_config or get_static_config()
        self._rule_service = rule_service or CharacterScoreRuleService(self._static_config)
        self._skill_runtime_support = skill_runtime_support or SkillRuntimeSupport(
            character_repository=character_repository,
            skill_repository=self._skill_repository,
            static_config=self._static_config,
        )
        self._skill_path_name_by_id = {
            path.path_id: path.name for path in self._static_config.skill_paths.paths
        }

    def refresh_character_score(self, *, character_id: int) -> CharacterScoreSnapshotDTO:
        """刷新单角色评分缓存与评分明细快照。"""
        aggregate = self._require_scoring_aggregate(character_id)
        scoring_input = self._build_scoring_input(aggregate)
        calculated_score = self._rule_service.calculate(scoring_input=scoring_input)
        source_payload = self._build_source_payload(scoring_input=scoring_input)
        source_summary = self._build_source_summary(aggregate=aggregate, scoring_input=scoring_input)
        source_digest = self._build_source_digest(source_payload=source_payload, score_version=calculated_score.score_version)
        computed_at = datetime.now(UTC).replace(tzinfo=None)

        self._character_repository.save_score_cache(
            character=aggregate.character,
            total_power_score=calculated_score.total_power_score,
            public_power_score=calculated_score.public_power_score,
            hidden_pvp_score=calculated_score.hidden_pvp_score,
        )

        breakdown_json = dict(calculated_score.breakdown)
        breakdown_json["source_summary"] = source_summary

        persisted_snapshot = self._score_snapshot_repository.upsert_snapshot(
            CharacterScoreSnapshot(
                character_id=aggregate.character.id,
                score_version=calculated_score.score_version,
                total_power_score=calculated_score.total_power_score,
                public_power_score=calculated_score.public_power_score,
                hidden_pvp_score=calculated_score.hidden_pvp_score,
                growth_score=calculated_score.growth_score,
                equipment_score=calculated_score.equipment_score,
                skill_score=calculated_score.skill_score,
                artifact_score=calculated_score.artifact_score,
                pvp_adjustment_score=calculated_score.pvp_adjustment_score,
                breakdown_json=breakdown_json,
                source_digest=source_digest,
                computed_at=computed_at,
            )
        )
        return CharacterScoreSnapshotDTO(
            character_id=aggregate.character.id,
            score_version=persisted_snapshot.score_version,
            total_power_score=persisted_snapshot.total_power_score,
            public_power_score=persisted_snapshot.public_power_score,
            hidden_pvp_score=persisted_snapshot.hidden_pvp_score,
            growth_score=persisted_snapshot.growth_score,
            equipment_score=persisted_snapshot.equipment_score,
            skill_score=persisted_snapshot.skill_score,
            artifact_score=persisted_snapshot.artifact_score,
            pvp_adjustment_score=persisted_snapshot.pvp_adjustment_score,
            main_path_id=calculated_score.main_path_id,
            main_path_name=calculated_score.main_path_name,
            preferred_scene=calculated_score.preferred_scene,
            source_digest=persisted_snapshot.source_digest,
            computed_at=persisted_snapshot.computed_at,
            breakdown=dict(persisted_snapshot.breakdown_json),
        )

    def _require_scoring_aggregate(self, character_id: int) -> CharacterAggregate:
        aggregate = self._character_repository.get_aggregate(character_id)
        if aggregate is None:
            raise CharacterScoreNotFoundError(f"角色不存在：{character_id}")
        if aggregate.progress is None:
            raise CharacterScoreStateError(f"角色缺少成长状态：{character_id}")
        return aggregate

    def _build_scoring_input(self, aggregate: CharacterAggregate) -> CharacterScoringInput:
        progress = aggregate.progress
        assert progress is not None
        growth_rule = self._static_config.daily_cultivation.entries
        realm_total_cultivation = next(
            (entry.total_cultivation for entry in growth_rule if entry.realm_id == progress.realm_id),
            progress.cultivation_value if progress.cultivation_value > 0 else 1,
        )
        equipped_items = tuple(
            self._build_equipment_item_input(item)
            for item in aggregate.equipment_items
            if item.item_state == _ACTIVE_ITEM_STATE and item.equipped_slot_id is not None
        )
        loadout_snapshot = self._skill_runtime_support.get_loadout_snapshot(character_id=aggregate.character.id)
        skill_loadout = ScoreSkillLoadoutInput(
            main_axis_id=loadout_snapshot.main_axis_id,
            main_path_id=loadout_snapshot.main_path_id,
            main_path_name=self._skill_path_name_by_id.get(loadout_snapshot.main_path_id, loadout_snapshot.main_path_id),
            behavior_template_id=loadout_snapshot.behavior_template_id,
            main_skill=self._build_skill_item_input(loadout_snapshot.main_skill),
            guard_skill=self._build_skill_item_input(loadout_snapshot.guard_skill),
            movement_skill=self._build_skill_item_input(loadout_snapshot.movement_skill),
            spirit_skill=self._build_skill_item_input(loadout_snapshot.spirit_skill),
        )
        return CharacterScoringInput(
            character_id=aggregate.character.id,
            growth=ScoreGrowthInput(
                realm_id=progress.realm_id,
                stage_id=progress.stage_id,
                cultivation_value=max(0, progress.cultivation_value),
                comprehension_value=max(0, progress.comprehension_value),
                realm_total_cultivation=max(1, realm_total_cultivation),
            ),
            skill_loadout=skill_loadout,
            equipped_items=equipped_items,
        )

    def _build_equipment_item_input(self, equipment_model: EquipmentItemModel) -> ScoreEquipmentItemInput:
        domain_item = self._to_domain_item(equipment_model)
        resolved_stats = tuple(
            ScoreStatInput(stat_id=stat.stat_id, value=stat.value)
            for stat in domain_item.resolved_stat_lines()
        )
        affixes = tuple(
            ScoreAffixInput(
                affix_id=affix.affix_id,
                tier_id=affix.tier_id,
                value=affix.value,
                is_pve_specialized=affix.is_pve_specialized,
                is_pvp_specialized=affix.is_pvp_specialized,
                affix_kind=affix.affix_kind,
                special_effect=self._build_score_special_effect_input(
                    affix.special_effect,
                    quality_id=domain_item.quality_id,
                ),
            )
            for affix in domain_item.affixes
        )
        artifact_profile = equipment_model.artifact_profile
        return ScoreEquipmentItemInput(
            item_id=equipment_model.id,
            slot_id=equipment_model.slot_id,
            equipped_slot_id=equipment_model.equipped_slot_id,
            quality_id=equipment_model.quality_id,
            template_id=equipment_model.template_id,
            is_artifact=equipment_model.is_artifact,
            enhancement_level=domain_item.enhancement_level,
            artifact_nurture_level=domain_item.artifact_nurture_level,
            refinement_level=0 if artifact_profile is None else max(0, artifact_profile.refinement_level),
            resonance_name=equipment_model.resonance_name,
            affixes=affixes,
            resolved_stats=resolved_stats,
        )

    @staticmethod
    def _build_score_special_effect_input(
        special_effect: EquipmentSpecialEffectValue | None,
        *,
        quality_id: str,
    ) -> ScoreSpecialEffectInput | None:
        if special_effect is None:
            return None
        return ScoreSpecialEffectInput(
            effect_id=special_effect.effect_id,
            effect_type=special_effect.effect_type,
            trigger_event=special_effect.trigger_event,
            public_score_key=special_effect.public_score_key,
            hidden_pvp_score_key=special_effect.hidden_pvp_score_key,
            payload=dict(scale_special_effect_payload(quality_id=quality_id, payload=special_effect.payload)),
            strength_multiplier_permille=int(
                special_effect_strength_multiplier_for_quality(quality_id=quality_id) * Decimal("1000")
            ),
        )

    def _build_skill_item_input(self, skill_item: SkillInventoryItemSnapshot) -> ScoreSkillItemInput:
        return ScoreSkillItemInput(
            item_id=skill_item.item_id,
            lineage_id=skill_item.lineage_id,
            skill_name=skill_item.skill_name,
            path_id=skill_item.path_id,
            path_name=self._skill_path_name_by_id.get(skill_item.path_id, skill_item.path_id),
            axis_id=skill_item.axis_id,
            skill_type=skill_item.skill_type,
            auxiliary_slot_id=skill_item.auxiliary_slot_id,
            rank_id=skill_item.rank_id,
            rank_name=skill_item.rank_name,
            rank_order=skill_item.rank_order,
            quality_id=skill_item.quality_id,
            quality_name=skill_item.quality_name,
            total_budget=skill_item.total_budget,
            resolved_patch_ids=skill_item.resolved_patch_ids,
        )

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
        rank_id = equipment_model.rank_id or "mortal"
        rank_name = equipment_model.rank_name or "一阶"
        rank_order = equipment_model.rank_order or 1
        mapped_realm_id = equipment_model.mapped_realm_id or "mortal"
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
            rank_id=rank_id,
            rank_name=rank_name,
            rank_order=rank_order,
            mapped_realm_id=mapped_realm_id,
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
    def _build_source_payload(*, scoring_input: CharacterScoringInput) -> dict[str, Any]:
        return {
            "character_id": scoring_input.character_id,
            "growth": {
                "realm_id": scoring_input.growth.realm_id,
                "stage_id": scoring_input.growth.stage_id,
                "cultivation_value": scoring_input.growth.cultivation_value,
                "comprehension_value": scoring_input.growth.comprehension_value,
                "realm_total_cultivation": scoring_input.growth.realm_total_cultivation,
            },
            "skill_loadout": None
            if scoring_input.skill_loadout is None
            else {
                "main_axis_id": scoring_input.skill_loadout.main_axis_id,
                "main_path_id": scoring_input.skill_loadout.main_path_id,
                "main_path_name": scoring_input.skill_loadout.main_path_name,
                "behavior_template_id": scoring_input.skill_loadout.behavior_template_id,
                "main_skill": CharacterScoreService._serialize_skill_item_payload(scoring_input.skill_loadout.main_skill),
                "guard_skill": CharacterScoreService._serialize_skill_item_payload(scoring_input.skill_loadout.guard_skill),
                "movement_skill": CharacterScoreService._serialize_skill_item_payload(scoring_input.skill_loadout.movement_skill),
                "spirit_skill": CharacterScoreService._serialize_skill_item_payload(scoring_input.skill_loadout.spirit_skill),
            },
            "equipped_items": [
                {
                    "item_id": item.item_id,
                    "slot_id": item.slot_id,
                    "equipped_slot_id": item.equipped_slot_id,
                    "quality_id": item.quality_id,
                    "template_id": item.template_id,
                    "is_artifact": item.is_artifact,
                    "enhancement_level": item.enhancement_level,
                    "artifact_nurture_level": item.artifact_nurture_level,
                    "refinement_level": item.refinement_level,
                    "resonance_name": item.resonance_name,
                    "affixes": [
                        {
                            "affix_id": affix.affix_id,
                            "tier_id": affix.tier_id,
                            "value": affix.value,
                            "is_pve_specialized": affix.is_pve_specialized,
                            "is_pvp_specialized": affix.is_pvp_specialized,
                            "affix_kind": affix.affix_kind,
                            "special_effect": None
                            if affix.special_effect is None
                            else {
                                "effect_id": affix.special_effect.effect_id,
                                "effect_type": affix.special_effect.effect_type,
                                "trigger_event": affix.special_effect.trigger_event,
                                "public_score_key": affix.special_effect.public_score_key,
                                "hidden_pvp_score_key": affix.special_effect.hidden_pvp_score_key,
                                "payload": dict(affix.special_effect.payload),
                            },
                        }
                        for affix in item.affixes
                    ],
                    "resolved_stats": [
                        {"stat_id": stat.stat_id, "value": stat.value}
                        for stat in item.resolved_stats
                    ],
                }
                for item in scoring_input.equipped_items
            ],
        }

    @staticmethod
    def _serialize_skill_item_payload(skill_item: ScoreSkillItemInput | None) -> dict[str, Any] | None:
        if skill_item is None:
            return None
        return {
            "item_id": skill_item.item_id,
            "lineage_id": skill_item.lineage_id,
            "skill_name": skill_item.skill_name,
            "path_id": skill_item.path_id,
            "path_name": skill_item.path_name,
            "axis_id": skill_item.axis_id,
            "skill_type": skill_item.skill_type,
            "auxiliary_slot_id": skill_item.auxiliary_slot_id,
            "rank_id": skill_item.rank_id,
            "rank_name": skill_item.rank_name,
            "rank_order": skill_item.rank_order,
            "quality_id": skill_item.quality_id,
            "quality_name": skill_item.quality_name,
            "total_budget": skill_item.total_budget,
            "resolved_patch_ids": list(skill_item.resolved_patch_ids),
        }

    @staticmethod
    def _build_source_digest(*, source_payload: dict[str, Any], score_version: str) -> str:
        serialized_payload = json.dumps(
            {
                "score_version": score_version,
                "payload": source_payload,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(serialized_payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _build_source_summary(
        *,
        aggregate: CharacterAggregate,
        scoring_input: CharacterScoringInput,
    ) -> dict[str, Any]:
        skill_loadout = scoring_input.skill_loadout
        main_skill = None if skill_loadout is None else skill_loadout.main_skill
        guard_skill = None if skill_loadout is None else skill_loadout.guard_skill
        movement_skill = None if skill_loadout is None else skill_loadout.movement_skill
        spirit_skill = None if skill_loadout is None else skill_loadout.spirit_skill
        return {
            "character_name": aggregate.character.name,
            "character_title": aggregate.character.title,
            "realm_id": scoring_input.growth.realm_id,
            "stage_id": scoring_input.growth.stage_id,
            "main_axis_id": None if skill_loadout is None else skill_loadout.main_axis_id,
            "main_path_id": None if skill_loadout is None else skill_loadout.main_path_id,
            "main_path_name": None if skill_loadout is None else skill_loadout.main_path_name,
            "behavior_template_id": None if skill_loadout is None else skill_loadout.behavior_template_id,
            "main_skill_item_id": None if main_skill is None else main_skill.item_id,
            "main_skill_name": None if main_skill is None else main_skill.skill_name,
            "main_skill_rank_name": None if main_skill is None else main_skill.rank_name,
            "main_skill_quality_name": None if main_skill is None else main_skill.quality_name,
            "guard_skill_item_id": None if guard_skill is None else guard_skill.item_id,
            "guard_skill_name": None if guard_skill is None else guard_skill.skill_name,
            "movement_skill_item_id": None if movement_skill is None else movement_skill.item_id,
            "movement_skill_name": None if movement_skill is None else movement_skill.skill_name,
            "spirit_skill_item_id": None if spirit_skill is None else spirit_skill.item_id,
            "spirit_skill_name": None if spirit_skill is None else spirit_skill.skill_name,
            "equipped_item_ids": [item.item_id for item in scoring_input.equipped_items],
            "equipped_slot_ids": [item.slot_id for item in scoring_input.equipped_items],
            "artifact_item_ids": [item.item_id for item in scoring_input.equipped_items if item.is_artifact],
            "equipped_skill_item_ids": [
                skill_item.item_id
                for skill_item in (main_skill, guard_skill, movement_skill, spirit_skill)
                if skill_item is not None
            ],
        }

    @staticmethod
    def _build_fallback_skill_repository(character_repository: CharacterRepository) -> SkillRepository:
        session = getattr(character_repository, "_session", None)
        if session is None:
            raise ValueError("CharacterScoreService 缺少 skill_repository，且无法从 character_repository 推导会话")
        return SqlAlchemySkillRepository(session)


__all__ = [
    "CharacterScoreNotFoundError",
    "CharacterScoreService",
    "CharacterScoreServiceError",
    "CharacterScoreSnapshotDTO",
    "CharacterScoreStateError",
]
