"""装备静态配置模型。"""

from __future__ import annotations

import re
from collections import Counter

from infrastructure.config.static.errors import StaticConfigIssueCollector
from infrastructure.config.static.models.common import (
    LAUNCH_REALM_IDS,
    NonNegativeInt,
    OrderedConfigItem,
    PercentageDecimal,
    PositiveDecimal,
    PositiveInt,
    ShortText,
    StableId,
    StaticConfigModel,
    VersionedSectionConfig,
)

LAUNCH_EQUIPMENT_SLOT_IDS: tuple[str, ...] = ("weapon", "armor", "accessory", "artifact")
NON_ARTIFACT_SLOT_IDS: tuple[str, ...] = ("weapon", "armor", "accessory")
LAUNCH_EQUIPMENT_RANK_IDS: tuple[str, ...] = LAUNCH_REALM_IDS
LAUNCH_EQUIPMENT_QUALITY_IDS: tuple[str, ...] = ("common", "rare", "epic", "earthly", "legendary", "immortal")
LAUNCH_AFFIX_TIER_IDS: tuple[str, ...] = ("yellow", "mystic", "earth", "heaven")
_ALLOWED_NAMING_PLACEHOLDERS: frozenset[str] = frozenset(
    {
        "quality_name",
        "slot_name",
        "template_name",
        "primary_affix_name",
        "resonance_name",
    }
)
_PLACEHOLDER_PATTERN = re.compile(r"\{([a-z_]+)\}")


class EquipmentStatValueDefinition(StaticConfigModel):
    """单条属性值定义。"""

    stat_id: StableId
    value: PositiveInt


class ResourceAmountDefinition(StaticConfigModel):
    """通用资源数量定义。"""

    resource_id: StableId
    quantity: PositiveInt


class EquipmentSlotDefinition(OrderedConfigItem):
    """装备部位定义。"""

    slot_id: StableId
    core_role: ShortText


class EquipmentRankDefinition(OrderedConfigItem):
    """装备与法宝统一阶数定义。"""

    rank_id: StableId
    mapped_realm_id: StableId
    base_attribute_multiplier: PositiveDecimal
    affix_base_value_multiplier: PositiveDecimal
    dismantle_reward_multiplier: PositiveDecimal


class EquipmentQualityDefinition(OrderedConfigItem):
    """装备品质定义。"""

    quality_id: StableId
    max_affix_tier_id: StableId
    base_affix_count: PositiveInt
    attribute_multiplier: PositiveDecimal
    max_enhancement_level: PositiveInt


class EquipmentBaseTemplateDefinition(OrderedConfigItem):
    """武器、护甲、饰品的底材模板。"""

    template_id: StableId
    slot_id: StableId
    generation_weight: PositiveInt
    preferred_affix_ids: tuple[StableId, ...]
    attributes: tuple[EquipmentStatValueDefinition, ...]
    summary: ShortText


class EnhancementLevelDefinition(StaticConfigModel):
    """单个强化等级的成功率、消耗与成长。"""

    target_level: PositiveInt
    success_rate: PercentageDecimal
    base_stat_bonus_ratio: PercentageDecimal
    affix_bonus_ratio: PercentageDecimal
    bonus_affix_unlock_count: NonNegativeInt
    costs: tuple[ResourceAmountDefinition, ...]


class EquipmentEnhancementConfig(StaticConfigModel):
    """装备强化规则配置。"""

    levels: tuple[EnhancementLevelDefinition, ...]


class AffixTierDefinition(OrderedConfigItem):
    """词条档位定义。"""

    tier_id: StableId
    min_multiplier: PositiveDecimal
    max_multiplier: PositiveDecimal


class EquipmentSpecialEffectDefinition(OrderedConfigItem):
    """特殊效果定义。"""

    effect_id: StableId
    effect_type: StableId
    trigger_event: StableId
    public_score_key: StableId | None = None
    hidden_pvp_score_key: StableId | None = None
    payload: dict[str, str | int | bool | None] = {}
    summary: ShortText
    detail_summary: ShortText


class EquipmentAffixDefinition(OrderedConfigItem):
    """词条条目定义。"""

    affix_id: StableId
    stat_id: StableId | None = None
    category: StableId
    slot_ids: tuple[StableId, ...]
    tier_ids: tuple[StableId, ...]
    selection_weight: PositiveInt
    base_value: NonNegativeInt = 0
    affix_kind: StableId = "numeric"
    special_effect_id: StableId | None = None
    is_pve_specialized: bool
    is_pvp_specialized: bool
    summary: ShortText


class SpecialAffixPoolDefinition(OrderedConfigItem):
    """特殊词条池定义。"""

    pool_id: StableId
    slot_ids: tuple[StableId, ...]
    quality_ids: tuple[StableId, ...]
    rank_ids: tuple[StableId, ...]
    affix_ids: tuple[StableId, ...]
    summary: ShortText


class EquipmentSpecialAffixGenerationConfig(StaticConfigModel):
    """特殊词条生成规则。"""

    minimum_quality_id: StableId = "epic"
    equipment_max_count: NonNegativeInt = 1
    artifact_max_count: NonNegativeInt = 2
    duplicate_effect_per_item_allowed: bool = False
    wash_can_add_special_affix: bool = False
    reforge_reroll_special_affixes: bool = True
    pools: tuple[SpecialAffixPoolDefinition, ...] = ()


class AffixTierWeightDefinition(StaticConfigModel):
    """单个词条档位的生成权重。"""

    tier_id: StableId
    weight: PositiveInt


