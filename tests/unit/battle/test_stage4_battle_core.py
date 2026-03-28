"""阶段 4 自动战斗核心测试。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal

import pytest

from application.battle.auto_battle_service import AutoBattleRequest, AutoBattleService
from domain.battle import (
    BattleEvent,
    BattleLossEvaluator,
    BattleOutcome,
    BattleRandomCall,
    BattleReactionType,
    BattleReportBuilder,
    BattleRuntimeContext,
    BattleSettlementEngine,
    BattleSide,
    BattleSnapshot,
    BattleStatusCategory,
    BattleStatusEffect,
    BattleTurnEngine,
    BattleUnitSnapshot,
    BattleUnitState,
    BattleTemplateParser,
    SeededBattleRandomSource,
)
from domain.battle.reporting import (
    BattleLossResult,
    BattleReportArtifacts,
    BattleReportDetail,
    BattleReportSummary,
)
from infrastructure.config.static import load_static_config
from infrastructure.db.models import CharacterProgress

STATIC_CONFIG = load_static_config()
TEMPLATE_PARSER = BattleTemplateParser(
    template_config=STATIC_CONFIG.battle_templates,
    skill_path_config=STATIC_CONFIG.skill_paths,
)


class ScriptedRandomSource:
    """按预设结果返回随机值的测试随机源。"""

    def __init__(self, scripted_results: Sequence[int]) -> None:
        self._scripted_results = list(scripted_results)
        self._calls: list[BattleRandomCall] = []
        self._sequence = 0

    def next_int(self, *, minimum: int, maximum: int, purpose: str) -> int:
        """返回预设随机值，并记录调用序列。"""
        if not self._scripted_results:
            raise AssertionError(f"随机结果不足，无法处理调用：{purpose}")
        result = self._scripted_results.pop(0)
        if result < minimum or result > maximum:
            raise AssertionError(
                f"随机结果 {result} 超出范围 [{minimum}, {maximum}]，调用用途：{purpose}"
            )
        self._sequence += 1
        call = BattleRandomCall(
            sequence=self._sequence,
            purpose=purpose,
            minimum=minimum,
            maximum=maximum,
            result=result,
        )
        self._calls.append(call)
        return result

    def export_calls(self) -> tuple[BattleRandomCall, ...]:
        """导出已消费的随机调用。"""
        return tuple(self._calls)


class _StubCharacterRepository:
    """仅用于映射测试的最小角色仓储桩。"""

    def get_aggregate(self, character_id: int):  # pragma: no cover - 当前测试不会调用
        raise AssertionError(f"当前测试不应读取角色聚合：{character_id}")

    def save_progress(self, progress: CharacterProgress) -> CharacterProgress:
        return progress


class _StubBattleRecordRepository:
    """仅用于映射测试的最小战报仓储桩。"""

    def add_battle_report(self, battle_report):  # pragma: no cover - 当前测试不会调用
        raise AssertionError("当前测试不应落库战报")


def _build_compiled_templates(*path_ids: str) -> dict[str, object]:
    """编译测试所需的运行期行为模板。"""
    return {
        path_id: TEMPLATE_PARSER.parse_template(path_id=path_id)
        for path_id in path_ids
    }


def _make_unit_snapshot(
    *,
    unit_id: str,
    side: BattleSide,
    path_id: str,
    unit_name: str | None = None,
    max_hp: int = 100,
    current_hp: int | None = None,
    current_shield: int = 0,
    max_resource: int = 100,
    current_resource: int | None = None,
    attack_power: int = 60,
    guard_power: int = 20,
    speed: int = 30,
    crit_rate_permille: int = 0,
    crit_damage_bonus_permille: int = 0,
    hit_rate_permille: int = 1000,
    dodge_rate_permille: int = 0,
    control_bonus_permille: int = 0,
    control_resist_permille: int = 0,
    healing_power_permille: int = 0,
    shield_power_permille: int = 0,
    damage_bonus_permille: int = 0,
    damage_reduction_permille: int = 0,
    counter_rate_permille: int = 0,
) -> BattleUnitSnapshot:
    """构造单个战斗单位快照。"""
    return BattleUnitSnapshot(
        unit_id=unit_id,
        unit_name=unit_name or unit_id,
        side=side,
        behavior_template_id=path_id,
        realm_id="foundation",
        stage_id="middle",
        max_hp=max_hp,
        current_hp=max_hp if current_hp is None else current_hp,
        current_shield=current_shield,
        max_resource=max_resource,
        current_resource=max_resource if current_resource is None else current_resource,
        attack_power=attack_power,
        guard_power=guard_power,
        speed=speed,
        crit_rate_permille=crit_rate_permille,
        crit_damage_bonus_permille=crit_damage_bonus_permille,
        hit_rate_permille=hit_rate_permille,
        dodge_rate_permille=dodge_rate_permille,
        control_bonus_permille=control_bonus_permille,
        control_resist_permille=control_resist_permille,
        healing_power_permille=healing_power_permille,
        shield_power_permille=shield_power_permille,
        damage_bonus_permille=damage_bonus_permille,
        damage_reduction_permille=damage_reduction_permille,
        counter_rate_permille=counter_rate_permille,
    )


def _make_snapshot(
    *,
    seed: int,
    allies: Sequence[BattleUnitSnapshot],
    enemies: Sequence[BattleUnitSnapshot],
    round_limit: int = 3,
    environment_tags: tuple[str, ...] = ("unit_test",),
) -> BattleSnapshot:
    """构造战斗快照。"""
    return BattleSnapshot(
        seed=seed,
        allies=tuple(allies),
        enemies=tuple(enemies),
        round_limit=round_limit,
        environment_tags=environment_tags,
    )


def _build_report(
    *,
    snapshot: BattleSnapshot,
    result,
    compiled_templates: Mapping[str, object],
    focus_unit_id: str,
    environment_snapshot: Mapping[str, str | int | bool | None] | None = None,
):
    """构造战报产物。"""
    return BattleReportBuilder().build(
        snapshot=snapshot,
        result=result,
        behavior_templates=compiled_templates,
        template_config_version=STATIC_CONFIG.battle_templates.config_version,
        focus_unit_id=focus_unit_id,
        environment_snapshot=environment_snapshot,
    )


def _build_runtime_context() -> tuple[BattleRuntimeContext, BattleUnitState]:
    """为状态合并测试构造运行时上下文。"""
    snapshot = _make_snapshot(
        seed=99,
        allies=(
            _make_unit_snapshot(
                unit_id="ally_controller",
                side=BattleSide.ALLY,
                path_id="wangchuan_spell",
            ),
        ),
        enemies=(
            _make_unit_snapshot(
                unit_id="enemy_target",
                side=BattleSide.ENEMY,
                path_id="manhuang_body",
            ),
        ),
        round_limit=1,
    )
    compiled_templates = _build_compiled_templates("wangchuan_spell", "manhuang_body")
    context = BattleRuntimeContext.from_snapshot(
        snapshot=snapshot,
        behavior_templates=compiled_templates,
        random_source=SeededBattleRandomSource(seed=snapshot.seed),
    )
    return context, context.get_unit("enemy_target")


def _make_final_unit_state(
    *,
    unit_id: str,
    path_id: str,
    side: BattleSide,
    current_hp: int,
    current_resource: int,
    current_shield: int = 0,
    max_hp: int = 100,
    max_resource: int = 100,
    base_shield: int = 0,
) -> BattleUnitState:
    """构造战损评估用终局单位状态。"""
    compiled_template = _build_compiled_templates(path_id)[path_id]
    snapshot = _make_unit_snapshot(
        unit_id=unit_id,
        unit_name=unit_id,
        side=side,
        path_id=path_id,
        max_hp=max_hp,
        current_hp=max_hp,
        current_shield=base_shield,
        max_resource=max_resource,
        current_resource=max_resource,
    )
    return BattleUnitState(
        base_snapshot=snapshot,
        behavior_template=compiled_template,
        stable_order=1 if side is BattleSide.ALLY else 2,
        side_id=0 if side is BattleSide.ALLY else 1,
        stable_first_strike_key=0 if "first_strike" in compiled_template.template_tags else 1,
        current_hp=current_hp,
        current_shield=current_shield,
        current_resource=current_resource,
    )


def _normalize_events(events: Sequence[BattleEvent]) -> tuple[tuple[object, ...], ...]:
    """把事件序列转换为可比较签名。"""
    return tuple(
        (
            event.sequence,
            event.round_index,
            event.phase.value,
            event.event_type,
            event.actor_unit_id,
            event.target_unit_id,
            event.action_id,
            event.detail_items,
        )
        for event in events
    )


def _normalize_random_calls(random_calls: Sequence[BattleRandomCall]) -> tuple[tuple[object, ...], ...]:
    """把随机调用转换为可比较签名。"""
    return tuple(
        (
            call.sequence,
            call.purpose,
            call.minimum,
            call.maximum,
            call.result,
        )
        for call in random_calls
    )


def _normalize_final_units(final_units: Sequence[BattleUnitState]) -> tuple[tuple[object, ...], ...]:
    """把终局单位状态转换为可比较签名。"""
    return tuple(
        (
            unit.unit_id,
            unit.side.value,
            unit.current_hp,
            unit.current_shield,
            unit.current_resource,
            tuple(
                (
                    status.status_id,
                    status.category.value,
                    status.source_unit_id,
                    status.source_action_id,
                    status.intensity_permille,
                    status.duration_rounds,
                    status.stack_count,
                    status.base_value,
                )
                for status in unit.ordered_statuses()
            ),
            tuple(sorted(unit.cooldowns.items())),
        )
        for unit in final_units
    )


def _make_report_artifacts(*, loss_result: BattleLossResult, result_text: str) -> BattleReportArtifacts:
    """构造映射测试所需的最小战报产物。"""
    summary = BattleReportSummary(
        schema_version="1.0.0",
        result=result_text,
        outcome=BattleOutcome.ALLY_VICTORY.value if result_text == "victory" else BattleOutcome.ENEMY_VICTORY.value,
        completed_rounds=2,
        main_path_id="wenxin_sword",
        main_paths=(
            {
                "unit_id": "hero",
                "unit_name": "hero",
                "template_id": "wenxin_sword",
                "path_id": "wenxin_sword",
                "axis_id": "sword",
                "template_name": "问心剑道",
                "template_tags": ("sword", "first_strike"),
                "applied_patch_ids": (),
                "actions_used": (),
            },
        ),
        key_trigger_counts={"successful_hits": 1},
        damage_summary={"ally_damage_dealt": 120, "enemy_damage_taken": 120},
        healing_summary={"ally_healing_done": 0},
        final_hp_ratio=loss_result.final_hp_ratio,
        final_mp_ratio=loss_result.final_mp_ratio,
        seed=7,
        template_config_version="1.0.0",
        snapshot_summary_hash="snapshot_hash",
        focus_unit_id="hero",
        focus_unit_name="hero",
    )
    detail = BattleReportDetail(
        schema_version="1.0.0",
        seed=7,
        template_config_version="1.0.0",
        snapshot_summary_hash="snapshot_hash",
        focus_unit_id="hero",
        environment_snapshot={"weather": "clear"},
        input_snapshot_summary={"seed": 7},
        rounds=(),
        terminal_statistics={"outcome": summary.outcome},
        random_calls=(),
        event_sequence=(),
    )
    return BattleReportArtifacts(summary=summary, detail=detail, loss=loss_result)


def test_turn_engine_and_report_builder_are_deterministic_for_fixed_snapshot_seed_and_environment() -> None:
    """固定快照、模板版本、环境与种子时，结构化结果应保持稳定。"""
    snapshot = _make_snapshot(
        seed=20260326,
        allies=(
            _make_unit_snapshot(
                unit_id="ally_blade",
                side=BattleSide.ALLY,
                path_id="zhanqing_sword",
                attack_power=72,
                speed=36,
                current_resource=100,
            ),
        ),
        enemies=(
            _make_unit_snapshot(
                unit_id="enemy_guard",
                side=BattleSide.ENEMY,
                path_id="manhuang_body",
                max_hp=180,
                current_hp=180,
                attack_power=58,
                guard_power=32,
                speed=26,
                current_resource=80,
            ),
        ),
        round_limit=3,
        environment_tags=("windless", "stable_seed"),
    )
    compiled_templates = _build_compiled_templates("zhanqing_sword", "manhuang_body")
    environment_snapshot = {
        "weather": "clear",
        "hazard_level": 0,
        "hard_mode": False,
    }
    engine = BattleTurnEngine()

    first_result = engine.execute(
        snapshot=snapshot,
        behavior_templates=compiled_templates,
        random_source=SeededBattleRandomSource(seed=snapshot.seed),
    )
    second_result = engine.execute(
        snapshot=snapshot,
        behavior_templates=compiled_templates,
        random_source=SeededBattleRandomSource(seed=snapshot.seed),
    )

    first_report = _build_report(
        snapshot=snapshot,
        result=first_result,
        compiled_templates=compiled_templates,
        focus_unit_id="ally_blade",
        environment_snapshot=environment_snapshot,
    )
    second_report = _build_report(
        snapshot=snapshot,
        result=second_result,
        compiled_templates=compiled_templates,
        focus_unit_id="ally_blade",
        environment_snapshot=environment_snapshot,
    )

    assert first_result.outcome is second_result.outcome
    assert first_result.completed_rounds == second_result.completed_rounds
    assert _normalize_events(first_result.events) == _normalize_events(second_result.events)
    assert _normalize_random_calls(first_result.random_calls) == _normalize_random_calls(second_result.random_calls)
    assert _normalize_final_units(first_result.final_units) == _normalize_final_units(second_result.final_units)
    assert first_report.summary.to_payload() == second_report.summary.to_payload()
    assert first_report.detail.to_payload() == second_report.detail.to_payload()
    assert first_report.loss.to_payload() == second_report.loss.to_payload()


@pytest.mark.parametrize(
    ("path_id", "expected_tag", "expected_action_type"),
    [
        ("wenxin_sword", "first_strike", "finisher"),
        ("zhanqing_sword", "pursuit", "combo_attack"),
        ("manhuang_body", "counter", "counter_attack"),
        ("changsheng_body", "heal", "heal_skill"),
        ("qingyun_spell", "clear_wave", "area_spell"),
        ("wangchuan_spell", "control", "control_spell"),
    ],
)
def test_report_surfaces_launch_path_differences_in_tags_and_action_categories(
    path_id: str,
    expected_tag: str,
    expected_action_type: str,
) -> None:
    """六条首发子方向的差异应能从战报模板标签与动作类别中直接看出。"""
    snapshot = _make_snapshot(
        seed=7000 + len(path_id),
        allies=(
            _make_unit_snapshot(
                unit_id="focus",
                side=BattleSide.ALLY,
                path_id=path_id,
                current_hp=86,
                current_resource=100,
                attack_power=68,
                speed=34,
            ),
        ),
        enemies=(
            _make_unit_snapshot(
                unit_id="enemy_front",
                side=BattleSide.ENEMY,
                path_id="manhuang_body",
                max_hp=160,
                guard_power=34,
                speed=28,
            ),
            _make_unit_snapshot(
                unit_id="enemy_back",
                side=BattleSide.ENEMY,
                path_id="wangchuan_spell",
                max_hp=140,
                attack_power=66,
                speed=27,
            ),
        ),
        round_limit=2,
    )
    compiled_templates = _build_compiled_templates(path_id, "manhuang_body", "wangchuan_spell")
    result = BattleTurnEngine().execute(
        snapshot=snapshot,
        behavior_templates=compiled_templates,
        random_source=SeededBattleRandomSource(seed=snapshot.seed),
    )
    report = _build_report(
        snapshot=snapshot,
        result=result,
        compiled_templates=compiled_templates,
        focus_unit_id="focus",
        environment_snapshot={"weather": "clear"},
    )

    main_path_payload = report.summary.to_payload()["main_paths"][0]
    behavior_template_payload = next(
        item
        for item in report.detail.to_payload()["terminal_statistics"]["behavior_templates"]
        if item["path_id"] == path_id
    )
    action_types = {action["action_type"] for action in behavior_template_payload["actions"]}

    assert expected_tag in main_path_payload["template_tags"]
    assert expected_action_type in action_types


@pytest.mark.parametrize(
    (
        "result_text",
        "focus_current_hp",
        "focus_current_resource",
        "focus_current_shield",
        "base_shield",
        "expected_injury_level",
        "expected_can_continue",
        "expected_loss_tags",
    ),
    [
        (
            "victory",
            20,
            40,
            0,
            18,
            "heavy",
            False,
            {"hp_critical", "shield_lost"},
        ),
        (
            "victory",
            60,
            5,
            0,
            0,
            "medium",
            False,
            {"mp_low"},
        ),
        (
            "defeat",
            0,
            30,
            0,
            0,
            "defeated",
            False,
            {"defeated", "hp_depleted"},
        ),
    ],
)
def test_loss_evaluator_and_progress_writeback_mapping_cover_stage4_boundaries(
    result_text: str,
    focus_current_hp: int,
    focus_current_resource: int,
    focus_current_shield: int,
    base_shield: int,
    expected_injury_level: str,
    expected_can_continue: bool,
    expected_loss_tags: set[str],
) -> None:
    """战损结果与进度回写映射应覆盖重伤、存活中伤与阵亡边界。"""
    focus_unit = _make_final_unit_state(
        unit_id="hero",
        path_id="wenxin_sword",
        side=BattleSide.ALLY,
        current_hp=focus_current_hp,
        current_resource=focus_current_resource,
        current_shield=focus_current_shield,
        base_shield=base_shield,
    )
    enemy_unit = _make_final_unit_state(
        unit_id="enemy",
        path_id="manhuang_body",
        side=BattleSide.ENEMY,
        current_hp=0 if result_text == "victory" else 55,
        current_resource=30,
        current_shield=0,
    )
    loss_result = BattleLossEvaluator().evaluate(
        final_units=(focus_unit, enemy_unit),
        focus_unit_id="hero",
    )

    assert loss_result.injury_level == expected_injury_level
    assert loss_result.can_continue is expected_can_continue
    assert expected_loss_tags.issubset(set(loss_result.loss_tags))
    assert loss_result.to_progress_update_payload() == {
        "current_hp_ratio": loss_result.final_hp_ratio,
        "current_mp_ratio": loss_result.final_mp_ratio,
    }

    service = AutoBattleService(
        character_repository=_StubCharacterRepository(),
        battle_record_repository=_StubBattleRecordRepository(),
        static_config=STATIC_CONFIG,
    )
    request = AutoBattleRequest(
        character_id=1,
        battle_type="endless",
        snapshot=_make_snapshot(
            seed=77,
            allies=(_make_unit_snapshot(unit_id="hero", side=BattleSide.ALLY, path_id="wenxin_sword"),),
            enemies=(_make_unit_snapshot(unit_id="enemy", side=BattleSide.ENEMY, path_id="manhuang_body"),),
            round_limit=1,
        ),
        opponent_ref="boundary_target",
    )
    artifacts = _make_report_artifacts(loss_result=loss_result, result_text=result_text)
    mapping = service._build_persistence_mapping(request=request, report_artifacts=artifacts)

    assert mapping.battle_report_payload.result == result_text
    assert mapping.progress_writeback.injury_level == expected_injury_level
    assert mapping.progress_writeback.can_continue is expected_can_continue
    assert set(mapping.progress_writeback.loss_tags) == set(loss_result.loss_tags)
    assert mapping.progress_writeback.to_payload() == {
        "character_id": 1,
        "current_hp_ratio": loss_result.final_hp_ratio,
        "current_mp_ratio": loss_result.final_mp_ratio,
        "injury_level": expected_injury_level,
        "can_continue": expected_can_continue,
        "loss_tags": list(loss_result.loss_tags),
    }

    progress = CharacterProgress(
        character_id=1,
        realm_id="foundation",
        stage_id="middle",
        cultivation_value=120,
        comprehension_value=30,
        breakthrough_qualification_obtained=False,
        highest_endless_floor=0,
        current_hp_ratio=Decimal("1.0000"),
        current_mp_ratio=Decimal("1.0000"),
    )
    updated_progress = mapping.progress_writeback.apply_to(progress)

    assert updated_progress.current_hp_ratio == Decimal(loss_result.final_hp_ratio)
    assert updated_progress.current_mp_ratio == Decimal(loss_result.final_mp_ratio)


def test_pursuit_is_resolved_before_counter_and_counter_only_triggers_once_per_round() -> None:
    """追击应先于反击结算，同一单位同回合最多反击一次，反击动作不会再触发反击。"""
    snapshot = _make_snapshot(
        seed=123,
        allies=(
            _make_unit_snapshot(
                unit_id="ally_blade",
                side=BattleSide.ALLY,
                path_id="zhanqing_sword",
                max_hp=180,
                attack_power=82,
                guard_power=24,
                current_resource=100,
                speed=35,
            ),
        ),
        enemies=(
            _make_unit_snapshot(
                unit_id="enemy_counter",
                side=BattleSide.ENEMY,
                path_id="manhuang_body",
                max_hp=620,
                attack_power=60,
                guard_power=28,
                current_resource=100,
                speed=24,
                counter_rate_permille=1000,
            ),
        ),
        round_limit=1,
    )
    compiled_templates = _build_compiled_templates("zhanqing_sword", "manhuang_body")
    result = BattleTurnEngine().execute(
        snapshot=snapshot,
        behavior_templates=compiled_templates,
        random_source=ScriptedRandomSource([400, 1]),
    )

    pursuit_events = [event for event in result.events if event.event_type == "pursuit_triggered"]
    counter_events = [event for event in result.events if event.event_type == "counter_triggered"]
    counter_checks = [event for event in result.events if event.event_type == "counter_check"]

    assert pursuit_events
    assert counter_events
    assert max(event.sequence for event in pursuit_events) < min(event.sequence for event in counter_events)
    assert len(counter_events) == 1
    assert len(counter_checks) == 1
    assert counter_checks[0].actor_unit_id == "enemy_counter"
    assert all(event.actor_unit_id != "ally_blade" for event in counter_events)


def test_hard_control_keeps_higher_intensity_or_longer_duration() -> None:
    """控制冲突时应保留更高强度者；强度相同则保留持续时间更长者。"""
    settlement_engine = BattleSettlementEngine()
    context, target = _build_runtime_context()

    existing = BattleStatusEffect(
        status_id="hard_control",
        status_name="硬控制",
        category=BattleStatusCategory.HARD_CONTROL,
        holder_unit_id=target.unit_id,
        source_unit_id="ally_controller",
        source_action_id="wangchuan_soul_bind",
        intensity_permille=420,
        duration_rounds=1,
        applied_round=1,
    )
    stronger = BattleStatusEffect(
        status_id="hard_control",
        status_name="硬控制",
        category=BattleStatusCategory.HARD_CONTROL,
        holder_unit_id=target.unit_id,
        source_unit_id="ally_controller",
        source_action_id="wangchuan_river_domain",
        intensity_permille=560,
        duration_rounds=1,
        applied_round=1,
    )
    target.statuses = [existing]

    settlement_engine._merge_hard_control(context=context, target=target, status=stronger)

    replaced_status = target.ordered_statuses()[0]
    assert replaced_status.intensity_permille == 560
    assert replaced_status.duration_rounds == 1

    equal_intensity_existing = BattleStatusEffect(
        status_id="hard_control",
        status_name="硬控制",
        category=BattleStatusCategory.HARD_CONTROL,
        holder_unit_id=target.unit_id,
        source_unit_id="ally_controller",
        source_action_id="wangchuan_soul_bind",
        intensity_permille=560,
        duration_rounds=1,
        applied_round=1,
    )
    longer = BattleStatusEffect(
        status_id="hard_control",
        status_name="硬控制",
        category=BattleStatusCategory.HARD_CONTROL,
        holder_unit_id=target.unit_id,
        source_unit_id="ally_controller",
        source_action_id="wangchuan_river_domain",
        intensity_permille=560,
        duration_rounds=2,
        applied_round=2,
    )
    target.statuses = [equal_intensity_existing]

    settlement_engine._merge_hard_control(context=context, target=target, status=longer)

    refreshed_status = target.ordered_statuses()[0]
    assert refreshed_status.intensity_permille == 560
    assert refreshed_status.duration_rounds == 2


def test_damage_over_time_stacks_and_attribute_suppression_keeps_highest_value() -> None:
    """持续伤害应叠层并刷新持续时间，属性压制应取更高强度并刷新持续时间。"""
    settlement_engine = BattleSettlementEngine()
    context, target = _build_runtime_context()

    existing_dot = BattleStatusEffect(
        status_id="damage_over_time",
        status_name="持续伤害",
        category=BattleStatusCategory.DAMAGE_OVER_TIME,
        holder_unit_id=target.unit_id,
        source_unit_id="ally_controller",
        source_action_id="wangchuan_river_domain",
        intensity_permille=220,
        duration_rounds=1,
        stack_count=2,
        max_stacks=5,
        base_value=14,
        applied_round=1,
    )
    incoming_dot = BattleStatusEffect(
        status_id="damage_over_time",
        status_name="持续伤害",
        category=BattleStatusCategory.DAMAGE_OVER_TIME,
        holder_unit_id=target.unit_id,
        source_unit_id="ally_controller",
        source_action_id="wangchuan_river_domain",
        intensity_permille=360,
        duration_rounds=2,
        stack_count=1,
        max_stacks=5,
        base_value=26,
        applied_round=2,
    )
    target.statuses = [existing_dot]

    settlement_engine._merge_damage_over_time(context=context, target=target, status=incoming_dot)

    stacked_dot = next(
        status for status in target.ordered_statuses() if status.category is BattleStatusCategory.DAMAGE_OVER_TIME
    )
    assert stacked_dot.intensity_permille == 360
    assert stacked_dot.duration_rounds == 2
    assert stacked_dot.stack_count == 3
    assert stacked_dot.base_value == 26

    existing_suppression = BattleStatusEffect(
        status_id="attribute_suppression",
        status_name="属性压制",
        category=BattleStatusCategory.ATTRIBUTE_SUPPRESSION,
        holder_unit_id=target.unit_id,
        source_unit_id="ally_controller",
        source_action_id="wangchuan_erosion",
        intensity_permille=260,
        duration_rounds=1,
        applied_round=1,
    )
    incoming_suppression = BattleStatusEffect(
        status_id="attribute_suppression",
        status_name="属性压制",
        category=BattleStatusCategory.ATTRIBUTE_SUPPRESSION,
        holder_unit_id=target.unit_id,
        source_unit_id="ally_controller",
        source_action_id="wangchuan_erosion",
        intensity_permille=440,
        duration_rounds=2,
        applied_round=2,
    )
    target.statuses = [existing_suppression]

    settlement_engine._merge_attribute_suppression(
        context=context,
        target=target,
        status=incoming_suppression,
    )

    refreshed_suppression = next(
        status
        for status in target.ordered_statuses()
        if status.category is BattleStatusCategory.ATTRIBUTE_SUPPRESSION
    )
    assert refreshed_suppression.intensity_permille == 440
    assert refreshed_suppression.duration_rounds == 2


def test_action_order_is_stable_and_independent_from_template_mapping_order() -> None:
    """行动顺序应由固定排序规则决定，而不是依赖模板字典遍历顺序。"""
    snapshot = _make_snapshot(
        seed=456,
        allies=(
            _make_unit_snapshot(
                unit_id="ally_first",
                side=BattleSide.ALLY,
                path_id="wenxin_sword",
                speed=30,
            ),
            _make_unit_snapshot(
                unit_id="ally_second",
                side=BattleSide.ALLY,
                path_id="zhanqing_sword",
                speed=30,
            ),
        ),
        enemies=(
            _make_unit_snapshot(
                unit_id="enemy_third",
                side=BattleSide.ENEMY,
                path_id="qingyun_spell",
                speed=30,
            ),
            _make_unit_snapshot(
                unit_id="enemy_fourth",
                side=BattleSide.ENEMY,
                path_id="changsheng_body",
                speed=30,
            ),
        ),
        round_limit=1,
    )
    compiled_templates = _build_compiled_templates(
        "wenxin_sword",
        "zhanqing_sword",
        "qingyun_spell",
        "changsheng_body",
    )
    reversed_templates = {
        key: compiled_templates[key]
        for key in reversed(tuple(compiled_templates))
    }
    engine = BattleTurnEngine()

    first_result = engine.execute(
        snapshot=snapshot,
        behavior_templates=compiled_templates,
        random_source=SeededBattleRandomSource(seed=snapshot.seed),
    )
    second_result = engine.execute(
        snapshot=snapshot,
        behavior_templates=reversed_templates,
        random_source=SeededBattleRandomSource(seed=snapshot.seed),
    )

    first_queue = tuple(
        event.actor_unit_id
        for event in first_result.events
        if event.event_type == "action_queue_entry"
    )
    second_queue = tuple(
        event.actor_unit_id
        for event in second_result.events
        if event.event_type == "action_queue_entry"
    )

    assert first_queue == ("ally_first", "ally_second", "enemy_third", "enemy_fourth")
    assert first_queue == second_queue
    assert _normalize_events(first_result.events) == _normalize_events(second_result.events)
    assert _normalize_random_calls(first_result.random_calls) == _normalize_random_calls(second_result.random_calls)
    assert _normalize_final_units(first_result.final_units) == _normalize_final_units(second_result.final_units)
