"""装备领域规则服务。"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace
from decimal import Decimal, ROUND_HALF_UP

from infrastructure.config.static.models.common import StaticGameConfig
from infrastructure.config.static.models.equipment import (
    AffixTierDefinition,
    ArtifactNurtureLevelDefinition,
    ArtifactTemplateDefinition,
    DismantleQualityRuleDefinition,
    EnhancementLevelDefinition,
    EquipmentAffixDefinition,
    EquipmentBaseTemplateDefinition,
    EquipmentNameTemplateDefinition,
    EquipmentQualityDefinition,
    EquipmentRankDefinition,
    EquipmentSlotDefinition,
    EquipmentStatValueDefinition,
    ResourceAmountDefinition,
)

from domain.equipment.models import (
    ArtifactNurtureResult,
    EquipmentAffixValue,
    EquipmentAttributeValue,
    EquipmentDismantleResult,
    EquipmentEnhancementResult,
    EquipmentGenerationRequest,
    EquipmentItem,
    EquipmentNamingRecord,
    EquipmentRandomSource,
    EquipmentReforgeResult,
    EquipmentResourceCost,
    EquipmentSpecialEffectValue,
    EquipmentWashResult,
)

_DECIMAL_ONE_THOUSAND = Decimal("1000")
_ALLOWED_PRIMARY_AFFIX_CATEGORIES = frozenset({"base_stat", "combat_bonus", "special_pattern"})
_TEMPLATE_NAMING_SOURCE = "template_rule"
_SPECIAL_AFFIX_KIND = "special_effect"


class EquipmentRuleError(ValueError):
    """装备规则输入不合法。"""


@dataclass(frozen=True, slots=True)
class EquipmentGenerationRule:
    """装备生成规则入口。"""

    static_config: StaticGameConfig

    def generate_equipment(
        self,
        *,
        request: EquipmentGenerationRequest,
        random_source: EquipmentRandomSource,
    ) -> EquipmentItem:
        """按配置生成一件普通装备或法宝。"""
        slot = self._require_slot(request.slot_id)
        quality = self._require_quality(request.quality_id)
        rank = self._require_rank(request.rank_id)
        affix_count = request.affix_count if request.affix_count is not None else quality.base_affix_count
        if affix_count < 0:
            raise EquipmentRuleError("词条数量不能为负数")

        if slot.slot_id == "artifact":
            template = self._pick_artifact_template(template_id=request.template_id, random_source=random_source)
            base_attributes = self._scale_attributes(
                self._build_attributes(template.attributes),
                rank.base_attribute_multiplier * quality.attribute_multiplier,
            )
            affixes = self._roll_affixes(
                slot_id=slot.slot_id,
                quality=quality,
                rank=rank,
                preferred_affix_ids=template.preferred_affix_ids,
                affix_count=affix_count,
                random_source=random_source,
            )
            return EquipmentItem(
                slot_id=slot.slot_id,
                slot_name=slot.name,
                quality_id=quality.quality_id,
                quality_name=quality.name,
                template_id=template.template_id,
                template_name=template.name,
                rank_id=rank.rank_id,
                rank_name=rank.name,
                rank_order=rank.order,
                mapped_realm_id=rank.mapped_realm_id,
                is_artifact=True,
                resonance_name=template.resonance_name,
                enhancement_level=0,
                artifact_nurture_level=0,
                base_attributes=base_attributes,
                affixes=affixes,
                base_attribute_multiplier=rank.base_attribute_multiplier,
                affix_base_value_multiplier=rank.affix_base_value_multiplier,
                dismantle_reward_multiplier=rank.dismantle_reward_multiplier,
            )

        template = self._pick_base_template(
            slot_id=slot.slot_id,
            template_id=request.template_id,
            random_source=random_source,
        )
        base_attributes = self._scale_attributes(
            self._build_attributes(template.attributes),
            rank.base_attribute_multiplier * quality.attribute_multiplier,
        )
        affixes = self._roll_affixes(
            slot_id=slot.slot_id,
            quality=quality,
            rank=rank,
            preferred_affix_ids=template.preferred_affix_ids,
            affix_count=affix_count,
            random_source=random_source,
        )
        return EquipmentItem(
            slot_id=slot.slot_id,
            slot_name=slot.name,
            quality_id=quality.quality_id,
            quality_name=quality.name,
            template_id=template.template_id,
            template_name=template.name,
            rank_id=rank.rank_id,
            rank_name=rank.name,
            rank_order=rank.order,
            mapped_realm_id=rank.mapped_realm_id,
            is_artifact=False,
            resonance_name=None,
            enhancement_level=0,
            artifact_nurture_level=0,
            base_attributes=base_attributes,
            affixes=affixes,
            base_attribute_multiplier=rank.base_attribute_multiplier,
            affix_base_value_multiplier=rank.affix_base_value_multiplier,
            dismantle_reward_multiplier=rank.dismantle_reward_multiplier,
        )

    def _pick_base_template(
        self,
        *,
        slot_id: str,
        template_id: str | None,
        random_source: EquipmentRandomSource,
    ) -> EquipmentBaseTemplateDefinition:
        candidates = [
            template for template in self.static_config.equipment.base_templates if template.slot_id == slot_id
        ]
        if template_id is not None:
            for candidate in candidates:
                if candidate.template_id == template_id:
                    return candidate
            raise EquipmentRuleError(f"未找到部位 {slot_id} 对应的装备底材模板：{template_id}")
        if not candidates:
            raise EquipmentRuleError(f"当前部位缺少可用装备底材模板：{slot_id}")
        return self._weighted_pick(candidates, random_source=random_source, weight_getter=lambda item: item.generation_weight)

    def _pick_artifact_template(
        self,
        *,
        template_id: str | None,
        random_source: EquipmentRandomSource,
    ) -> ArtifactTemplateDefinition:
        candidates = list(self.static_config.equipment.artifact_templates)
        if template_id is not None:
            for candidate in candidates:
                if candidate.template_id == template_id:
                    return candidate
            raise EquipmentRuleError(f"未找到法宝模板：{template_id}")
        if not candidates:
            raise EquipmentRuleError("当前配置缺少可用法宝模板")
        return self._weighted_pick(candidates, random_source=random_source, weight_getter=lambda item: item.generation_weight)

    def _roll_affixes(
        self,
        *,
        slot_id: str,
        quality: EquipmentQualityDefinition,
        rank: EquipmentRankDefinition,
        preferred_affix_ids: tuple[str, ...],
        affix_count: int,
        random_source: EquipmentRandomSource,
        occupied_affixes: tuple[EquipmentAffixValue, ...] = (),
        allow_new_special_affixes: bool = True,
    ) -> tuple[EquipmentAffixValue, ...]:
        if affix_count == 0:
            return ()

        selected_affixes: list[EquipmentAffixValue] = []
        selected_numeric_ids = {affix.affix_id for affix in occupied_affixes if not affix.is_special}
        selected_effect_ids = {
            effect_id
            for effect_id in (affix.special_effect_id for affix in occupied_affixes)
            if effect_id is not None
        }

        remaining_special_capacity = self._resolve_remaining_special_capacity(
            slot_id=slot_id,
            quality=quality,
            occupied_affixes=occupied_affixes,
            allow_new_special_affixes=allow_new_special_affixes,
        )
        if remaining_special_capacity > 0:
            special_affixes = self._roll_special_affixes(
                slot_id=slot_id,
                quality=quality,
                rank=rank,
                preferred_affix_ids=preferred_affix_ids,
                affix_count=min(affix_count, remaining_special_capacity),
                random_source=random_source,
                occupied_affix_ids=selected_numeric_ids,
                occupied_effect_ids=selected_effect_ids,
            )
            selected_affixes.extend(special_affixes)
            selected_numeric_ids.update(affix.affix_id for affix in special_affixes)
            selected_effect_ids.update(
                effect_id
                for effect_id in (affix.special_effect_id for affix in special_affixes)
                if effect_id is not None
            )

        remaining_numeric_count = affix_count - len(selected_affixes)
        if remaining_numeric_count > 0:
            numeric_affixes = self._roll_numeric_affixes(
                slot_id=slot_id,
                quality=quality,
                rank=rank,
                preferred_affix_ids=preferred_affix_ids,
                affix_count=remaining_numeric_count,
                random_source=random_source,
                occupied_affix_ids=selected_numeric_ids,
            )
            selected_affixes.extend(numeric_affixes)
        return tuple(selected_affixes)

    def _roll_numeric_affixes(
        self,
        *,
        slot_id: str,
        quality: EquipmentQualityDefinition,
        rank: EquipmentRankDefinition,
        preferred_affix_ids: tuple[str, ...],
        affix_count: int,
        random_source: EquipmentRandomSource,
        occupied_affix_ids: set[str],
    ) -> tuple[EquipmentAffixValue, ...]:
        selected_affixes: list[EquipmentAffixValue] = []
        selected_ids = set(occupied_affix_ids)
        remaining_candidates = [
            affix
            for affix in self.static_config.equipment.ordered_affixes
            if affix.affix_kind != _SPECIAL_AFFIX_KIND
        ]

        while len(selected_affixes) < affix_count:
            candidates = [
                affix
                for affix in remaining_candidates
                if slot_id in affix.slot_ids
                and self._quality_allows_affix(quality=quality, affix=affix)
                and (
                    self.static_config.equipment.affix_generation.duplicate_affix_allowed
                    or affix.affix_id not in selected_ids
                )
            ]
            if not candidates:
                break
            picked_affix = self._weighted_pick(
                candidates,
                random_source=random_source,
                weight_getter=lambda item: self._resolve_affix_weight(
                    affix=item,
                    quality=quality,
                    preferred_affix_ids=preferred_affix_ids,
                    slot_id=slot_id,
                ),
            )
            affix_value = self._build_affix_value(
                affix=picked_affix,
                quality=quality,
                rank=rank,
                random_source=random_source,
            )
            selected_affixes.append(affix_value)
            selected_ids.add(picked_affix.affix_id)
            if not self.static_config.equipment.affix_generation.duplicate_affix_allowed:
                remaining_candidates = [item for item in remaining_candidates if item.affix_id != picked_affix.affix_id]
        return tuple(selected_affixes)

    def _roll_special_affixes(
        self,
        *,
        slot_id: str,
        quality: EquipmentQualityDefinition,
        rank: EquipmentRankDefinition,
        preferred_affix_ids: tuple[str, ...],
        affix_count: int,
        random_source: EquipmentRandomSource,
        occupied_affix_ids: set[str],
        occupied_effect_ids: set[str],
    ) -> tuple[EquipmentAffixValue, ...]:
        if affix_count <= 0:
            return ()

        selected_affixes: list[EquipmentAffixValue] = []
        selected_affix_ids = set(occupied_affix_ids)
        selected_effect_ids = set(occupied_effect_ids)
        remaining_candidates = self._resolve_special_affix_candidates(slot_id=slot_id, quality=quality, rank=rank)

        while len(selected_affixes) < affix_count:
            candidates = [
                affix
                for affix in remaining_candidates
                if affix.affix_id not in selected_affix_ids
                and self._quality_allows_affix(quality=quality, affix=affix)
                and self._special_effect_is_available(affix=affix, occupied_effect_ids=selected_effect_ids)
            ]
            if not candidates:
                break
            picked_affix = self._weighted_pick(
                candidates,
                random_source=random_source,
                weight_getter=lambda item: self._resolve_affix_weight(
                    affix=item,
                    quality=quality,
                    preferred_affix_ids=preferred_affix_ids,
                    slot_id=slot_id,
                ),
            )
            affix_value = self._build_affix_value(
                affix=picked_affix,
                quality=quality,
                rank=rank,
                random_source=random_source,
            )
            selected_affixes.append(affix_value)
            selected_affix_ids.add(picked_affix.affix_id)
            if affix_value.special_effect_id is not None:
                selected_effect_ids.add(affix_value.special_effect_id)
            remaining_candidates = [item for item in remaining_candidates if item.affix_id != picked_affix.affix_id]
        return tuple(selected_affixes)

    def _resolve_special_affix_candidates(
        self,
        *,
        slot_id: str,
        quality: EquipmentQualityDefinition,
        rank: EquipmentRankDefinition,
    ) -> list[EquipmentAffixDefinition]:
        if not self._quality_allows_special_affixes(quality=quality):
            return []
        matched_affix_ids: list[str] = []
        for pool in self.static_config.equipment.special_affix_generation.pools:
            if slot_id not in pool.slot_ids:
                continue
            if quality.quality_id not in pool.quality_ids:
                continue
            if rank.rank_id not in pool.rank_ids:
                continue
            matched_affix_ids.extend(pool.affix_ids)
        candidates: list[EquipmentAffixDefinition] = []
        for affix_id in matched_affix_ids:
            affix = self.static_config.equipment.get_affix(affix_id)
            if affix is None or affix.affix_kind != _SPECIAL_AFFIX_KIND:
                continue
            candidates.append(affix)
        return candidates

    def _special_effect_is_available(
        self,
        *,
        affix: EquipmentAffixDefinition,
        occupied_effect_ids: set[str],
    ) -> bool:
        if affix.special_effect_id is None:
            return False
        if self.static_config.equipment.special_affix_generation.duplicate_effect_per_item_allowed:
            return True
        return affix.special_effect_id not in occupied_effect_ids

    def _resolve_remaining_special_capacity(
        self,
        *,
        slot_id: str,
        quality: EquipmentQualityDefinition,
        occupied_affixes: tuple[EquipmentAffixValue, ...],
        allow_new_special_affixes: bool,
    ) -> int:
        if not allow_new_special_affixes:
            return 0
        if not self._quality_allows_special_affixes(quality=quality):
            return 0
        max_count = self._resolve_special_affix_cap(slot_id=slot_id)
        current_special_count = sum(1 for affix in occupied_affixes if affix.is_special)
        return max(0, max_count - current_special_count)

    def _resolve_special_affix_cap(self, *, slot_id: str) -> int:
        generation_config = self.static_config.equipment.special_affix_generation
        if slot_id == "artifact":
            return generation_config.artifact_max_count
        return generation_config.equipment_max_count

    def _quality_allows_special_affixes(self, *, quality: EquipmentQualityDefinition) -> bool:
        minimum_quality = self._require_quality(self.static_config.equipment.special_affix_generation.minimum_quality_id)
        return quality.order >= minimum_quality.order

    def _build_affix_value(
        self,
        *,
        affix: EquipmentAffixDefinition,
        quality: EquipmentQualityDefinition,
        rank: EquipmentRankDefinition,
        random_source: EquipmentRandomSource,
    ) -> EquipmentAffixValue:
        tier = self._pick_affix_tier(affix=affix, quality=quality, random_source=random_source)
        multiplier = self._roll_decimal_between(
            minimum=tier.min_multiplier,
            maximum=tier.max_multiplier,
            random_source=random_source,
        )
        value = 0
        if affix.base_value > 0:
            value = int(
                (Decimal(affix.base_value) * rank.affix_base_value_multiplier * multiplier).to_integral_value(
                    rounding=ROUND_HALF_UP
                )
            )
        special_effect = self._build_special_effect_value(affix=affix)
        return EquipmentAffixValue(
            affix_id=affix.affix_id,
            affix_name=affix.name,
            stat_id="" if affix.stat_id is None else affix.stat_id,
            category=affix.category,
            tier_id=tier.tier_id,
            tier_name=tier.name,
            rolled_multiplier=multiplier,
            value=value,
            is_pve_specialized=affix.is_pve_specialized,
            is_pvp_specialized=affix.is_pvp_specialized,
            affix_kind=affix.affix_kind,
            special_effect=special_effect,
        )

    def _build_special_effect_value(self, *, affix: EquipmentAffixDefinition) -> EquipmentSpecialEffectValue | None:
        if affix.affix_kind != _SPECIAL_AFFIX_KIND:
            return None
        if affix.special_effect_id is None:
            raise EquipmentRuleError(f"特殊词条缺少特殊效果定义：{affix.affix_id}")
        effect_definition = self.static_config.equipment.get_special_effect(affix.special_effect_id)
        if effect_definition is None:
            raise EquipmentRuleError(f"未找到特殊效果定义：{affix.special_effect_id}")
        return EquipmentSpecialEffectValue(
            effect_id=effect_definition.effect_id,
            effect_name=effect_definition.name,
            effect_type=effect_definition.effect_type,
            trigger_event=effect_definition.trigger_event,
            payload=effect_definition.payload,
            public_score_key=effect_definition.public_score_key,
            hidden_pvp_score_key=effect_definition.hidden_pvp_score_key,
        )

    def _pick_affix_tier(
        self,
        *,
        affix: EquipmentAffixDefinition,
        quality: EquipmentQualityDefinition,
        random_source: EquipmentRandomSource,
    ) -> AffixTierDefinition:
        allowed_tier_ids = set(affix.tier_ids)
        quality_limit = self._require_affix_tier(quality.max_affix_tier_id)
        quality_tier_order = quality_limit.order
        weighted_candidates: list[tuple[AffixTierDefinition, int]] = []
        for item in self.static_config.equipment.affix_generation.tier_weights:
            tier = self._require_affix_tier(item.tier_id)
            if tier.tier_id not in allowed_tier_ids:
                continue
            if tier.order > quality_tier_order:
                continue
            weighted_candidates.append((tier, item.weight))
        if not weighted_candidates:
            raise EquipmentRuleError(f"词条 {affix.affix_id} 在品质 {quality.quality_id} 下不存在可用档位")
        return self._weighted_pick(weighted_candidates, random_source=random_source, weight_getter=lambda item: item[1])[0]

    def _resolve_affix_weight(
        self,
        *,
        affix: EquipmentAffixDefinition,
        quality: EquipmentQualityDefinition,
        preferred_affix_ids: tuple[str, ...],
        slot_id: str,
    ) -> int:
        weight = Decimal(affix.selection_weight)
        generation_config = self.static_config.equipment.affix_generation
        if affix.affix_id in preferred_affix_ids:
            multiplier = (
                generation_config.artifact_preferred_affix_weight_multiplier
                if slot_id == "artifact"
                else generation_config.preferred_affix_weight_multiplier
            )
            weight *= multiplier
        if affix.is_pve_specialized or affix.is_pvp_specialized:
            minimum_quality = self._require_quality(generation_config.minimum_specialized_quality_id)
            if quality.order < minimum_quality.order:
                return 0
            weight *= generation_config.specialized_affix_weight_multiplier
        return max(1, int(weight.to_integral_value(rounding=ROUND_HALF_UP)))

    def _quality_allows_affix(self, *, quality: EquipmentQualityDefinition, affix: EquipmentAffixDefinition) -> bool:
        max_tier = self._require_affix_tier(quality.max_affix_tier_id)
        allowed_tiers = [self._require_affix_tier(tier_id) for tier_id in affix.tier_ids]
        return any(tier.order <= max_tier.order for tier in allowed_tiers)

    @staticmethod
    def _scale_attributes(
        attributes: tuple[EquipmentAttributeValue, ...],
        multiplier: Decimal,
    ) -> tuple[EquipmentAttributeValue, ...]:
        scaled_attributes: list[EquipmentAttributeValue] = []
        for attribute in attributes:
            scaled_value = int((Decimal(attribute.value) * multiplier).to_integral_value(rounding=ROUND_HALF_UP))
            scaled_attributes.append(EquipmentAttributeValue(stat_id=attribute.stat_id, value=scaled_value))
        return tuple(scaled_attributes)

    @staticmethod
    def _build_attributes(attributes: tuple[EquipmentStatValueDefinition, ...]) -> tuple[EquipmentAttributeValue, ...]:
        return tuple(EquipmentAttributeValue(stat_id=item.stat_id, value=item.value) for item in attributes)

    def _require_slot(self, slot_id: str) -> EquipmentSlotDefinition:
        slot = self.static_config.equipment.get_slot(slot_id)
        if slot is None:
            raise EquipmentRuleError(f"未找到装备部位定义：{slot_id}")
        return slot

    def _require_rank(self, rank_id: str) -> EquipmentRankDefinition:
        rank = self.static_config.equipment.get_equipment_rank(rank_id)
        if rank is None:
            raise EquipmentRuleError(f"未找到装备阶数定义：{rank_id}")
        return rank

    def _require_quality(self, quality_id: str) -> EquipmentQualityDefinition:
        quality = self.static_config.equipment.get_quality(quality_id)
        if quality is None:
            raise EquipmentRuleError(f"未找到装备品质定义：{quality_id}")
        return quality

    def _require_affix_tier(self, tier_id: str) -> AffixTierDefinition:
        tier = self.static_config.equipment.get_affix_tier(tier_id)
        if tier is None:
            raise EquipmentRuleError(f"未找到词条档位定义：{tier_id}")
        return tier

    @staticmethod
    def _weighted_pick(items, *, random_source: EquipmentRandomSource, weight_getter):
        total_weight = sum(max(0, weight_getter(item)) for item in items)
        if total_weight <= 0:
            raise EquipmentRuleError("当前候选池权重总和必须大于 0")
        cursor = random_source.randrange(total_weight)
        running = 0
        for item in items:
            weight = max(0, weight_getter(item))
            running += weight
            if cursor < running:
                return item
        return items[-1]

    @staticmethod
    def _roll_decimal_between(
        *,
        minimum: Decimal,
        maximum: Decimal,
        random_source: EquipmentRandomSource,
    ) -> Decimal:
        if minimum == maximum:
            return minimum
        scaled_min = int((minimum * _DECIMAL_ONE_THOUSAND).to_integral_value(rounding=ROUND_HALF_UP))
        scaled_max = int((maximum * _DECIMAL_ONE_THOUSAND).to_integral_value(rounding=ROUND_HALF_UP))
        sampled = scaled_min + random_source.randrange((scaled_max - scaled_min) + 1)
        return Decimal(sampled) / _DECIMAL_ONE_THOUSAND


@dataclass(frozen=True, slots=True)
class EquipmentEnhancementRule:
    """装备强化规则。"""

    static_config: StaticGameConfig
    generation_rule: EquipmentGenerationRule

    def enhance(
        self,
        *,
        item: EquipmentItem,
        random_source: EquipmentRandomSource,
    ) -> EquipmentEnhancementResult:
        """执行一次强化尝试。失败只消耗资源，不掉级。"""
        quality = self._require_quality(item.quality_id)
        target_level = item.enhancement_level + 1
        if target_level > quality.max_enhancement_level:
            raise EquipmentRuleError(f"装备品质 {item.quality_id} 已达到强化上限 {quality.max_enhancement_level}")

        level_rule = self._require_enhancement_level(target_level)
        costs = self._build_costs(level_rule.costs)
        success = Decimal(str(random_source.random())) < level_rule.success_rate
        if not success:
            return EquipmentEnhancementResult(
                item=item,
                success=False,
                previous_level=item.enhancement_level,
                target_level=target_level,
                success_rate=level_rule.success_rate,
                costs=costs,
            )

        added_affixes = self._roll_bonus_affixes(
            item=item,
            target_level=target_level,
            bonus_affix_unlock_count=level_rule.bonus_affix_unlock_count,
            random_source=random_source,
        )
        enhanced_item = replace(
            item,
            enhancement_level=target_level,
            enhancement_base_stat_bonus_ratio=item.enhancement_base_stat_bonus_ratio + level_rule.base_stat_bonus_ratio,
            enhancement_affix_bonus_ratio=item.enhancement_affix_bonus_ratio + level_rule.affix_bonus_ratio,
            affixes=item.affixes + added_affixes,
        )
        return EquipmentEnhancementResult(
            item=enhanced_item,
            success=True,
            previous_level=item.enhancement_level,
            target_level=target_level,
            success_rate=level_rule.success_rate,
            costs=costs,
            added_affixes=added_affixes,
        )

    def _roll_bonus_affixes(
        self,
        *,
        item: EquipmentItem,
        target_level: int,
        bonus_affix_unlock_count: int,
        random_source: EquipmentRandomSource,
    ) -> tuple[EquipmentAffixValue, ...]:
        if bonus_affix_unlock_count == 0:
            return ()
        quality = self._require_quality(item.quality_id)
        rank = self.generation_rule._require_rank(item.rank_id)
        preferred_affix_ids = self._resolve_preferred_affix_ids(item)
        new_affixes = self.generation_rule._roll_affixes(
            slot_id=item.slot_id,
            quality=quality,
            rank=rank,
            preferred_affix_ids=preferred_affix_ids,
            affix_count=bonus_affix_unlock_count,
            random_source=random_source,
            occupied_affixes=item.affixes,
            allow_new_special_affixes=True,
        )
        if len(new_affixes) < bonus_affix_unlock_count:
            raise EquipmentRuleError(f"强化等级 {target_level} 需要新增词条，但当前词条池不足")
        return new_affixes

    def _resolve_preferred_affix_ids(self, item: EquipmentItem) -> tuple[str, ...]:
        if item.is_artifact:
            template = self.static_config.equipment.get_artifact_template(item.template_id)
            if template is None:
                raise EquipmentRuleError(f"未找到法宝模板：{item.template_id}")
            return template.preferred_affix_ids
        template = self.static_config.equipment.get_base_template(item.template_id)
        if template is None:
            raise EquipmentRuleError(f"未找到装备底材模板：{item.template_id}")
        return template.preferred_affix_ids

    def _require_quality(self, quality_id: str) -> EquipmentQualityDefinition:
        quality = self.static_config.equipment.get_quality(quality_id)
        if quality is None:
            raise EquipmentRuleError(f"未找到装备品质定义：{quality_id}")
        return quality

    def _require_enhancement_level(self, target_level: int) -> EnhancementLevelDefinition:
        level = self.static_config.equipment.get_enhancement_level(target_level)
        if level is None:
            raise EquipmentRuleError(f"未找到强化等级规则：{target_level}")
        return level

    @staticmethod
    def _build_costs(costs: tuple[ResourceAmountDefinition, ...]) -> tuple[EquipmentResourceCost, ...]:
        return tuple(EquipmentResourceCost(resource_id=item.resource_id, quantity=item.quantity) for item in costs)


@dataclass(frozen=True, slots=True)
class EquipmentAffixOperationRule:
    """洗炼与重铸规则。"""

    static_config: StaticGameConfig
    generation_rule: EquipmentGenerationRule

    def wash(
        self,
        *,
        item: EquipmentItem,
        locked_affix_indices: tuple[int, ...],
        random_source: EquipmentRandomSource,
    ) -> EquipmentWashResult:
        """保留部分词条并重洗其余词条。"""
        self._validate_lock_indices(item=item, locked_affix_indices=locked_affix_indices)
        locked_index_set = set(locked_affix_indices)
        locked_affixes = tuple(item.affixes[index] for index in sorted(locked_index_set))
        preserved_special_affixes = tuple(
            affix
            for index, affix in enumerate(item.affixes)
            if index not in locked_index_set and affix.is_special
        )
        reroll_positions = [
            index
            for index, affix in enumerate(item.affixes)
            if index not in locked_index_set and not affix.is_special
        ]
        reroll_count = len(reroll_positions)
        quality = self._require_quality(item.quality_id)
        rank = self.generation_rule._require_rank(item.rank_id)
        preferred_affix_ids = self._resolve_preferred_affix_ids(item)
        rerolled_affixes = self.generation_rule._roll_affixes(
            slot_id=item.slot_id,
            quality=quality,
            rank=rank,
            preferred_affix_ids=preferred_affix_ids,
            affix_count=reroll_count,
            random_source=random_source,
            occupied_affixes=locked_affixes + preserved_special_affixes,
            allow_new_special_affixes=False,
        )
        if len(rerolled_affixes) != reroll_count:
            raise EquipmentRuleError("当前配置下无法完成本次洗炼，请检查词条池覆盖")

        final_affixes = list(item.affixes)
        for index, affix in zip(reroll_positions, rerolled_affixes, strict=True):
            final_affixes[index] = affix

        lock_count = len(locked_index_set)
        costs = self._build_costs(
            base_costs=self.static_config.equipment.wash.base_costs,
            extra_costs=self.static_config.equipment.wash.lock_extra_costs,
            multiplier=lock_count,
        )
        washed_item = replace(item, affixes=tuple(final_affixes))
        return EquipmentWashResult(
            item=washed_item,
            locked_affix_indices=tuple(sorted(locked_index_set)),
            costs=costs,
            rerolled_affixes=rerolled_affixes,
        )

    def reforge(
        self,
        *,
        item: EquipmentItem,
        random_source: EquipmentRandomSource,
    ) -> EquipmentReforgeResult:
        """对当前装备执行重铸。"""
        request = EquipmentGenerationRequest(
            slot_id=item.slot_id,
            quality_id=item.quality_id,
            rank_id=item.rank_id,
            template_id=None if self.static_config.equipment.reforge.allow_template_change else item.template_id,
            affix_count=len(item.affixes),
        )
        regenerated = self.generation_rule.generate_equipment(request=request, random_source=random_source)
        reforged_item = replace(
            regenerated,
            enhancement_level=item.enhancement_level if self.static_config.equipment.reforge.preserve_enhancement_level else 0,
            enhancement_base_stat_bonus_ratio=(
                item.enhancement_base_stat_bonus_ratio
                if self.static_config.equipment.reforge.preserve_enhancement_level
                else Decimal("0")
            ),
            enhancement_affix_bonus_ratio=(
                item.enhancement_affix_bonus_ratio
                if self.static_config.equipment.reforge.preserve_enhancement_level
                else Decimal("0")
            ),
            artifact_nurture_level=item.artifact_nurture_level if self.static_config.equipment.reforge.preserve_nurture_level else 0,
            nurture_base_stat_bonus_ratio=(
                item.nurture_base_stat_bonus_ratio
                if self.static_config.equipment.reforge.preserve_nurture_level
                else Decimal("0")
            ),
            nurture_affix_bonus_ratio=(
                item.nurture_affix_bonus_ratio
                if self.static_config.equipment.reforge.preserve_nurture_level
                else Decimal("0")
            ),
        )
        return EquipmentReforgeResult(
            item=reforged_item,
            previous_template_id=item.template_id,
            previous_affixes=item.affixes,
            costs=tuple(
                EquipmentResourceCost(resource_id=cost.resource_id, quantity=cost.quantity)
                for cost in self.static_config.equipment.reforge.costs
            ),
        )

    def _validate_lock_indices(self, *, item: EquipmentItem, locked_affix_indices: tuple[int, ...]) -> None:
        unique_indices = set(locked_affix_indices)
        if len(unique_indices) > self.static_config.equipment.wash.max_locked_affix_count:
            raise EquipmentRuleError("锁定词条数量超过洗炼上限")
        for index in unique_indices:
            if index < 0 or index >= len(item.affixes):
                raise EquipmentRuleError(f"锁定词条下标越界：{index}")

    def _resolve_preferred_affix_ids(self, item: EquipmentItem) -> tuple[str, ...]:
        if item.is_artifact:
            template = self.static_config.equipment.get_artifact_template(item.template_id)
            if template is None:
                raise EquipmentRuleError(f"未找到法宝模板：{item.template_id}")
            return template.preferred_affix_ids
        template = self.static_config.equipment.get_base_template(item.template_id)
        if template is None:
            raise EquipmentRuleError(f"未找到装备底材模板：{item.template_id}")
        return template.preferred_affix_ids

    def _require_quality(self, quality_id: str) -> EquipmentQualityDefinition:
        quality = self.static_config.equipment.get_quality(quality_id)
        if quality is None:
            raise EquipmentRuleError(f"未找到装备品质定义：{quality_id}")
        return quality

    @staticmethod
    def _build_costs(
        *,
        base_costs: tuple[ResourceAmountDefinition, ...],
        extra_costs: tuple[ResourceAmountDefinition, ...],
        multiplier: int,
    ) -> tuple[EquipmentResourceCost, ...]:
        total_costs: dict[str, int] = defaultdict(int)
        for cost in base_costs:
            total_costs[cost.resource_id] += cost.quantity
        for cost in extra_costs:
            total_costs[cost.resource_id] += cost.quantity * multiplier
        return tuple(
            EquipmentResourceCost(resource_id=resource_id, quantity=quantity)
            for resource_id, quantity in sorted(total_costs.items(), key=lambda item: item[0])
        )


@dataclass(frozen=True, slots=True)
class ArtifactNurtureRule:
    """法宝培养规则。"""

    static_config: StaticGameConfig

    def nurture(self, *, item: EquipmentItem) -> ArtifactNurtureResult:
        """执行一次法宝培养。"""
        if not item.is_artifact:
            raise EquipmentRuleError("只有法宝可以执行培养")
        target_level = item.artifact_nurture_level + 1
        level_rule = self._require_nurture_level(target_level)
        nurtured_item = replace(
            item,
            artifact_nurture_level=target_level,
            nurture_base_stat_bonus_ratio=item.nurture_base_stat_bonus_ratio + level_rule.base_stat_bonus_ratio,
            nurture_affix_bonus_ratio=item.nurture_affix_bonus_ratio + level_rule.affix_bonus_ratio,
        )
        return ArtifactNurtureResult(
            item=nurtured_item,
            previous_level=item.artifact_nurture_level,
            target_level=target_level,
            costs=tuple(
                EquipmentResourceCost(resource_id=cost.resource_id, quantity=cost.quantity)
                for cost in level_rule.costs
            ),
        )

    def _require_nurture_level(self, target_level: int) -> ArtifactNurtureLevelDefinition:
        level = self.static_config.equipment.get_artifact_nurture_level(target_level)
        if level is None:
            raise EquipmentRuleError(f"未找到法宝培养等级规则：{target_level}")
        return level


@dataclass(frozen=True, slots=True)
class EquipmentDismantleRule:
    """装备分解规则。"""

    static_config: StaticGameConfig

    def dismantle(self, *, item: EquipmentItem) -> EquipmentDismantleResult:
        """按品质、强化与词条数量计算分解回收。"""
        rule = self._require_rule(item.quality_id)
        returns: dict[str, int] = defaultdict(int)
        self._accumulate(returns=returns, resources=rule.base_returns, multiplier=1)
        self._accumulate(returns=returns, resources=rule.enhancement_returns_per_level, multiplier=item.enhancement_level)
        self._accumulate(returns=returns, resources=rule.affix_returns_per_count, multiplier=len(item.affixes))
        if item.is_artifact:
            self._accumulate(returns=returns, resources=rule.artifact_bonus_returns, multiplier=1)
            self._accumulate(
                returns=returns,
                resources=rule.artifact_nurture_returns_per_level,
                multiplier=item.artifact_nurture_level,
            )
        scaled_returns = tuple(
            EquipmentResourceCost(
                resource_id=resource_id,
                quantity=self._scale_dismantle_quantity(quantity=quantity, multiplier=item.dismantle_reward_multiplier),
            )
            for resource_id, quantity in sorted(returns.items(), key=lambda entry: entry[0])
        )
        return EquipmentDismantleResult(item=item, returns=scaled_returns)

    def _require_rule(self, quality_id: str) -> DismantleQualityRuleDefinition:
        rule = self.static_config.equipment.get_dismantle_rule(quality_id)
        if rule is None:
            raise EquipmentRuleError(f"未找到装备分解规则：{quality_id}")
        return rule

    @staticmethod
    def _accumulate(
        *,
        returns: dict[str, int],
        resources: tuple[ResourceAmountDefinition, ...],
        multiplier: int,
    ) -> None:
        for resource in resources:
            returns[resource.resource_id] += resource.quantity * multiplier

    @staticmethod
    def _scale_dismantle_quantity(*, quantity: int, multiplier: Decimal) -> int:
        return int((Decimal(quantity) * multiplier).to_integral_value(rounding=ROUND_HALF_UP))


class EquipmentNamingService:
    """装备命名服务抽象。"""

    def assign_name(self, *, item: EquipmentItem) -> EquipmentNamingRecord:
        """根据装备内容返回命名结果。"""
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class TemplateEquipmentNamingService(EquipmentNamingService):
    """基于静态模板的装备命名实现。"""

    static_config: StaticGameConfig

    def assign_name(self, *, item: EquipmentItem) -> EquipmentNamingRecord:
        """按命名模板匹配装备名称。"""
        template = self._select_template(item=item)
        metadata = self._build_metadata(item=item, template=template)
        resolved_name = template.pattern
        for key, value in metadata.items():
            resolved_name = resolved_name.replace(f"{{{key}}}", value)
        return EquipmentNamingRecord(
            resolved_name=resolved_name,
            naming_template_id=template.template_id,
            naming_source=_TEMPLATE_NAMING_SOURCE,
            naming_metadata=metadata,
        )

    def _select_template(self, *, item: EquipmentItem) -> EquipmentNameTemplateDefinition:
        high_tier_threshold = self._require_high_tier_threshold()
        high_tier_count = sum(1 for affix in item.affixes if self._require_tier(affix.tier_id).order >= high_tier_threshold.order)
        for template in self.static_config.equipment.ordered_name_templates:
            if item.slot_id not in template.slot_ids:
                continue
            if item.quality_id not in template.quality_ids:
                continue
            if template.artifact_only and not item.is_artifact:
                continue
            if high_tier_count < template.minimum_high_tier_affix_count:
                continue
            return template
        raise EquipmentRuleError(f"当前装备缺少可用命名模板：{item.slot_id}/{item.quality_id}")

    def _build_metadata(
        self,
        *,
        item: EquipmentItem,
        template: EquipmentNameTemplateDefinition,
    ) -> dict[str, str]:
        primary_affix = self._select_primary_affix(item=item)
        metadata = {
            "quality_name": item.quality_name,
            "slot_name": item.slot_name,
            "template_name": item.template_name,
            "primary_affix_name": primary_affix.affix_name if primary_affix is not None else item.slot_name,
            "resonance_name": item.resonance_name or item.quality_name,
        }
        required_keys = {key for key in metadata if f"{{{key}}}" in template.pattern}
        return {key: metadata[key] for key in metadata if key in required_keys or not required_keys}

    def _select_primary_affix(self, *, item: EquipmentItem) -> EquipmentAffixValue | None:
        candidates = [affix for affix in item.affixes if affix.category in _ALLOWED_PRIMARY_AFFIX_CATEGORIES]
        if not candidates:
            return None
        return sorted(
            candidates,
            key=lambda affix: (
                self._require_tier(affix.tier_id).order,
                affix.value,
                affix.affix_id,
            ),
            reverse=True,
        )[0]

    def _require_high_tier_threshold(self) -> AffixTierDefinition:
        tier_id = self.static_config.equipment.naming.high_tier_affix_tier_id
        tier = self.static_config.equipment.get_affix_tier(tier_id)
        if tier is None:
            raise EquipmentRuleError(f"未找到命名高价值档位定义：{tier_id}")
        return tier

    def _require_tier(self, tier_id: str) -> AffixTierDefinition:
        tier = self.static_config.equipment.get_affix_tier(tier_id)
        if tier is None:
            raise EquipmentRuleError(f"未找到词条档位定义：{tier_id}")
        return tier


__all__ = [
    "ArtifactNurtureRule",
    "EquipmentAffixOperationRule",
    "EquipmentDismantleRule",
    "EquipmentEnhancementRule",
    "EquipmentGenerationRule",
    "EquipmentNamingService",
    "EquipmentRuleError",
    "TemplateEquipmentNamingService",
]