class AffixGenerationConfig(StaticConfigModel):
    """词条生成规则。"""

    duplicate_affix_allowed: bool
    minimum_specialized_quality_id: StableId
    preferred_affix_weight_multiplier: PositiveDecimal
    artifact_preferred_affix_weight_multiplier: PositiveDecimal
    specialized_affix_weight_multiplier: PositiveDecimal
    tier_weights: tuple[AffixTierWeightDefinition, ...]


class EquipmentWashConfig(StaticConfigModel):
    """洗炼规则配置。"""

    max_locked_affix_count: NonNegativeInt
    base_costs: tuple[ResourceAmountDefinition, ...]
    lock_extra_costs: tuple[ResourceAmountDefinition, ...]


class EquipmentReforgeConfig(StaticConfigModel):
    """重铸规则配置。"""

    allow_template_change: bool
    preserve_enhancement_level: bool
    preserve_nurture_level: bool
    costs: tuple[ResourceAmountDefinition, ...]


class ArtifactTemplateDefinition(OrderedConfigItem):
    """法宝模板定义。"""

    template_id: StableId
    generation_weight: PositiveInt
    resonance_name: ShortText
    preferred_affix_ids: tuple[StableId, ...]
    attributes: tuple[EquipmentStatValueDefinition, ...]
    summary: ShortText


class ArtifactNurtureLevelDefinition(StaticConfigModel):
    """单级法宝培养成长规则。"""

    target_level: PositiveInt
    base_stat_bonus_ratio: PercentageDecimal
    affix_bonus_ratio: PercentageDecimal
    costs: tuple[ResourceAmountDefinition, ...]


class ArtifactNurtureConfig(StaticConfigModel):
    """法宝培养规则。"""

    levels: tuple[ArtifactNurtureLevelDefinition, ...]


class DismantleQualityRuleDefinition(StaticConfigModel):
    """单个品质的分解回收规则。"""

    quality_id: StableId
    base_returns: tuple[ResourceAmountDefinition, ...]
    enhancement_returns_per_level: tuple[ResourceAmountDefinition, ...]
    affix_returns_per_count: tuple[ResourceAmountDefinition, ...]
    artifact_bonus_returns: tuple[ResourceAmountDefinition, ...]
    artifact_nurture_returns_per_level: tuple[ResourceAmountDefinition, ...]


class EquipmentDismantleConfig(StaticConfigModel):
    """装备分解规则。"""

    rules: tuple[DismantleQualityRuleDefinition, ...]


class EquipmentNameTemplateDefinition(OrderedConfigItem):
    """模板命名规则。"""

    template_id: StableId
    slot_ids: tuple[StableId, ...]
    quality_ids: tuple[StableId, ...]
    minimum_high_tier_affix_count: NonNegativeInt
    artifact_only: bool
    pattern: ShortText
    summary: ShortText


class EquipmentNamingConfig(StaticConfigModel):
    """装备命名模板配置。"""

    high_tier_affix_tier_id: StableId
    templates: tuple[EquipmentNameTemplateDefinition, ...]


