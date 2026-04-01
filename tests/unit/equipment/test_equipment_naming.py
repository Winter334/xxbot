"""阶段 6 装备模板命名测试。"""

from __future__ import annotations

from random import Random

import pytest

from application.equipment.panel_query_service import format_equipment_affix_display_line
from domain.equipment import EquipmentGenerationRequest, EquipmentGenerationRule, TemplateEquipmentNamingService
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
def naming_service(static_config):
    """构造模板命名服务。"""
    return TemplateEquipmentNamingService(static_config)


def test_template_naming_uses_quality_slot_and_affix_info(generation_rule, naming_service) -> None:
    """模板命名应生成非空名称并标记模板来源。"""
    item = generation_rule.generate_equipment(
        request=EquipmentGenerationRequest(
            slot_id="artifact",
            quality_id="epic",
            rank_id="foundation",
            template_id="skyfire_mirror",
        ),
        random_source=Random(5),
    )

    naming = naming_service.assign_name(item=item)

    assert naming.resolved_name
    assert naming.naming_source == "template_rule"


def test_generated_affix_display_uses_renamed_cultivation_style_name(static_config, generation_rule) -> None:
    """装备生成后的展示文案应输出修仙化后的词条名称。"""
    item = generation_rule.generate_equipment(
        request=EquipmentGenerationRequest(
            slot_id="weapon",
            quality_id="common",
            rank_id="foundation",
            template_id="iron_sword",
        ),
        random_source=Random(1),
    )

    affix = item.affixes[0]
    display_line = format_equipment_affix_display_line(affix, static_config=static_config)

    assert affix.affix_id == "attack_power"
    assert affix.affix_name == "锋芒"
    assert display_line.startswith("锋芒：")
    assert "攻力 +" in display_line


def test_template_naming_deterministic_for_same_input(generation_rule, naming_service) -> None:
    """同一输入执行两次模板命名应得到相同结果。"""
    item = generation_rule.generate_equipment(
        request=EquipmentGenerationRequest(
            slot_id="weapon",
            quality_id="legendary",
            rank_id="foundation",
            template_id="iron_sword",
        ),
        random_source=Random(11),
    )

    first_naming = naming_service.assign_name(item=item)
    second_naming = naming_service.assign_name(item=item)

    assert first_naming.resolved_name == second_naming.resolved_name
    assert first_naming.naming_template_id == second_naming.naming_template_id
    assert dict(first_naming.naming_metadata) == dict(second_naming.naming_metadata)


def test_template_naming_different_qualities_produce_different_names(generation_rule, naming_service) -> None:
    """不同品质的等价底材应生成不同名称。"""
    common_item = generation_rule.generate_equipment(
        request=EquipmentGenerationRequest(
            slot_id="weapon",
            quality_id="common",
            rank_id="foundation",
            template_id="iron_sword",
        ),
        random_source=Random(1),
    )
    legendary_item = generation_rule.generate_equipment(
        request=EquipmentGenerationRequest(
            slot_id="weapon",
            quality_id="legendary",
            rank_id="foundation",
            template_id="iron_sword",
        ),
        random_source=Random(11),
    )

    common_name = naming_service.assign_name(item=common_item)
    legendary_name = naming_service.assign_name(item=legendary_item)

    assert common_name.resolved_name != legendary_name.resolved_name
