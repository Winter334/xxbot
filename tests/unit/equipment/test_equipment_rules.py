"""阶段 6 装备领域规则测试。"""

from __future__ import annotations

from decimal import Decimal
from random import Random

import pytest

from domain.equipment import (
    ArtifactNurtureRule,
    EquipmentAffixOperationRule,
    EquipmentDismantleRule,
    EquipmentEnhancementRule,
    EquipmentGenerationRequest,
    EquipmentGenerationRule,
    EquipmentRuleError,
)
from infrastructure.config.static import load_static_config


@pytest.fixture(scope="module")
def static_config():
    """加载真实静态配置。"""
    return load_static_config()


@pytest.fixture(scope="module")
def generation_rule(static_config):
    """构造装备生成规则。"""
    return EquipmentGenerationRule(static_config)


@pytest.fixture(scope="module")
def enhancement_rule(static_config, generation_rule):
    """构造强化规则。"""
    return EquipmentEnhancementRule(static_config, generation_rule)


@pytest.fixture(scope="module")
def affix_operation_rule(static_config, generation_rule):
    """构造洗炼与重铸规则。"""
    return EquipmentAffixOperationRule(static_config, generation_rule)


@pytest.fixture(scope="module")
def artifact_nurture_rule(static_config):
    """构造法宝培养规则。"""
    return ArtifactNurtureRule(static_config)


@pytest.fixture(scope="module")
def dismantle_rule(static_config):
    """构造分解规则。"""
    return EquipmentDismantleRule(static_config)


def _resource_map(resources) -> dict[str, int]:
    """将资源条目转换为资源映射。"""
    return {entry.resource_id: entry.quantity for entry in resources}


def _base_attribute_total(item) -> int:
    """计算装备基础属性总值。"""
    return sum(attribute.value for attribute in item.base_attributes)


def _affix_signature(item) -> set[tuple[str, str, int]]:
    """提取装备词条签名集合。"""
    return {(affix.affix_id, affix.tier_id, affix.value) for affix in item.affixes}


def test_generation_four_slots_produce_valid_equipment(static_config, generation_rule) -> None:
    """四个部位应能生成基础属性与品质词条数量合法的装备。"""
    quality = static_config.equipment.get_quality("rare")
    assert quality is not None

    request_specs = {
        "weapon": ("iron_sword", 11),
        "armor": ("iron_armor", 12),
        "accessory": ("jade_ring", 13),
        "artifact": ("skyfire_mirror", 14),
    }

    for slot_id, (template_id, seed) in request_specs.items():
        item = generation_rule.generate_equipment(
            request=EquipmentGenerationRequest(
                slot_id=slot_id,
                quality_id="rare",
                rank_id="foundation",
                template_id=template_id,
            ),
            random_source=Random(seed),
        )

        assert item.slot_id == slot_id
        assert item.base_attributes
        assert len(item.affixes) == quality.base_affix_count
        assert item.rank_id == "foundation"
        assert item.rank_name == "三阶"


def test_generation_rank_affects_base_stats_and_affix_values(generation_rule) -> None:
    """同品质同底材在不同阶数下应体现基础属性与词条值提升。"""
    mortal_item = generation_rule.generate_equipment(
        request=EquipmentGenerationRequest(
            slot_id="weapon",
            quality_id="rare",
            rank_id="mortal",
            template_id="iron_sword",
        ),
        random_source=Random(7),
    )
    tribulation_item = generation_rule.generate_equipment(
        request=EquipmentGenerationRequest(
            slot_id="weapon",
            quality_id="rare",
            rank_id="tribulation",
            template_id="iron_sword",
        ),
        random_source=Random(7),
    )

    assert _base_attribute_total(tribulation_item) > _base_attribute_total(mortal_item)
    assert tribulation_item.affixes[0].value > mortal_item.affixes[0].value
    assert tribulation_item.base_attribute_multiplier == Decimal("48.24")
    assert tribulation_item.affix_base_value_multiplier == Decimal("48.24")


