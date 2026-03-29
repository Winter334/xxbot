"""静态配置中心成功加载测试。"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from infrastructure.config.static import load_static_config

EXPECTED_SECTION_NAMES = {
    "realm_progression",
    "daily_cultivation",
    "base_coefficients",
    "cultivation_sources",
    "skill_paths",
    "skill_lineages",
    "skill_generation",
    "skill_drops",
    "battle_templates",
    "equipment",
    "enemies",
    "breakthrough_trials",
    "endless_dungeon",
    "pvp",
}
EXPECTED_STAGE_IDS = ("early", "middle", "late", "perfect")


def test_load_static_config_exposes_all_sections() -> None:
    """完整配置应暴露全部可读取 section。"""
    config = load_static_config()

    assert set(config.sections) == EXPECTED_SECTION_NAMES
    assert config.base_coefficients.scalar.base_hp == 100
    for section_name in EXPECTED_SECTION_NAMES:
        assert config.get_section(section_name) is config.sections[section_name]


def test_load_static_config_matches_launch_progression_and_cultivation_rules() -> None:
    """首发境界、日修为与修为来源应符合阶段 1 设计边界。"""
    config = load_static_config()

    ordered_realms = tuple(sorted(config.realm_progression.realms, key=lambda item: item.order))
    ordered_daily_entries = tuple(sorted(config.daily_cultivation.entries, key=lambda item: item.order))

    assert tuple(realm.realm_id for realm in ordered_realms)[-1] == "tribulation"
    assert len(ordered_realms) == 10
    assert all(tuple(realm.stage_ids) == EXPECTED_STAGE_IDS for realm in ordered_realms)

    assert tuple(entry.realm_id for entry in ordered_daily_entries) == tuple(realm.realm_id for realm in ordered_realms)
    assert len(ordered_daily_entries) == 10

    ratio_totals: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    for source in config.cultivation_sources.sources:
        ratio_totals[source.realm_id] += source.ratio

    assert set(ratio_totals) == {realm.realm_id for realm in ordered_realms}
    assert all(total_ratio == Decimal("1") for total_ratio in ratio_totals.values())


def test_load_static_config_matches_skill_equipment_enemy_breakthrough_and_endless_shapes() -> None:
    """功法、战斗模板、装备、敌人、突破与无尽配置应完整可读。"""
    config = load_static_config()

    assert len(config.skill_paths.axes) == 3
    assert len(config.skill_paths.paths) == 6
    assert config.skill_paths.get_path("wenxin_sword") is not None
    assert config.skill_paths.get_path("wenxin_sword").template_id == "wenxin_sword"

    assert len(config.skill_lineages.lineages) == 30
    assert config.skill_lineages.get_lineage("seven_kill_sword") is not None
    assert config.skill_lineages.get_lineage("seven_kill_sword").path_id == "wenxin_sword"
    assert config.skill_lineages.get_lineage("golden_bell_guard").auxiliary_slot_id == "guard"

    assert len(config.skill_generation.ranks) == 10
    assert config.skill_generation.get_rank("mortal") is not None
    assert config.skill_generation.get_rank("mortal").main_budget_min == 8
    assert config.skill_generation.get_rank("tribulation").auxiliary_budget_max == 31
    assert len(config.skill_generation.qualities) == 5
    assert config.skill_generation.get_quality("perfect") is not None
    assert config.skill_generation.get_quality("perfect").budget_bonus == 14
    assert len(config.skill_generation.attribute_pools) == 8
    assert config.skill_generation.get_attribute_pool("spirit_pool") is not None
    assert "control_hit_permille" in config.skill_generation.get_attribute_pool("spirit_pool").stat_ids
    assert len(config.skill_generation.patch_pools) == 10
    assert config.skill_generation.get_patch_pool("movement_patch_pool") is not None

    assert len(config.skill_drops.pools) == 4
    assert config.skill_drops.get_pool("launch_main_pool") is not None
    assert len(config.skill_drops.get_pool("launch_main_pool").entries) == 12
    assert config.skill_drops.default_probabilities.main_lineage_drop_rate == Decimal("0.35")
    assert config.skill_drops.default_probabilities.auxiliary_lineage_drop_rate == Decimal("0.65")
    assert config.skill_drops.default_probabilities.guard_slot_rate == Decimal("0.34")
    assert config.skill_drops.default_probabilities.movement_slot_rate == Decimal("0.33")
    assert config.skill_drops.default_probabilities.spirit_slot_rate == Decimal("0.33")
    assert config.skill_drops.default_probabilities.duplicate_drop_allowed is True

    assert len(config.battle_templates.templates) == 6
    assert config.battle_templates.get_template_by_path_id("wenxin_sword") is not None
    assert config.battle_templates.get_template_by_path_id("wenxin_sword").resource_policy == "burst"
    assert config.battle_templates.get_template_by_path_id("qingyun_spell").actions[1].action_type == "area_spell"
    assert config.battle_templates.get_template_by_path_id("wangchuan_spell").actions[0].control_chance_permille == 520

    assert len(config.equipment.slots) == 4
    assert len(config.equipment.qualities) == 4
    assert len(config.equipment.affix_tiers) == 4

    assert len(config.enemies.templates) == 5
    assert len(config.enemies.races) == 5
    assert len(config.enemies.region_biases) == 5
    assert config.enemies.races[0].favored_template_ids
    assert config.enemies.region_biases[0].template_weights[0].template_id == "swift"

    assert len(config.breakthrough_trials.trial_groups) == 3
    assert len(config.breakthrough_trials.trials) == 9

    first_trial = config.breakthrough_trials.trials[0]
    last_trial = config.breakthrough_trials.trials[-1]

    assert first_trial.from_realm_id == "mortal"
    assert first_trial.to_realm_id == "qi_refining"
    assert first_trial.required_comprehension_value == 10
    assert len(first_trial.required_items) == 1
    assert first_trial.required_items[0].item_type == "material"
    assert first_trial.required_items[0].item_id == "qi_condensation_grass"
    assert first_trial.required_items[0].quantity == 2

    assert last_trial.to_realm_id == "tribulation"
    assert last_trial.required_comprehension_value == 1800
    assert len(last_trial.required_items) == 2
    assert config.breakthrough_trials.get_trial_by_from_realm_id("great_vehicle") == last_trial
    assert config.breakthrough_trials.get_trial_by_from_realm_id("tribulation") is None

    assert config.endless_dungeon.structure.floors_per_region == 20
    assert config.endless_dungeon.structure.elite_interval == 5
    assert config.endless_dungeon.structure.anchor_interval == 10
    assert config.endless_dungeon.encounter.normal_enemy_count == 1
    assert config.endless_dungeon.encounter.elite_enemy_count == 2
    assert config.endless_dungeon.encounter.boss_enemy_count == 3
    assert tuple(region.region_id for region in config.endless_dungeon.ordered_regions) == (
        "wind",
        "flame",
        "frost",
        "shade",
        "thunder",
    )
    normal_reward = config.endless_dungeon.get_node_reward("normal")
    elite_reward = config.endless_dungeon.get_node_reward("elite")
    anchor_reward = config.endless_dungeon.get_node_reward("anchor_boss")

    assert normal_reward is not None
    assert elite_reward is not None
    assert anchor_reward is not None
    assert normal_reward.pending_drop_progress == 1
    assert elite_reward.pending_drop_progress == 12
    assert anchor_reward.pending_drop_progress == 10
    assert normal_reward.pending_drop_progress * 8 + elite_reward.pending_drop_progress == 20
