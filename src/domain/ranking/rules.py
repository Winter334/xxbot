"""评分领域规则服务。"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
import math

from domain.character import CharacterGrowthProgression
from domain.ranking.models import (
    CharacterScoringInput,
    CalculatedCharacterScore,
    ScoreAffixInput,
    ScoreEquipmentItemInput,
    ScoreSkillItemInput,
    ScoreSpecialEffectInput,
    ScoreStatInput,
)
from infrastructure.config.static.models.common import StaticGameConfig

_DEFAULT_MAIN_PATH_ID = "zhanqing_sword"
_SCORE_VERSION = "stage8.v3"
_PVP_ADJUSTMENT_LIMIT_RATIO = Decimal("0.15")
_NON_ARTIFACT_SLOT_IDS = frozenset({"weapon", "armor", "accessory"})

_PUBLIC_STAT_WEIGHT_BY_ID: dict[str, Decimal] = {
    "attack_power": Decimal("1.00"),
    "max_hp": Decimal("0.20"),
    "guard_power": Decimal("0.95"),
    "speed": Decimal("11.00"),
    "crit_rate_permille": Decimal("0.65"),
    "crit_damage_permille": Decimal("0.50"),
    "damage_bonus_permille": Decimal("0.80"),
    "damage_reduction_permille": Decimal("1.10"),
    "control_resist_permille": Decimal("0.70"),
    "shield_power_permille": Decimal("0.85"),
    "penetration_permille": Decimal("0.90"),
    "hit_rate_permille": Decimal("0.70"),
    "dodge_rate_permille": Decimal("0.75"),
    "lifesteal_permille": Decimal("0.65"),
}

_PVP_STAT_WEIGHT_BY_ID: dict[str, Decimal] = {
    "attack_power": Decimal("0.12"),
    "speed": Decimal("3.20"),
    "guard_power": Decimal("0.25"),
    "damage_reduction_permille": Decimal("1.20"),
    "control_resist_permille": Decimal("1.00"),
    "shield_power_permille": Decimal("1.10"),
    "penetration_permille": Decimal("1.00"),
    "hit_rate_permille": Decimal("0.95"),
    "dodge_rate_permille": Decimal("0.95"),
    "crit_rate_permille": Decimal("0.55"),
    "crit_damage_permille": Decimal("0.40"),
    "pvp_damage_permille": Decimal("1.25"),
    "pve_damage_permille": Decimal("-0.80"),
}

_SPECIAL_EFFECT_BASE_SCORE = 18
_SPECIAL_EFFECT_ARTIFACT_BONUS = 6
_SPECIAL_EFFECT_TIER_STEP = 10
_SPECIAL_EFFECT_PVP_BASE = 14
_SPECIAL_EFFECT_PVP_TIER_STEP = 8
_PUBLIC_SPECIAL_EFFECT_OFFSET_BY_KEY: dict[str, int] = {
    "se_sunder_on_hit": 2,
    "se_dot_on_hit": 4,
    "se_battle_start_barrier": 1,
    "se_barrier_on_damage_taken": 2,
    "se_low_hp_regen": 0,
    "se_heal_after_attack": -1,
    "se_round_end_barrier_if_empty": -2,
    "se_counter_sunder": 2,
    "se_damage_to_barrier": 1,
    "se_counter_dot": 3,
    "se_duanyue_mark": 4,
    "se_liemai_mark": 5,
    "se_zhuohun_mark": 5,
    "se_qianshi_mark": 3,
    "se_xiantian_barrier": 3,
    "se_shoujie_barrier": 3,
    "se_canmai_regen": 2,
    "se_zhanhou_heal": 2,
    "se_kongming_barrier": 0,
    "se_shanghua_barrier": 3,
    "se_huifeng_sunder": 4,
    "se_fanshi_flame": 4,
}
_HIDDEN_PVP_SPECIAL_EFFECT_OFFSET_BY_KEY: dict[str, int] = {
    "pvp_se_sunder_on_hit": 3,
    "pvp_se_dot_on_hit": 2,
    "pvp_se_battle_start_barrier": 1,
    "pvp_se_barrier_on_damage_taken": 3,
    "pvp_se_low_hp_regen": 0,
    "pvp_se_heal_after_attack": -1,
    "pvp_se_round_end_barrier_if_empty": 0,
    "pvp_se_counter_sunder": 4,
    "pvp_se_damage_to_barrier": 1,
    "pvp_se_counter_dot": 3,
    "pvp_se_duanyue_mark": 4,
    "pvp_se_liemai_mark": 5,
    "pvp_se_zhuohun_mark": 4,
    "pvp_se_qianshi_mark": 3,
    "pvp_se_xiantian_barrier": 2,
    "pvp_se_shoujie_barrier": 4,
    "pvp_se_canmai_regen": 2,
    "pvp_se_zhanhou_heal": 2,
    "pvp_se_kongming_barrier": 1,
    "pvp_se_shanghua_barrier": 3,
    "pvp_se_huifeng_sunder": 5,
    "pvp_se_fanshi_flame": 4,
}


@dataclass(frozen=True, slots=True)
class ResolvedSkillContext:
    """评分用功法配置上下文。"""

    main_axis_id: str
    main_path_id: str
    main_path_name: str
    main_skill_name: str
    preferred_scene: str
    behavior_template_id: str
    main_skill: ScoreSkillItemInput | None
    guard_skill: ScoreSkillItemInput | None
    movement_skill: ScoreSkillItemInput | None
    spirit_skill: ScoreSkillItemInput | None
    fallback_applied: bool


class CharacterScoreRuleService:
    """根据角色当前聚合输入计算单角色评分。"""

    def __init__(self, static_config: StaticGameConfig) -> None:
        self._static_config = static_config
        self._growth_progression = CharacterGrowthProgression(static_config)
        self._realm_coefficient_by_id = {
            entry.realm_id: entry.coefficient for entry in static_config.base_coefficients.realm_curve.entries
        }
        self._stage_order_by_id = {
            stage.stage_id: stage.order for stage in static_config.realm_progression.stages
        }
        self._quality_order_by_id = {
            quality.quality_id: quality.order for quality in static_config.equipment.qualities
        }
        self._affix_tier_order_by_id = {
            tier.tier_id: tier.order for tier in static_config.equipment.affix_tiers
        }
        self._skill_path_by_id = {
            path.path_id: path for path in static_config.skill_paths.paths
        }

    @property
    def score_version(self) -> str:
        """返回当前评分公式版本。"""
        return _SCORE_VERSION

    def calculate(self, *, scoring_input: CharacterScoringInput) -> CalculatedCharacterScore:
        """计算角色公开评分与隐藏对战评分。"""
        growth_score, growth_breakdown = self._calculate_growth_score(scoring_input=scoring_input)
        non_artifact_items = tuple(item for item in scoring_input.equipped_items if not item.is_artifact)
        artifact_items = tuple(item for item in scoring_input.equipped_items if item.is_artifact)
        equipment_score, equipment_breakdown = self._calculate_equipment_score(items=non_artifact_items)
        artifact_score, artifact_breakdown = self._calculate_artifact_score(items=artifact_items)
        skill_context = self._resolve_skill_context(scoring_input=scoring_input)
        skill_score, skill_breakdown = self._calculate_skill_score(skill_context=skill_context)

        public_power_score = growth_score + equipment_score + skill_score + artifact_score
        pvp_adjustment_score, pvp_breakdown = self._calculate_pvp_adjustment_score(
            public_power_score=public_power_score,
            items=scoring_input.equipped_items,
            skill_context=skill_context,
        )
        hidden_pvp_score = public_power_score + pvp_adjustment_score
        breakdown = {
            "score_version": self.score_version,
            "growth": growth_breakdown,
            "equipment": equipment_breakdown,
            "skill": skill_breakdown,
            "artifact": artifact_breakdown,
            "pvp_adjustment": pvp_breakdown,
            "totals": {
                "growth_score": growth_score,
                "equipment_score": equipment_score,
                "skill_score": skill_score,
                "artifact_score": artifact_score,
                "public_power_score": public_power_score,
                "pvp_adjustment_score": pvp_adjustment_score,
                "hidden_pvp_score": hidden_pvp_score,
            },
        }
        return CalculatedCharacterScore(
            score_version=self.score_version,
            total_power_score=public_power_score,
            public_power_score=public_power_score,
            hidden_pvp_score=hidden_pvp_score,
            growth_score=growth_score,
            equipment_score=equipment_score,
            skill_score=skill_score,
            artifact_score=artifact_score,
            pvp_adjustment_score=pvp_adjustment_score,
            main_path_id=skill_context.main_path_id,
            main_path_name=skill_context.main_skill_name,
            preferred_scene=skill_context.preferred_scene,
            breakdown=breakdown,
        )

    def _calculate_growth_score(self, *, scoring_input: CharacterScoringInput) -> tuple[int, dict[str, object]]:
        growth = scoring_input.growth
        realm_rule = self._growth_progression.get_realm_rule(growth.realm_id)
        stage_threshold = self._growth_progression.resolve_stage(growth.realm_id, growth.cultivation_value)
        realm_coefficient = self._realm_coefficient_by_id.get(growth.realm_id, 1)
        stage_order = self._stage_order_by_id.get(growth.stage_id, stage_threshold.order)
        cultivation_ratio = self._clamp_ratio(
            numerator=growth.cultivation_value,
            denominator=max(1, growth.realm_total_cultivation),
        )
        realm_base_score = int(round(math.log10(realm_coefficient + 1) * 320))
        stage_score = max(0, (stage_order - 1) * 48)
        cultivation_progress_score = self._scale_ratio(cultivation_ratio, 260)
        comprehension_reference = max(1, growth.realm_total_cultivation // 4)
        comprehension_ratio = self._clamp_ratio(
            numerator=growth.comprehension_value,
            denominator=comprehension_reference,
        )
        comprehension_score = min(120, self._scale_ratio(comprehension_ratio, 120))
        total_score = realm_base_score + stage_score + cultivation_progress_score + comprehension_score
        return total_score, {
            "realm_id": growth.realm_id,
            "realm_name": realm_rule.realm_name,
            "stage_id": growth.stage_id,
            "resolved_stage_id": stage_threshold.stage_id,
            "cultivation_value": growth.cultivation_value,
            "comprehension_value": growth.comprehension_value,
            "realm_total_cultivation": growth.realm_total_cultivation,
            "realm_coefficient": realm_coefficient,
            "realm_base_score": realm_base_score,
            "stage_score": stage_score,
            "cultivation_progress_score": cultivation_progress_score,
            "cultivation_ratio_permille": self._ratio_to_permille(cultivation_ratio),
            "comprehension_score": comprehension_score,
            "comprehension_ratio_permille": self._ratio_to_permille(comprehension_ratio),
        }

    def _calculate_equipment_score(self, *, items: tuple[ScoreEquipmentItemInput, ...]) -> tuple[int, dict[str, object]]:
        slot_breakdowns: list[dict[str, object]] = []
        equipped_slot_ids: set[str] = set()
        total_score = 0
        for item in items:
            equipped_slot_ids.add(item.slot_id)
            quality_order = self._quality_order_by_id.get(item.quality_id, 1)
            stat_score = self._score_public_stats(item.resolved_stats)
            affix_score, special_effect_breakdown = self._score_public_affixes(item.affixes, is_artifact=False)
            quality_score = quality_order * 70
            enhancement_score = item.enhancement_level * 34
            item_score = stat_score + affix_score + quality_score + enhancement_score
            total_score += item_score
            slot_breakdowns.append(
                {
                    "item_id": item.item_id,
                    "slot_id": item.slot_id,
                    "quality_id": item.quality_id,
                    "template_id": item.template_id,
                    "enhancement_level": item.enhancement_level,
                    "stat_score": stat_score,
                    "affix_score": affix_score,
                    "quality_score": quality_score,
                    "enhancement_score": enhancement_score,
                    "item_score": item_score,
                    "affix_count": len(item.affixes),
                    "special_effects": special_effect_breakdown,
                }
            )
        completeness_bonus = 90 if equipped_slot_ids == _NON_ARTIFACT_SLOT_IDS else len(equipped_slot_ids) * 20
        total_score += completeness_bonus
        return total_score, {
            "item_count": len(items),
            "equipped_slot_ids": sorted(equipped_slot_ids),
            "completeness_bonus": completeness_bonus,
            "slot_scores": slot_breakdowns,
        }

    def _calculate_artifact_score(self, *, items: tuple[ScoreEquipmentItemInput, ...]) -> tuple[int, dict[str, object]]:
        artifact_breakdowns: list[dict[str, object]] = []
        total_score = 0
        for item in items:
            quality_order = self._quality_order_by_id.get(item.quality_id, 1)
            stat_score = self._score_public_stats(item.resolved_stats)
            affix_score, special_effect_breakdown = self._score_public_affixes(item.affixes, is_artifact=True)
            quality_score = quality_order * 60
            nurture_score = item.artifact_nurture_level * 55
            refinement_score = item.refinement_level * 45
            resonance_score = 30 if item.resonance_name else 0
            item_score = stat_score + affix_score + quality_score + nurture_score + refinement_score + resonance_score
            total_score += item_score
            artifact_breakdowns.append(
                {
                    "item_id": item.item_id,
                    "slot_id": item.slot_id,
                    "quality_id": item.quality_id,
                    "template_id": item.template_id,
                    "resonance_name": item.resonance_name,
                    "artifact_nurture_level": item.artifact_nurture_level,
                    "refinement_level": item.refinement_level,
                    "stat_score": stat_score,
                    "affix_score": affix_score,
                    "quality_score": quality_score,
                    "nurture_score": nurture_score,
                    "refinement_score": refinement_score,
                    "resonance_score": resonance_score,
                    "item_score": item_score,
                    "affix_count": len(item.affixes),
                    "special_effects": special_effect_breakdown,
                }
            )
        return total_score, {
            "item_count": len(items),
            "artifact_scores": artifact_breakdowns,
        }

    def _calculate_skill_score(self, *, skill_context: ResolvedSkillContext) -> tuple[int, dict[str, object]]:
        auxiliary_skills = tuple(
            skill_item
            for skill_item in (
                skill_context.guard_skill,
                skill_context.movement_skill,
                skill_context.spirit_skill,
            )
            if skill_item is not None
        )
        main_skill_presence_score = 240 if skill_context.main_skill is not None else 0
        main_skill_budget_score = 0 if skill_context.main_skill is None else max(0, skill_context.main_skill.total_budget) * 10
        auxiliary_completeness_score = len(auxiliary_skills) * 55
        auxiliary_budget_score = sum(max(0, skill_item.total_budget) for skill_item in auxiliary_skills) * 7
        total_score = (
            main_skill_presence_score
            + main_skill_budget_score
            + auxiliary_completeness_score
            + auxiliary_budget_score
        )
        return total_score, {
            "main_axis_id": skill_context.main_axis_id,
            "main_path_id": skill_context.main_path_id,
            "main_path_name": skill_context.main_path_name,
            "main_skill_name": skill_context.main_skill_name,
            "behavior_template_id": skill_context.behavior_template_id,
            "preferred_scene": skill_context.preferred_scene,
            "fallback_applied": skill_context.fallback_applied,
            "main_skill_presence_score": main_skill_presence_score,
            "main_skill_budget_score": main_skill_budget_score,
            "auxiliary_completeness_score": auxiliary_completeness_score,
            "auxiliary_budget_score": auxiliary_budget_score,
            "configured_auxiliary_count": len(auxiliary_skills),
            "main_skill": self._serialize_skill_item(skill_context.main_skill),
            "guard_skill": self._serialize_skill_item(skill_context.guard_skill),
            "movement_skill": self._serialize_skill_item(skill_context.movement_skill),
            "spirit_skill": self._serialize_skill_item(skill_context.spirit_skill),
        }

    def _calculate_pvp_adjustment_score(
        self,
        *,
        public_power_score: int,
        items: tuple[ScoreEquipmentItemInput, ...],
        skill_context: ResolvedSkillContext,
    ) -> tuple[int, dict[str, object]]:
        specialization_score = 0
        pvp_specialized_affix_count = 0
        pve_specialized_affix_count = 0
        stat_adjustment_score = 0
        special_effect_adjustment_score = 0
        special_effect_adjustments: list[dict[str, object]] = []
        for item in items:
            stat_adjustment_score += self._score_pvp_stats(item.resolved_stats)
            for affix in item.affixes:
                tier_order = self._affix_tier_order_by_id.get(affix.tier_id, 1)
                if affix.is_pvp_specialized:
                    specialization_score += 32 + tier_order * 6
                    pvp_specialized_affix_count += 1
                if affix.is_pve_specialized:
                    specialization_score -= 18 + tier_order * 4
                    pve_specialized_affix_count += 1
                effect_score = self._score_hidden_pvp_special_effect(affix.special_effect, tier_order=tier_order)
                if effect_score == 0:
                    continue
                special_effect_adjustment_score += effect_score
                if affix.special_effect is not None:
                    special_effect_adjustments.append(
                        {
                            "effect_id": affix.special_effect.effect_id,
                            "effect_type": affix.special_effect.effect_type,
                            "trigger_event": affix.special_effect.trigger_event,
                            "score": effect_score,
                            "hidden_pvp_score_key": affix.special_effect.hidden_pvp_score_key,
                        }
                    )
        preferred_scene_score = 0
        if "PVP" in skill_context.preferred_scene:
            preferred_scene_score += 60
        if "PVE" in skill_context.preferred_scene:
            preferred_scene_score -= 30
        raw_adjustment = specialization_score + stat_adjustment_score + special_effect_adjustment_score + preferred_scene_score
        adjustment_limit = self._scale_decimal(Decimal(public_power_score), _PVP_ADJUSTMENT_LIMIT_RATIO)
        bounded_adjustment = max(-adjustment_limit, min(adjustment_limit, raw_adjustment))
        return bounded_adjustment, {
            "specialization_score": specialization_score,
            "stat_adjustment_score": stat_adjustment_score,
            "special_effect_adjustment_score": special_effect_adjustment_score,
            "special_effect_adjustments": special_effect_adjustments,
            "preferred_scene_score": preferred_scene_score,
            "raw_adjustment": raw_adjustment,
            "adjustment_limit": adjustment_limit,
            "bounded_adjustment": bounded_adjustment,
            "pvp_specialized_affix_count": pvp_specialized_affix_count,
            "pve_specialized_affix_count": pve_specialized_affix_count,
            "preferred_scene": skill_context.preferred_scene,
        }

    def _resolve_skill_context(self, *, scoring_input: CharacterScoringInput) -> ResolvedSkillContext:
        raw_loadout = scoring_input.skill_loadout
        fallback_applied = raw_loadout is None
        resolved_main_path_id = _DEFAULT_MAIN_PATH_ID
        if raw_loadout is not None and raw_loadout.main_skill is not None:
            candidate_path_id = raw_loadout.main_skill.path_id
            if candidate_path_id in self._skill_path_by_id:
                resolved_main_path_id = candidate_path_id
            else:
                fallback_applied = True
        elif raw_loadout is not None and raw_loadout.main_path_id in self._skill_path_by_id:
            assert raw_loadout.main_path_id is not None
            resolved_main_path_id = raw_loadout.main_path_id
        else:
            fallback_applied = True
        path_definition = self._skill_path_by_id[resolved_main_path_id]

        resolved_main_axis_id = path_definition.axis_id
        if raw_loadout is not None and raw_loadout.main_skill is not None and raw_loadout.main_skill.axis_id.strip():
            resolved_main_axis_id = raw_loadout.main_skill.axis_id
        elif raw_loadout is not None and raw_loadout.main_axis_id is not None and raw_loadout.main_axis_id.strip():
            resolved_main_axis_id = raw_loadout.main_axis_id

        resolved_behavior_template_id = path_definition.template_id
        if raw_loadout is not None and raw_loadout.behavior_template_id is not None and raw_loadout.behavior_template_id.strip():
            resolved_behavior_template_id = raw_loadout.behavior_template_id

        main_skill = None if raw_loadout is None else raw_loadout.main_skill
        guard_skill = None if raw_loadout is None else raw_loadout.guard_skill
        movement_skill = None if raw_loadout is None else raw_loadout.movement_skill
        spirit_skill = None if raw_loadout is None else raw_loadout.spirit_skill
        main_skill_name = path_definition.name if main_skill is None else main_skill.skill_name
        return ResolvedSkillContext(
            main_axis_id=resolved_main_axis_id,
            main_path_id=resolved_main_path_id,
            main_path_name=path_definition.name,
            main_skill_name=main_skill_name,
            preferred_scene=path_definition.preferred_scene,
            behavior_template_id=resolved_behavior_template_id,
            main_skill=main_skill,
            guard_skill=guard_skill,
            movement_skill=movement_skill,
            spirit_skill=spirit_skill,
            fallback_applied=fallback_applied,
        )

    @staticmethod
    def _serialize_skill_item(skill_item: ScoreSkillItemInput | None) -> dict[str, object] | None:
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

    def _score_public_stats(self, stats: tuple[ScoreStatInput, ...]) -> int:
        return sum(self._weighted_stat_value(stat, weight_map=_PUBLIC_STAT_WEIGHT_BY_ID) for stat in stats)

    def _score_pvp_stats(self, stats: tuple[ScoreStatInput, ...]) -> int:
        return sum(self._weighted_stat_value(stat, weight_map=_PVP_STAT_WEIGHT_BY_ID, default_weight=Decimal("0")) for stat in stats)

    def _weighted_stat_value(
        self,
        stat: ScoreStatInput,
        *,
        weight_map: dict[str, Decimal],
        default_weight: Decimal | None = None,
    ) -> int:
        weight = weight_map.get(stat.stat_id)
        if weight is None:
            if default_weight is not None:
                weight = default_weight
            elif stat.stat_id.endswith("_permille"):
                weight = Decimal("0.70")
            else:
                weight = Decimal("0.20") if "hp" in stat.stat_id else Decimal("0.50")
        return self._scale_decimal(Decimal(stat.value), weight)

    def _score_public_affixes(
        self,
        affixes: tuple[ScoreAffixInput, ...],
        *,
        is_artifact: bool,
    ) -> tuple[int, list[dict[str, object]]]:
        total_score = 0
        special_effect_breakdown: list[dict[str, object]] = []
        for affix in affixes:
            tier_order = self._affix_tier_order_by_id.get(affix.tier_id, 1)
            specialization_bonus = 8 if affix.is_pve_specialized or affix.is_pvp_specialized else 0
            if is_artifact:
                total_score += 24 + tier_order * 14 + self._scale_decimal(Decimal(affix.value), Decimal("0.12")) + specialization_bonus
            else:
                total_score += 20 + tier_order * 12 + self._scale_decimal(Decimal(affix.value), Decimal("0.10")) + specialization_bonus
            effect_score = self._score_public_special_effect(affix.special_effect, tier_order=tier_order, is_artifact=is_artifact)
            if effect_score == 0:
                continue
            total_score += effect_score
            if affix.special_effect is not None:
                special_effect_breakdown.append(
                    {
                        "effect_id": affix.special_effect.effect_id,
                        "effect_type": affix.special_effect.effect_type,
                        "trigger_event": affix.special_effect.trigger_event,
                        "score": effect_score,
                        "public_score_key": affix.special_effect.public_score_key,
                    }
                )
        return total_score, special_effect_breakdown

    def _score_public_special_effect(
        self,
        effect: ScoreSpecialEffectInput | None,
        *,
        tier_order: int,
        is_artifact: bool,
    ) -> int:
        if effect is None:
            return 0
        if effect.public_score_key is None:
            return 0
        offset = _PUBLIC_SPECIAL_EFFECT_OFFSET_BY_KEY.get(effect.public_score_key)
        if offset is None:
            return 0
        score = _SPECIAL_EFFECT_BASE_SCORE + offset + tier_order * _SPECIAL_EFFECT_TIER_STEP
        score += self._scale_special_effect_strength_bonus(effect.strength_multiplier_permille)
        if is_artifact:
            score += _SPECIAL_EFFECT_ARTIFACT_BONUS
        return score

    def _score_hidden_pvp_special_effect(self, effect: ScoreSpecialEffectInput | None, *, tier_order: int) -> int:
        if effect is None:
            return 0
        if effect.hidden_pvp_score_key is None:
            return 0
        offset = _HIDDEN_PVP_SPECIAL_EFFECT_OFFSET_BY_KEY.get(effect.hidden_pvp_score_key)
        if offset is None:
            return 0
        return (
            _SPECIAL_EFFECT_PVP_BASE
            + offset
            + tier_order * _SPECIAL_EFFECT_PVP_TIER_STEP
            + self._scale_special_effect_strength_bonus(effect.strength_multiplier_permille)
        )

    @staticmethod
    def _scale_decimal(value: Decimal, factor: Decimal) -> int:
        return int((value * factor).to_integral_value(rounding=ROUND_HALF_UP))

    @staticmethod
    def _scale_special_effect_strength_bonus(strength_multiplier_permille: int) -> int:
        overflow_permille = max(0, strength_multiplier_permille - 1000)
        if overflow_permille == 0:
            return 0
        return int((Decimal(overflow_permille) * Decimal("0.08")).to_integral_value(rounding=ROUND_HALF_UP))

    @staticmethod
    def _clamp_ratio(*, numerator: int, denominator: int) -> Decimal:
        if denominator <= 0:
            return Decimal("0")
        ratio = Decimal(max(0, numerator)) / Decimal(denominator)
        if ratio < Decimal("0"):
            return Decimal("0")
        if ratio > Decimal("1"):
            return Decimal("1")
        return ratio

    @staticmethod
    def _scale_ratio(ratio: Decimal, scale: int) -> int:
        return int((ratio * Decimal(scale)).to_integral_value(rounding=ROUND_HALF_UP))

    @staticmethod
    def _ratio_to_permille(ratio: Decimal) -> int:
        return int((ratio * Decimal(1000)).to_integral_value(rounding=ROUND_HALF_UP))


__all__ = [
    "CharacterScoreRuleService",
    "ResolvedSkillContext",
]