def test_generation_affix_tier_within_quality_ceiling(static_config, generation_rule) -> None:
    """词条档位不得突破品质上限，传说品质应存在命中最高档位的可行结果。"""
    tier_order_map = {tier.tier_id: tier.order for tier in static_config.equipment.affix_tiers}

    common_quality = static_config.equipment.get_quality("common")
    legendary_quality = static_config.equipment.get_quality("legendary")
    assert common_quality is not None
    assert legendary_quality is not None

    common_item = generation_rule.generate_equipment(
        request=EquipmentGenerationRequest(
            slot_id="weapon",
            quality_id="common",
            rank_id="mortal",
            template_id="iron_sword",
        ),
        random_source=Random(1),
    )
    common_ceiling = tier_order_map[common_quality.max_affix_tier_id]
    assert all(tier_order_map[affix.tier_id] <= common_ceiling for affix in common_item.affixes)

    legendary_max_tier_id = legendary_quality.max_affix_tier_id
    saw_highest_tier = False
    for seed in range(1, 257):
        legendary_item = generation_rule.generate_equipment(
            request=EquipmentGenerationRequest(
                slot_id="armor",
                quality_id="legendary",
                rank_id="mortal",
                template_id="iron_armor",
            ),
            random_source=Random(seed),
        )
        if any(affix.tier_id == legendary_max_tier_id for affix in legendary_item.affixes):
            saw_highest_tier = True
            break

    assert saw_highest_tier is True


def test_generation_same_tier_affixes_show_value_variance(generation_rule) -> None:
    """同档位同词条在多次生成中应出现档内浮动。"""
    multipliers = set()

    for seed in range(1, 21):
        item = generation_rule.generate_equipment(
            request=EquipmentGenerationRequest(
                slot_id="weapon",
                quality_id="common",
                rank_id="mortal",
                template_id="iron_sword",
            ),
            random_source=Random(seed),
        )
        assert len(item.affixes) == 1
        affix = item.affixes[0]
        assert affix.affix_id == "attack_power"
        assert affix.tier_id == "yellow"
        multipliers.add(affix.rolled_multiplier)

    assert len(multipliers) >= 2


def test_enhancement_success_consumes_and_levels_up(generation_rule, enhancement_rule) -> None:
    """强化成功时应消耗资源并提升一级。"""
    item = generation_rule.generate_equipment(
        request=EquipmentGenerationRequest(
            slot_id="weapon",
            quality_id="rare",
            rank_id="foundation",
            template_id="iron_sword",
        ),
        random_source=Random(21),
    )

    result = enhancement_rule.enhance(item=item, random_source=Random(1))
    cost_map = _resource_map(result.costs)

    assert result.success is True
    assert result.item.enhancement_level == item.enhancement_level + 1
    assert result.item.rank_id == item.rank_id
    assert cost_map["spirit_stone"] > 0
    assert cost_map["enhancement_stone"] > 0


def test_enhancement_failure_only_consumes_no_level_change(generation_rule, enhancement_rule) -> None:
    """强化失败时只消耗资源，不应改变强化等级。"""
    item = generation_rule.generate_equipment(
        request=EquipmentGenerationRequest(
            slot_id="weapon",
            quality_id="rare",
            rank_id="foundation",
            template_id="iron_sword",
        ),
        random_source=Random(22),
    )

    result = enhancement_rule.enhance(item=item, random_source=Random(2))
    cost_map = _resource_map(result.costs)

    assert result.success is False
    assert result.item.enhancement_level == item.enhancement_level
    assert result.item.rank_id == item.rank_id
    assert cost_map["spirit_stone"] > 0
    assert cost_map["enhancement_stone"] > 0


def test_wash_replaces_target_affix_preserves_rank_and_other_affixes(generation_rule, affix_operation_rule) -> None:
    """洗炼指定位置时应替换目标词条并保留阶数与其他词条。"""
    item = generation_rule.generate_equipment(
        request=EquipmentGenerationRequest(
            slot_id="artifact",
            quality_id="epic",
            rank_id="deity_transformation",
            template_id="skyfire_mirror",
        ),
        random_source=Random(5),
    )

    locked_affix_indices = (0, 2)
    original_target_affix = item.affixes[1]
    wash_result = None

    for seed in range(1, 257):
        candidate = affix_operation_rule.wash(
            item=item,
            locked_affix_indices=locked_affix_indices,
            random_source=Random(seed),
        )
        if candidate.item.affixes[1] != original_target_affix:
            wash_result = candidate
            break

    assert wash_result is not None
    assert wash_result.item.affixes[1] != original_target_affix
    assert wash_result.item.affixes[0] == item.affixes[0]
    assert wash_result.item.affixes[2] == item.affixes[2]
    assert wash_result.item.rank_id == item.rank_id
    assert wash_result.item.rank_name == item.rank_name