class EquipmentConfig(VersionedSectionConfig):
    """阶段 6 装备、词条、强化、法宝规则配置。"""

    slots: tuple[EquipmentSlotDefinition, ...]
    equipment_ranks: tuple[EquipmentRankDefinition, ...]
    qualities: tuple[EquipmentQualityDefinition, ...]
    base_templates: tuple[EquipmentBaseTemplateDefinition, ...]
    enhancement: EquipmentEnhancementConfig
    affix_tiers: tuple[AffixTierDefinition, ...]
    special_effects: tuple[EquipmentSpecialEffectDefinition, ...] = ()
    affixes: tuple[EquipmentAffixDefinition, ...]
    affix_generation: AffixGenerationConfig
    special_affix_generation: EquipmentSpecialAffixGenerationConfig
    wash: EquipmentWashConfig
    reforge: EquipmentReforgeConfig
    artifact_templates: tuple[ArtifactTemplateDefinition, ...]
    artifact_nurture: ArtifactNurtureConfig
    dismantle: EquipmentDismantleConfig
    naming: EquipmentNamingConfig

    @property
    def ordered_slots(self) -> tuple[EquipmentSlotDefinition, ...]:
        """按顺序返回装备部位。"""
        return tuple(sorted(self.slots, key=lambda item: item.order))

    @property
    def ordered_equipment_ranks(self) -> tuple[EquipmentRankDefinition, ...]:
        """按顺序返回装备阶数。"""
        return tuple(sorted(self.equipment_ranks, key=lambda item: item.order))

    @property
    def ordered_qualities(self) -> tuple[EquipmentQualityDefinition, ...]:
        """按顺序返回装备品质。"""
        return tuple(sorted(self.qualities, key=lambda item: item.order))

    @property
    def ordered_base_templates(self) -> tuple[EquipmentBaseTemplateDefinition, ...]:
        """按顺序返回全部普通装备底材模板。"""
        return tuple(sorted(self.base_templates, key=lambda item: item.order))

    @property
    def ordered_affix_tiers(self) -> tuple[AffixTierDefinition, ...]:
        """按顺序返回词条档位。"""
        return tuple(sorted(self.affix_tiers, key=lambda item: item.order))

    @property
    def ordered_affixes(self) -> tuple[EquipmentAffixDefinition, ...]:
        """按顺序返回词条池。"""
        return tuple(sorted(self.affixes, key=lambda item: item.order))

    @property
    def ordered_artifact_templates(self) -> tuple[ArtifactTemplateDefinition, ...]:
        """按顺序返回法宝模板。"""
        return tuple(sorted(self.artifact_templates, key=lambda item: item.order))

    @property
    def ordered_name_templates(self) -> tuple[EquipmentNameTemplateDefinition, ...]:
        """按顺序返回命名模板。"""
        return tuple(sorted(self.naming.templates, key=lambda item: item.order))

    @property
    def ordered_enhancement_levels(self) -> tuple[EnhancementLevelDefinition, ...]:
        """按强化等级顺序返回全部强化规则。"""
        return tuple(sorted(self.enhancement.levels, key=lambda item: item.target_level))

    @property
    def ordered_artifact_nurture_levels(self) -> tuple[ArtifactNurtureLevelDefinition, ...]:
        """按培养等级顺序返回法宝培养规则。"""
        return tuple(sorted(self.artifact_nurture.levels, key=lambda item: item.target_level))

    def get_slot(self, slot_id: str) -> EquipmentSlotDefinition | None:
        """读取指定部位定义。"""
        for slot in self.slots:
            if slot.slot_id == slot_id:
                return slot
        return None

    def get_equipment_rank(self, rank_id: str) -> EquipmentRankDefinition | None:
        """读取指定装备阶数定义。"""
        for rank in self.equipment_ranks:
            if rank.rank_id == rank_id:
                return rank
        return None

    def get_quality(self, quality_id: str) -> EquipmentQualityDefinition | None:
        """读取指定品质定义。"""
        for quality in self.qualities:
            if quality.quality_id == quality_id:
                return quality
        return None

    def get_base_template(self, template_id: str) -> EquipmentBaseTemplateDefinition | None:
        """读取指定普通装备底材模板。"""
        for template in self.base_templates:
            if template.template_id == template_id:
                return template
        return None

    def get_affix_tier(self, tier_id: str) -> AffixTierDefinition | None:
        """读取指定词条档位定义。"""
        for tier in self.affix_tiers:
            if tier.tier_id == tier_id:
                return tier
        return None

    def get_special_effect(self, effect_id: str) -> EquipmentSpecialEffectDefinition | None:
        """读取指定特殊效果定义。"""
        for effect in self.special_effects:
            if effect.effect_id == effect_id:
                return effect
        return None

    def get_affix(self, affix_id: str) -> EquipmentAffixDefinition | None:
        """读取指定词条定义。"""
        for affix in self.affixes:
            if affix.affix_id == affix_id:
                return affix
        return None

    def get_enhancement_level(self, target_level: int) -> EnhancementLevelDefinition | None:
        """读取指定强化等级规则。"""
        for level in self.enhancement.levels:
            if level.target_level == target_level:
                return level
        return None

    def get_artifact_template(self, template_id: str) -> ArtifactTemplateDefinition | None:
        """读取指定法宝模板。"""
        for template in self.artifact_templates:
            if template.template_id == template_id:
                return template
        return None

    def get_artifact_nurture_level(self, target_level: int) -> ArtifactNurtureLevelDefinition | None:
        """读取指定法宝培养等级规则。"""
        for level in self.artifact_nurture.levels:
            if level.target_level == target_level:
                return level
        return None

    def get_dismantle_rule(self, quality_id: str) -> DismantleQualityRuleDefinition | None:
        """读取指定品质的分解规则。"""
        for rule in self.dismantle.rules:
            if rule.quality_id == quality_id:
                return rule
        return None

    def collect_issues(self, *, filename: str, collector: StaticConfigIssueCollector) -> None:
        """收集当前配置节的结构与引用错误。"""
        self._collect_slots(filename=filename, collector=collector)
        self._collect_equipment_ranks(filename=filename, collector=collector)
        self._collect_qualities(filename=filename, collector=collector)
        self._collect_affix_tiers(filename=filename, collector=collector)
        self._collect_special_effects(filename=filename, collector=collector)
        self._collect_affixes(filename=filename, collector=collector)
        self._collect_base_templates(filename=filename, collector=collector)
        self._collect_enhancement(filename=filename, collector=collector)
        self._collect_affix_generation(filename=filename, collector=collector)
        self._collect_special_affix_generation(filename=filename, collector=collector)
        self._collect_wash(filename=filename, collector=collector)
        self._collect_reforge(filename=filename, collector=collector)
        self._collect_artifact_templates(filename=filename, collector=collector)
        self._collect_artifact_nurture(filename=filename, collector=collector)
        self._collect_dismantle(filename=filename, collector=collector)
        self._collect_naming(filename=filename, collector=collector)

    def _collect_slots(self, *, filename: str, collector: StaticConfigIssueCollector) -> None:
        ordered_slots = self.ordered_slots
        slot_ids = tuple(slot.slot_id for slot in ordered_slots)
        slot_order_counter = Counter(slot.order for slot in self.slots)
        slot_id_counter = Counter(slot.slot_id for slot in self.slots)

        if slot_ids != LAUNCH_EQUIPMENT_SLOT_IDS:
            collector.add(
                filename=filename,
                config_path="slots",
                identifier="slot_sequence",
                reason="装备部位必须固定为 weapon、armor、accessory、artifact",
            )

        for slot in self.slots:
            if slot_order_counter[slot.order] > 1:
                collector.add(
                    filename=filename,
                    config_path="slots[].order",
                    identifier=slot.slot_id,
                    reason=f"装备部位顺序值 {slot.order} 重复",
                )
            if slot_id_counter[slot.slot_id] > 1:
                collector.add(
                    filename=filename,
                    config_path="slots[].slot_id",
                    identifier=slot.slot_id,
                    reason="装备部位标识重复",
                )

    def _collect_equipment_ranks(self, *, filename: str, collector: StaticConfigIssueCollector) -> None:
        ordered_ranks = self.ordered_equipment_ranks
        rank_ids = tuple(rank.rank_id for rank in ordered_ranks)
        mapped_realm_ids = tuple(rank.mapped_realm_id for rank in ordered_ranks)
        rank_order_counter = Counter(rank.order for rank in self.equipment_ranks)
        rank_id_counter = Counter(rank.rank_id for rank in self.equipment_ranks)

        if rank_ids != LAUNCH_EQUIPMENT_RANK_IDS:
            collector.add(
                filename=filename,
                config_path="equipment_ranks",
                identifier="equipment_rank_sequence",
                reason="装备阶数必须固定为 mortal 至 tribulation 的首发十阶顺序",
            )

        if mapped_realm_ids != LAUNCH_REALM_IDS:
            collector.add(
                filename=filename,
                config_path="equipment_ranks",
                identifier="mapped_realm_sequence",
                reason="装备阶数映射的大境界顺序必须与首发大境界顺序完全一致",
            )

        previous_rank: EquipmentRankDefinition | None = None
        for rank in ordered_ranks:
            if rank_order_counter[rank.order] > 1:
                collector.add(
                    filename=filename,
                    config_path="equipment_ranks[].order",
                    identifier=rank.rank_id,
                    reason=f"装备阶数顺序值 {rank.order} 重复",
                )
            if rank_id_counter[rank.rank_id] > 1:
                collector.add(
                    filename=filename,
                    config_path="equipment_ranks[].rank_id",
                    identifier=rank.rank_id,
                    reason="装备阶数标识重复",
                )
            if previous_rank is not None:
                if rank.base_attribute_multiplier <= previous_rank.base_attribute_multiplier:
                    collector.add(
                        filename=filename,
                        config_path="equipment_ranks[].base_attribute_multiplier",
                        identifier=rank.rank_id,
                        reason="高阶装备基础属性倍率必须严格高于低阶",
                    )
                if rank.affix_base_value_multiplier <= previous_rank.affix_base_value_multiplier:
                    collector.add(
                        filename=filename,
                        config_path="equipment_ranks[].affix_base_value_multiplier",
                        identifier=rank.rank_id,
                        reason="高阶装备词条基础倍率必须严格高于低阶",
                    )
                if rank.dismantle_reward_multiplier <= previous_rank.dismantle_reward_multiplier:
                    collector.add(
                        filename=filename,
                        config_path="equipment_ranks[].dismantle_reward_multiplier",
                        identifier=rank.rank_id,
                        reason="高阶装备分解收益倍率必须严格高于低阶",
                    )
            previous_rank = rank

    def _collect_qualities(self, *, filename: str, collector: StaticConfigIssueCollector) -> None:
        ordered_qualities = self.ordered_qualities
        quality_ids = tuple(quality.quality_id for quality in ordered_qualities)
        quality_order_counter = Counter(quality.order for quality in self.qualities)
        quality_id_counter = Counter(quality.quality_id for quality in self.qualities)
        known_tier_ids = {tier.tier_id for tier in self.affix_tiers}

        if quality_ids != LAUNCH_EQUIPMENT_QUALITY_IDS:
            collector.add(
                filename=filename,
                config_path="qualities",
                identifier="quality_sequence",
                reason="装备品质必须固定为 common、rare、epic、earthly、legendary、immortal",
            )

        previous_quality: EquipmentQualityDefinition | None = None
        for quality in ordered_qualities:
            if quality_order_counter[quality.order] > 1:
                collector.add(
                    filename=filename,
                    config_path="qualities[].order",
                    identifier=quality.quality_id,
                    reason=f"装备品质顺序值 {quality.order} 重复",
                )
            if quality_id_counter[quality.quality_id] > 1:
                collector.add(
                    filename=filename,
                    config_path="qualities[].quality_id",
                    identifier=quality.quality_id,
                    reason="装备品质标识重复",
                )
            if quality.max_affix_tier_id not in known_tier_ids:
                collector.add(
                    filename=filename,
                    config_path="qualities[].max_affix_tier_id",
                    identifier=quality.quality_id,
                    reason=f"引用了未定义的词条档位 {quality.max_affix_tier_id}",
                )
            if previous_quality is not None:
                if quality.attribute_multiplier < previous_quality.attribute_multiplier:
                    collector.add(
                        filename=filename,
                        config_path="qualities[].attribute_multiplier",
                        identifier=quality.quality_id,
                        reason="高品质的基础属性倍率不能低于低品质",
                    )
                if quality.max_enhancement_level < previous_quality.max_enhancement_level:
                    collector.add(
                        filename=filename,
                        config_path="qualities[].max_enhancement_level",
                        identifier=quality.quality_id,
                        reason="高品质的强化上限不能低于低品质",
                    )
            previous_quality = quality

    def _collect_affix_tiers(self, *, filename: str, collector: StaticConfigIssueCollector) -> None:
        ordered_tiers = self.ordered_affix_tiers
        tier_ids = tuple(tier.tier_id for tier in ordered_tiers)
        tier_order_counter = Counter(tier.order for tier in self.affix_tiers)
        tier_id_counter = Counter(tier.tier_id for tier in self.affix_tiers)

        if tier_ids != LAUNCH_AFFIX_TIER_IDS:
            collector.add(
                filename=filename,
                config_path="affix_tiers",
                identifier="affix_tier_sequence",
                reason="词条档位必须固定为 yellow、mystic、earth、heaven",
            )

        previous_tier: AffixTierDefinition | None = None
        for tier in ordered_tiers:
            if tier_order_counter[tier.order] > 1:
                collector.add(
                    filename=filename,
                    config_path="affix_tiers[].order",
                    identifier=tier.tier_id,
                    reason=f"词条档位顺序值 {tier.order} 重复",
                )
            if tier_id_counter[tier.tier_id] > 1:
                collector.add(
                    filename=filename,
                    config_path="affix_tiers[].tier_id",
                    identifier=tier.tier_id,
                    reason="词条档位标识重复",
                )
            if tier.max_multiplier < tier.min_multiplier:
                collector.add(
                    filename=filename,
                    config_path="affix_tiers[].max_multiplier",
                    identifier=tier.tier_id,
                    reason="词条档位的最大浮动系数不能小于最小浮动系数",
                )
            if previous_tier is not None and tier.min_multiplier <= previous_tier.min_multiplier:
                collector.add(
                    filename=filename,
                    config_path="affix_tiers[].min_multiplier",
                    identifier=tier.tier_id,
                    reason="高档位词条的最小浮动系数必须严格递增",
                )
            previous_tier = tier

    def _collect_special_effects(self, *, filename: str, collector: StaticConfigIssueCollector) -> None:
        effect_id_counter = Counter(effect.effect_id for effect in self.special_effects)
        effect_order_counter = Counter(effect.order for effect in self.special_effects)
        for effect in self.special_effects:
            if effect_id_counter[effect.effect_id] > 1:
                collector.add(
                    filename=filename,
                    config_path="special_effects[].effect_id",
                    identifier=effect.effect_id,
                    reason="特殊效果标识重复",
                )
            if effect_order_counter[effect.order] > 1:
                collector.add(
                    filename=filename,
                    config_path="special_effects[].order",
                    identifier=effect.effect_id,
                    reason=f"特殊效果顺序值 {effect.order} 重复",
                )
            if not effect.detail_summary.strip():
                collector.add(
                    filename=filename,
                    config_path="special_effects[].detail_summary",
                    identifier=effect.effect_id,
                    reason="特殊效果必须提供可直接展示的完整效果描述",
                )

    def _collect_affixes(self, *, filename: str, collector: StaticConfigIssueCollector) -> None:
        known_slot_ids = {slot.slot_id for slot in self.slots}
        known_tier_ids = {tier.tier_id for tier in self.affix_tiers}
        known_effect_ids = {effect.effect_id for effect in self.special_effects}
        affix_id_counter = Counter(affix.affix_id for affix in self.affixes)
        affix_order_counter = Counter(affix.order for affix in self.affixes)

        for affix in self.affixes:
            if affix_id_counter[affix.affix_id] > 1:
                collector.add(
                    filename=filename,
                    config_path="affixes[].affix_id",
                    identifier=affix.affix_id,
                    reason="词条标识重复",
                )
            if affix_order_counter[affix.order] > 1:
                collector.add(
                    filename=filename,
                    config_path="affixes[].order",
                    identifier=affix.affix_id,
                    reason=f"词条顺序值 {affix.order} 重复",
                )
            for slot_id in affix.slot_ids:
                if slot_id not in known_slot_ids:
                    collector.add(
                        filename=filename,
                        config_path="affixes[].slot_ids",
                        identifier=affix.affix_id,
                        reason=f"词条池引用了未定义部位 {slot_id}",
                    )
            for tier_id in affix.tier_ids:
                if tier_id not in known_tier_ids:
                    collector.add(
                        filename=filename,
                        config_path="affixes[].tier_ids",
                        identifier=affix.affix_id,
                        reason=f"词条池引用了未定义档位 {tier_id}",
                    )
            if affix.affix_kind == "special_effect" and affix.special_effect_id is None:
                collector.add(
                    filename=filename,
                    config_path="affixes[].special_effect_id",
                    identifier=affix.affix_id,
                    reason="特殊词条必须绑定特殊效果定义",
                )
            if affix.affix_kind != "special_effect" and affix.special_effect_id is not None:
                collector.add(
                    filename=filename,
                    config_path="affixes[].special_effect_id",
                    identifier=affix.affix_id,
                    reason="数值词条不能绑定特殊效果定义",
                )
            if affix.special_effect_id is not None and affix.special_effect_id not in known_effect_ids:
                collector.add(
                    filename=filename,
                    config_path="affixes[].special_effect_id",
                    identifier=affix.affix_id,
                    reason=f"词条绑定了未定义特殊效果 {affix.special_effect_id}",
                )
            if affix.is_pve_specialized and affix.is_pvp_specialized:
                collector.add(
                    filename=filename,
                    config_path="affixes[].is_pvp_specialized",
                    identifier=affix.affix_id,
                    reason="首发专精词条不能同时声明为 PVE 与 PVP 专精",
                )

    def _collect_base_templates(self, *, filename: str, collector: StaticConfigIssueCollector) -> None:
        known_affix_ids = {affix.affix_id for affix in self.affixes}
        template_id_counter = Counter(template.template_id for template in self.base_templates)
        template_order_counter = Counter(template.order for template in self.base_templates)
        covered_slot_ids = {template.slot_id for template in self.base_templates}

        for expected_slot_id in NON_ARTIFACT_SLOT_IDS:
            if expected_slot_id not in covered_slot_ids:
                collector.add(
                    filename=filename,
                    config_path="base_templates",
                    identifier=expected_slot_id,
                    reason="武器、护甲、饰品三类普通装备都必须至少声明一个底材模板",
                )

        for template in self.base_templates:
            if template.slot_id not in NON_ARTIFACT_SLOT_IDS:
                collector.add(
                    filename=filename,
                    config_path="base_templates[].slot_id",
                    identifier=template.template_id,
                    reason="普通装备底材模板只能绑定 weapon、armor、accessory 三类部位",
                )
            if template_id_counter[template.template_id] > 1:
                collector.add(
                    filename=filename,
                    config_path="base_templates[].template_id",
                    identifier=template.template_id,
                    reason="装备底材模板标识重复",
                )
            if template_order_counter[template.order] > 1:
                collector.add(
                    filename=filename,
                    config_path="base_templates[].order",
                    identifier=template.template_id,
                    reason=f"装备底材模板顺序值 {template.order} 重复",
                )
            self._collect_duplicate_stats(
                filename=filename,
                collector=collector,
                config_path="base_templates[].attributes",
                identifier=template.template_id,
                attributes=template.attributes,
            )
            for affix_id in template.preferred_affix_ids:
                if affix_id not in known_affix_ids:
                    collector.add(
                        filename=filename,
                        config_path="base_templates[].preferred_affix_ids",
                        identifier=template.template_id,
                        reason=f"装备底材模板引用了未定义词条 {affix_id}",
                    )

    def _collect_enhancement(self, *, filename: str, collector: StaticConfigIssueCollector) -> None:
        ordered_levels = self.ordered_enhancement_levels
        level_targets = tuple(level.target_level for level in ordered_levels)
        max_level = max(quality.max_enhancement_level for quality in self.qualities)
        expected_targets = tuple(range(1, max_level + 1))

        if level_targets != expected_targets:
            collector.add(
                filename=filename,
                config_path="enhancement.levels",
                identifier="level_sequence",
                reason="强化等级定义必须从 1 开始连续覆盖至首发最高强化上限",
            )

        previous_level: EnhancementLevelDefinition | None = None
        for level in ordered_levels:
            self._collect_duplicate_resources(
                filename=filename,
                collector=collector,
                config_path="enhancement.levels[].costs",
                identifier=str(level.target_level),
                resources=level.costs,
            )
            if previous_level is not None and level.success_rate > previous_level.success_rate:
                collector.add(
                    filename=filename,
                    config_path="enhancement.levels[].success_rate",
                    identifier=str(level.target_level),
                    reason="强化等级越高，成功率不能高于前一等级",
                )
            previous_level = level

    def _collect_affix_generation(self, *, filename: str, collector: StaticConfigIssueCollector) -> None:
        tier_weight_ids = tuple(item.tier_id for item in self.affix_generation.tier_weights)
        known_quality_order = {quality.quality_id: quality.order for quality in self.qualities}
        known_tier_ids = {tier.tier_id for tier in self.affix_tiers}

        if tier_weight_ids != LAUNCH_AFFIX_TIER_IDS:
            collector.add(
                filename=filename,
                config_path="affix_generation.tier_weights",
                identifier="tier_weight_sequence",
                reason="词条档位权重必须完整覆盖 yellow、mystic、earth、heaven",
            )

        if self.affix_generation.minimum_specialized_quality_id not in known_quality_order:
            collector.add(
                filename=filename,
                config_path="affix_generation.minimum_specialized_quality_id",
                identifier="minimum_specialized_quality_id",
                reason=f"专精词条最低品质引用了未定义品质 {self.affix_generation.minimum_specialized_quality_id}",
            )
        elif known_quality_order[self.affix_generation.minimum_specialized_quality_id] < 3:
            collector.add(
                filename=filename,
                config_path="affix_generation.minimum_specialized_quality_id",
                identifier=self.affix_generation.minimum_specialized_quality_id,
                reason="专精词条最低品质必须至少达到玄品第三档以上",
            )

        tier_weight_counter = Counter(item.tier_id for item in self.affix_generation.tier_weights)
        for item in self.affix_generation.tier_weights:
            if item.tier_id not in known_tier_ids:
                collector.add(
                    filename=filename,
                    config_path="affix_generation.tier_weights[].tier_id",
                    identifier=item.tier_id,
                    reason=f"词条档位权重引用了未定义档位 {item.tier_id}",
                )
            if tier_weight_counter[item.tier_id] > 1:
                collector.add(
                    filename=filename,
                    config_path="affix_generation.tier_weights[].tier_id",
                    identifier=item.tier_id,
                    reason="词条档位权重重复声明",
                )

    def _collect_special_affix_generation(self, *, filename: str, collector: StaticConfigIssueCollector) -> None:
        known_slot_ids = {slot.slot_id for slot in self.slots}
        known_quality_ids = {quality.quality_id for quality in self.qualities}
        known_rank_ids = {rank.rank_id for rank in self.equipment_ranks}
        known_affix_ids = {affix.affix_id for affix in self.affixes if affix.affix_kind == "special_effect"}
        pool_id_counter = Counter(pool.pool_id for pool in self.special_affix_generation.pools)
        pool_order_counter = Counter(pool.order for pool in self.special_affix_generation.pools)

        if self.special_affix_generation.minimum_quality_id not in known_quality_ids:
            collector.add(
                filename=filename,
                config_path="special_affix_generation.minimum_quality_id",
                identifier=self.special_affix_generation.minimum_quality_id,
                reason="特殊词条最低开放品质引用了未定义品质",
            )

        for pool in self.special_affix_generation.pools:
            if pool_id_counter[pool.pool_id] > 1:
                collector.add(
                    filename=filename,
                    config_path="special_affix_generation.pools[].pool_id",
                    identifier=pool.pool_id,
                    reason="特殊词条池标识重复",
                )
            if pool_order_counter[pool.order] > 1:
                collector.add(
                    filename=filename,
                    config_path="special_affix_generation.pools[].order",
                    identifier=pool.pool_id,
                    reason=f"特殊词条池顺序值 {pool.order} 重复",
                )
            if not pool.rank_ids:
                collector.add(
                    filename=filename,
                    config_path="special_affix_generation.pools[].rank_ids",
                    identifier=pool.pool_id,
                    reason="特殊词条池必须显式声明开放阶数范围",
                )
            for slot_id in pool.slot_ids:
                if slot_id not in known_slot_ids:
                    collector.add(
                        filename=filename,
                        config_path="special_affix_generation.pools[].slot_ids",
                        identifier=pool.pool_id,
                        reason=f"特殊词条池引用了未定义部位 {slot_id}",
                    )
            for quality_id in pool.quality_ids:
                if quality_id not in known_quality_ids:
                    collector.add(
                        filename=filename,
                        config_path="special_affix_generation.pools[].quality_ids",
                        identifier=pool.pool_id,
                        reason=f"特殊词条池引用了未定义品质 {quality_id}",
                    )
            for rank_id in pool.rank_ids:
                if rank_id not in known_rank_ids:
                    collector.add(
                        filename=filename,
                        config_path="special_affix_generation.pools[].rank_ids",
                        identifier=pool.pool_id,
                        reason=f"特殊词条池引用了未定义阶数 {rank_id}",
                    )
            for affix_id in pool.affix_ids:
                if affix_id not in known_affix_ids:
                    collector.add(
                        filename=filename,
                        config_path="special_affix_generation.pools[].affix_ids",
                        identifier=pool.pool_id,
                        reason=f"特殊词条池引用了未定义或非特殊词条 {affix_id}",
                    )

    def _collect_wash(self, *, filename: str, collector: StaticConfigIssueCollector) -> None:
        max_affix_count = max(quality.base_affix_count for quality in self.qualities) + sum(
            level.bonus_affix_unlock_count for level in self.enhancement.levels
        )
        if self.wash.max_locked_affix_count >= max_affix_count:
            collector.add(
                filename=filename,
                config_path="wash.max_locked_affix_count",
                identifier="max_locked_affix_count",
                reason="洗炼锁定上限必须小于首发装备可能拥有的最大词条数量",
            )
        self._collect_duplicate_resources(
            filename=filename,
            collector=collector,
            config_path="wash.base_costs",
            identifier="base_costs",
            resources=self.wash.base_costs,
        )
        self._collect_duplicate_resources(
            filename=filename,
            collector=collector,
            config_path="wash.lock_extra_costs",
            identifier="lock_extra_costs",
            resources=self.wash.lock_extra_costs,
        )

    def _collect_reforge(self, *, filename: str, collector: StaticConfigIssueCollector) -> None:
        self._collect_duplicate_resources(
            filename=filename,
            collector=collector,
            config_path="reforge.costs",
            identifier="reforge_costs",
            resources=self.reforge.costs,
        )

    def _collect_artifact_templates(self, *, filename: str, collector: StaticConfigIssueCollector) -> None:
        known_affix_ids = {affix.affix_id for affix in self.affixes}
        template_id_counter = Counter(template.template_id for template in self.artifact_templates)
        template_order_counter = Counter(template.order for template in self.artifact_templates)

        if not self.artifact_templates:
            collector.add(
                filename=filename,
                config_path="artifact_templates",
                identifier="artifact_templates",
                reason="法宝模板至少需要声明一个条目",
            )

        for template in self.artifact_templates:
            if template_id_counter[template.template_id] > 1:
                collector.add(
                    filename=filename,
                    config_path="artifact_templates[].template_id",
                    identifier=template.template_id,
                    reason="法宝模板标识重复",
                )
            if template_order_counter[template.order] > 1:
                collector.add(
                    filename=filename,
                    config_path="artifact_templates[].order",
                    identifier=template.template_id,
                    reason=f"法宝模板顺序值 {template.order} 重复",
                )
            self._collect_duplicate_stats(
                filename=filename,
                collector=collector,
                config_path="artifact_templates[].attributes",
                identifier=template.template_id,
                attributes=template.attributes,
            )
            for affix_id in template.preferred_affix_ids:
                if affix_id not in known_affix_ids:
                    collector.add(
                        filename=filename,
                        config_path="artifact_templates[].preferred_affix_ids",
                        identifier=template.template_id,
                        reason=f"法宝模板引用了未定义词条 {affix_id}",
                    )

    def _collect_artifact_nurture(self, *, filename: str, collector: StaticConfigIssueCollector) -> None:
        ordered_levels = self.ordered_artifact_nurture_levels
        level_targets = tuple(level.target_level for level in ordered_levels)
        expected_targets = tuple(range(1, len(ordered_levels) + 1))
        if level_targets != expected_targets:
            collector.add(
                filename=filename,
                config_path="artifact_nurture.levels",
                identifier="level_sequence",
                reason="法宝培养等级定义必须从 1 开始连续声明",
            )

        for level in ordered_levels:
            self._collect_duplicate_resources(
                filename=filename,
                collector=collector,
                config_path="artifact_nurture.levels[].costs",
                identifier=str(level.target_level),
                resources=level.costs,
            )

    def _collect_dismantle(self, *, filename: str, collector: StaticConfigIssueCollector) -> None:
        rule_quality_ids = tuple(rule.quality_id for rule in self.dismantle.rules)
        quality_id_counter = Counter(rule.quality_id for rule in self.dismantle.rules)

        if rule_quality_ids != LAUNCH_EQUIPMENT_QUALITY_IDS:
            collector.add(
                filename=filename,
                config_path="dismantle.rules",
                identifier="dismantle_quality_sequence",
                reason="装备分解规则必须完整覆盖 common、rare、epic、earthly、legendary、immortal 六档品质",
            )

        for rule in self.dismantle.rules:
            if quality_id_counter[rule.quality_id] > 1:
                collector.add(
                    filename=filename,
                    config_path="dismantle.rules[].quality_id",
                    identifier=rule.quality_id,
                    reason="装备分解品质规则重复声明",
                )
            self._collect_duplicate_resources(
                filename=filename,
                collector=collector,
                config_path="dismantle.rules[].base_returns",
                identifier=rule.quality_id,
                resources=rule.base_returns,
            )
            self._collect_duplicate_resources(
                filename=filename,
                collector=collector,
                config_path="dismantle.rules[].enhancement_returns_per_level",
                identifier=rule.quality_id,
                resources=rule.enhancement_returns_per_level,
            )
            self._collect_duplicate_resources(
                filename=filename,
                collector=collector,
                config_path="dismantle.rules[].affix_returns_per_count",
                identifier=rule.quality_id,
                resources=rule.affix_returns_per_count,
            )
            self._collect_duplicate_resources(
                filename=filename,
                collector=collector,
                config_path="dismantle.rules[].artifact_bonus_returns",
                identifier=rule.quality_id,
                resources=rule.artifact_bonus_returns,
            )
            self._collect_duplicate_resources(
                filename=filename,
                collector=collector,
                config_path="dismantle.rules[].artifact_nurture_returns_per_level",
                identifier=rule.quality_id,
                resources=rule.artifact_nurture_returns_per_level,
            )

    def _collect_naming(self, *, filename: str, collector: StaticConfigIssueCollector) -> None:
        known_slot_ids = {slot.slot_id for slot in self.slots}
        known_quality_ids = {quality.quality_id for quality in self.qualities}
        known_tier_ids = {tier.tier_id for tier in self.affix_tiers}
        template_id_counter = Counter(template.template_id for template in self.naming.templates)
        template_order_counter = Counter(template.order for template in self.naming.templates)

        if self.naming.high_tier_affix_tier_id not in known_tier_ids:
            collector.add(
                filename=filename,
                config_path="naming.high_tier_affix_tier_id",
                identifier="high_tier_affix_tier_id",
                reason=f"命名配置引用了未定义的高价值词条档位 {self.naming.high_tier_affix_tier_id}",
            )

        for template in self.naming.templates:
            if template_id_counter[template.template_id] > 1:
                collector.add(
                    filename=filename,
                    config_path="naming.templates[].template_id",
                    identifier=template.template_id,
                    reason="命名模板标识重复",
                )
            if template_order_counter[template.order] > 1:
                collector.add(
                    filename=filename,
                    config_path="naming.templates[].order",
                    identifier=template.template_id,
                    reason=f"命名模板顺序值 {template.order} 重复",
                )
            for slot_id in template.slot_ids:
                if slot_id not in known_slot_ids:
                    collector.add(
                        filename=filename,
                        config_path="naming.templates[].slot_ids",
                        identifier=template.template_id,
                        reason=f"命名模板引用了未定义部位 {slot_id}",
                    )
            for quality_id in template.quality_ids:
                if quality_id not in known_quality_ids:
                    collector.add(
                        filename=filename,
                        config_path="naming.templates[].quality_ids",
                        identifier=template.template_id,
                        reason=f"命名模板引用了未定义品质 {quality_id}",
                    )
            self._collect_unknown_placeholders(
                filename=filename,
                collector=collector,
                identifier=template.template_id,
                pattern=template.pattern,
            )

    @staticmethod
    def _collect_duplicate_stats(
        *,
        filename: str,
        collector: StaticConfigIssueCollector,
        config_path: str,
        identifier: str,
        attributes: tuple[EquipmentStatValueDefinition, ...],
    ) -> None:
        stat_counter = Counter(attribute.stat_id for attribute in attributes)
        for stat_id, count in stat_counter.items():
            if count > 1:
                collector.add(
                    filename=filename,
                    config_path=config_path,
                    identifier=identifier,
                    reason=f"属性 {stat_id} 在同一模板中重复声明",
                )

    @staticmethod
    def _collect_duplicate_resources(
        *,
        filename: str,
        collector: StaticConfigIssueCollector,
        config_path: str,
        identifier: str,
        resources: tuple[ResourceAmountDefinition, ...],
    ) -> None:
        resource_counter = Counter(resource.resource_id for resource in resources)
        for resource_id, count in resource_counter.items():
            if count > 1:
                collector.add(
                    filename=filename,
                    config_path=config_path,
                    identifier=identifier,
                    reason=f"资源 {resource_id} 重复声明",
                )

    @staticmethod
    def _collect_unknown_placeholders(
        *,
        filename: str,
        collector: StaticConfigIssueCollector,
        identifier: str,
        pattern: str,
    ) -> None:
        placeholders = set(_PLACEHOLDER_PATTERN.findall(pattern))
        unknown = sorted(placeholders - _ALLOWED_NAMING_PLACEHOLDERS)
        if unknown:
            collector.add(
                filename=filename,
                config_path="naming.templates[].pattern",
                identifier=identifier,
                reason=f"命名模板包含未支持的占位符 {', '.join(unknown)}",
            )


__all__ = [
    "AffixGenerationConfig",
    "AffixTierDefinition",
    "AffixTierWeightDefinition",
    "ArtifactNurtureConfig",
    "ArtifactNurtureLevelDefinition",
    "ArtifactTemplateDefinition",
    "DismantleQualityRuleDefinition",
    "EnhancementLevelDefinition",
    "EquipmentAffixDefinition",
    "EquipmentBaseTemplateDefinition",
    "EquipmentConfig",
    "EquipmentDismantleConfig",
    "EquipmentEnhancementConfig",
    "EquipmentNameTemplateDefinition",
    "EquipmentNamingConfig",
    "EquipmentQualityDefinition",
    "EquipmentRankDefinition",
    "EquipmentReforgeConfig",
    "EquipmentSlotDefinition",
    "EquipmentStatValueDefinition",
    "EquipmentWashConfig",
    "LAUNCH_AFFIX_TIER_IDS",
    "LAUNCH_EQUIPMENT_QUALITY_IDS",
    "LAUNCH_EQUIPMENT_RANK_IDS",
    "LAUNCH_EQUIPMENT_SLOT_IDS",
    "NON_ARTIFACT_SLOT_IDS",
    "ResourceAmountDefinition",
]
