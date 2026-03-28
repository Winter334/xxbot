"""无尽副本领域规则测试。"""

from __future__ import annotations

from domain.dungeon import EndlessDungeonProgression, EndlessEncounterGenerator, EndlessNodeType
from infrastructure.config.static import load_static_config


def test_endless_progression_resolves_region_and_node_type_by_floor() -> None:
    """楼层应能稳定推导区域与节点类型。"""
    progression = EndlessDungeonProgression(load_static_config())

    floor_1 = progression.resolve_floor(1)
    floor_5 = progression.resolve_floor(5)
    floor_10 = progression.resolve_floor(10)
    floor_20 = progression.resolve_floor(20)
    floor_21 = progression.resolve_floor(21)
    floor_101 = progression.resolve_floor(101)

    assert floor_1.region.region_id == "wind"
    assert floor_1.node_type is EndlessNodeType.NORMAL
    assert floor_1.region.start_floor == 1
    assert floor_1.region.end_floor == 20

    assert floor_5.region.region_id == "wind"
    assert floor_5.node_type is EndlessNodeType.ELITE
    assert floor_5.is_elite_floor is True
    assert floor_5.is_anchor_floor is False

    assert floor_10.region.region_id == "wind"
    assert floor_10.node_type is EndlessNodeType.ANCHOR_BOSS
    assert floor_10.anchor_floor == 10
    assert floor_10.next_anchor_floor == 20

    assert floor_20.region.region_id == "wind"
    assert floor_20.node_type is EndlessNodeType.ANCHOR_BOSS
    assert floor_20.region.end_floor == 20

    assert floor_21.region.region_id == "flame"
    assert floor_21.region.start_floor == 21
    assert floor_21.region.end_floor == 40

    assert floor_101.region.region_id == "wind"
    assert floor_101.region.region_index == 6


def test_endless_progression_resolves_anchor_unlock_and_start_floors() -> None:
    """锚点与起点解锁应符合已通关锚点状态。"""
    progression = EndlessDungeonProgression(load_static_config())

    locked_floor = progression.resolve_floor(18, highest_unlocked_anchor_floor=0)
    unlocked_floor = progression.resolve_floor(18, highest_unlocked_anchor_floor=10)
    deep_unlocked_floor = progression.resolve_floor(37, highest_unlocked_anchor_floor=30)

    assert locked_floor.unlocked_as_start_floor is False
    assert locked_floor.start_floor == 1

    assert unlocked_floor.unlocked_as_start_floor is True
    assert unlocked_floor.anchor_floor == 10
    assert unlocked_floor.start_floor == 10

    assert deep_unlocked_floor.unlocked_as_start_floor is True
    assert deep_unlocked_floor.anchor_floor == 30
    assert deep_unlocked_floor.start_floor == 30

    assert progression.get_available_start_floors(0) == (1,)
    assert progression.get_available_start_floors(10) == (1, 10)
    assert progression.get_available_start_floors(30) == (1, 10, 20, 30)


def test_endless_encounter_generation_is_deterministic_for_same_seed_and_floor() -> None:
    """同一楼层与种子应生成完全相同的遭遇。"""
    generator = EndlessEncounterGenerator(load_static_config())

    encounter_a = generator.generate(floor=27, seed=20260326)
    encounter_b = generator.generate(floor=27, seed=20260326)
    encounter_c = generator.generate(floor=28, seed=20260326)

    assert encounter_a == encounter_b
    assert encounter_a.region_id == "flame"
    assert encounter_a.region_bias_id == "flame"
    assert encounter_a.node_type is EndlessNodeType.NORMAL
    assert encounter_a.enemy_count == 1

    assert encounter_c != encounter_a


def test_endless_encounter_generation_uses_node_type_enemy_count() -> None:
    """普通、精英、锚点节点应使用不同敌人数。"""
    generator = EndlessEncounterGenerator(load_static_config())

    normal_encounter = generator.generate(floor=4, seed=11)
    elite_encounter = generator.generate(floor=5, seed=11)
    boss_encounter = generator.generate(floor=10, seed=11)

    assert normal_encounter.node_type is EndlessNodeType.NORMAL
    assert normal_encounter.enemy_count == 1

    assert elite_encounter.node_type is EndlessNodeType.ELITE
    assert elite_encounter.enemy_count == 2

    assert boss_encounter.node_type is EndlessNodeType.ANCHOR_BOSS
    assert boss_encounter.enemy_count == 3


def test_endless_reward_rules_support_stable_pending_retreat_and_failure_results() -> None:
    """收益规则应区分稳定收益、未稳收益、撤离与战败。"""
    progression = EndlessDungeonProgression(load_static_config())

    elite_rewards = progression.build_reward_breakdown(25)
    anchor_rewards = progression.build_reward_breakdown(50)
    retreat_rewards = progression.settle_retreat_rewards(anchor_rewards)
    failure_rewards = progression.settle_failure_pending_rewards(anchor_rewards)

    assert elite_rewards.stable_cultivation == 160
    assert elite_rewards.stable_insight == 35
    assert elite_rewards.stable_refining_essence == 16
    assert elite_rewards.pending_equipment_score == 36
    assert elite_rewards.pending_artifact_score == 3
    assert elite_rewards.pending_dao_pattern_score == 8

    assert anchor_rewards.stable_cultivation == 260
    assert anchor_rewards.stable_insight == 58
    assert anchor_rewards.stable_refining_essence == 28
    assert anchor_rewards.pending_equipment_score == 92
    assert anchor_rewards.pending_artifact_score == 24
    assert anchor_rewards.pending_dao_pattern_score == 20

    assert retreat_rewards == anchor_rewards
    assert failure_rewards.stable_cultivation == anchor_rewards.stable_cultivation
    assert failure_rewards.stable_insight == anchor_rewards.stable_insight
    assert failure_rewards.pending_equipment_score == 46
    assert failure_rewards.pending_artifact_score == 12
    assert failure_rewards.pending_dao_pattern_score == 10