def test_reforge_rebuilds_affixes_preserves_identity_and_rank(
    static_config,
    generation_rule,
    enhancement_rule,
    affix_operation_rule,
) -> None:
    """重铸应重建词条，同时保留部位、品质、阶数与配置声明的强化等级策略。"""
    item = generation_rule.generate_equipment(
        request=EquipmentGenerationRequest(
            slot_id="weapon",
            quality_id="epic",
            rank_id="great_vehicle",
            template_id="iron_sword",
        ),
        random_source=Random(31),
    )

    for _ in range(3):
        enhancement_result = enhancement_rule.enhance(item=item, random_source=Random(1))
        assert enhancement_result.success is True
        item = enhancement_result.item

    reforge_result = None
    for seed in range(1, 257):
        candidate = affix_operation_rule.reforge(item=item, random_source=Random(seed))
        if _affix_signature(candidate.item) != _affix_signature(item):
            reforge_result = candidate
            break

    assert reforge_result is not None
    assert reforge_result.item.slot_id == item.slot_id
    assert reforge_result.item.quality_id == item.quality_id
    assert reforge_result.item.rank_id == item.rank_id
    assert reforge_result.item.rank_order == item.rank_order

    expected_enhancement_level = item.enhancement_level if static_config.equipment.reforge.preserve_enhancement_level else 0
    assert reforge_result.item.enhancement_level == expected_enhancement_level


def test_artifact_nurture_only_applies_to_artifact_slot(generation_rule, artifact_nurture_rule) -> None:
    """法宝培养应拒绝普通装备，并对法宝返回有效结果。"""
    normal_item = generation_rule.generate_equipment(
        request=EquipmentGenerationRequest(
            slot_id="weapon",
            quality_id="rare",
            rank_id="foundation",
            template_id="iron_sword",
        ),
        random_source=Random(41),
    )
    artifact_item = generation_rule.generate_equipment(
        request=EquipmentGenerationRequest(
            slot_id="artifact",
            quality_id="epic",
            rank_id="foundation",
            template_id="skyfire_mirror",
        ),
        random_source=Random(42),
    )

    with pytest.raises(EquipmentRuleError, match="只有法宝可以执行培养"):
        artifact_nurture_rule.nurture(item=normal_item)

    result = artifact_nurture_rule.nurture(item=artifact_item)
    cost_map = _resource_map(result.costs)

    assert result.item.is_artifact is True
    assert result.target_level == 1
    assert result.item.artifact_nurture_level == 1
    assert result.item.rank_id == artifact_item.rank_id
    assert cost_map["spirit_stone"] > 0
    assert cost_map["artifact_essence"] > 0


def test_dismantle_returns_scaled_by_rank_multiplier(generation_rule, dismantle_rule) -> None:
    """分解收益应应用阶数倍率。"""
    mortal_item = generation_rule.generate_equipment(
        request=EquipmentGenerationRequest(
            slot_id="armor",
            quality_id="common",
            rank_id="mortal",
            template_id="iron_armor",
        ),
        random_source=Random(51),
    )
    tribulation_item = generation_rule.generate_equipment(
        request=EquipmentGenerationRequest(
            slot_id="armor",
            quality_id="common",
            rank_id="tribulation",
            template_id="iron_armor",
        ),
        random_source=Random(51),
    )

    mortal_returns = _resource_map(dismantle_rule.dismantle(item=mortal_item).returns)
    tribulation_returns = _resource_map(dismantle_rule.dismantle(item=tribulation_item).returns)

    assert mortal_returns["spirit_sand"] == 4
    assert tribulation_returns["spirit_sand"] == 41
    assert tribulation_returns["wash_dust"] == 10


def test_equipment_rank_config_can_load(static_config) -> None:
    """装备阶数配置应能正确加载。"""
    ranks = static_config.equipment.ordered_equipment_ranks

    assert len(ranks) == 10
    assert ranks[0].rank_id == "mortal"
    assert ranks[-1].rank_id == "tribulation"
    assert ranks[-1].mapped_realm_id == "tribulation"
    assert ranks[-1].dismantle_reward_multiplier == Decimal("10.23")
