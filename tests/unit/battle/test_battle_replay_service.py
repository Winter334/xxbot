"""共享战斗回放构建测试。"""

from __future__ import annotations

from collections.abc import Sequence

from application.battle import BattleReplayDisplayContext, BattleReplayService
from domain.battle import (
    BattleSide,
    BattleSnapshot,
    BattleTemplateParser,
    BattleTurnEngine,
    BattleUnitSnapshot,
    SeededBattleRandomSource,
)
from domain.battle.reporting import BattleReportBuilder
from infrastructure.config.static import load_static_config

STATIC_CONFIG = load_static_config()
TEMPLATE_PARSER = BattleTemplateParser(
    template_config=STATIC_CONFIG.battle_templates,
    skill_path_config=STATIC_CONFIG.skill_paths,
)



def _build_compiled_templates(*path_ids: str) -> dict[str, object]:
    return {
        path_id: TEMPLATE_PARSER.parse_template(path_id=path_id)
        for path_id in path_ids
    }



def _make_unit_snapshot(
    *,
    unit_id: str,
    unit_name: str,
    side: BattleSide,
    path_id: str,
    max_hp: int,
    current_hp: int,
    attack_power: int,
    guard_power: int,
    speed: int,
    max_resource: int = 100,
    current_resource: int = 100,
    crit_rate_permille: int = 0,
    crit_damage_bonus_permille: int = 0,
    hit_rate_permille: int = 1000,
    dodge_rate_permille: int = 0,
) -> BattleUnitSnapshot:
    return BattleUnitSnapshot(
        unit_id=unit_id,
        unit_name=unit_name,
        side=side,
        behavior_template_id=path_id,
        realm_id="foundation",
        stage_id="middle",
        max_hp=max_hp,
        current_hp=current_hp,
        current_shield=0,
        max_resource=max_resource,
        current_resource=current_resource,
        attack_power=attack_power,
        guard_power=guard_power,
        speed=speed,
        crit_rate_permille=crit_rate_permille,
        crit_damage_bonus_permille=crit_damage_bonus_permille,
        hit_rate_permille=hit_rate_permille,
        dodge_rate_permille=dodge_rate_permille,
        control_bonus_permille=0,
        control_resist_permille=0,
        healing_power_permille=0,
        shield_power_permille=0,
        damage_bonus_permille=0,
        damage_reduction_permille=0,
        counter_rate_permille=0,
    )



def _make_snapshot(
    *,
    seed: int,
    allies: Sequence[BattleUnitSnapshot],
    enemies: Sequence[BattleUnitSnapshot],
    round_limit: int,
) -> BattleSnapshot:
    return BattleSnapshot(
        seed=seed,
        allies=tuple(allies),
        enemies=tuple(enemies),
        round_limit=round_limit,
        environment_tags=("breakthrough", "replay_test"),
    )



def test_battle_replay_service_builds_game_style_frames_from_real_battle_report() -> None:
    compiled_templates = _build_compiled_templates("wenxin_sword", "manhuang_body")
    snapshot = _make_snapshot(
        seed=20260331,
        allies=(
            _make_unit_snapshot(
                unit_id="ally_blade",
                unit_name="青玄",
                side=BattleSide.ALLY,
                path_id="wenxin_sword",
                max_hp=1200,
                current_hp=1200,
                attack_power=190,
                guard_power=95,
                speed=120,
                crit_rate_permille=350,
                crit_damage_bonus_permille=700,
            ),
        ),
        enemies=(
            _make_unit_snapshot(
                unit_id="enemy_guard",
                unit_name="镇关石傀",
                side=BattleSide.ENEMY,
                path_id="manhuang_body",
                max_hp=920,
                current_hp=920,
                attack_power=110,
                guard_power=80,
                speed=60,
            ),
            _make_unit_snapshot(
                unit_id="enemy_shadow",
                unit_name="裂风残影",
                side=BattleSide.ENEMY,
                path_id="manhuang_body",
                max_hp=760,
                current_hp=760,
                attack_power=120,
                guard_power=70,
                speed=78,
            ),
        ),
        round_limit=6,
    )
    result = BattleTurnEngine().execute(
        snapshot=snapshot,
        behavior_templates=compiled_templates,
        random_source=SeededBattleRandomSource(seed=snapshot.seed),
    )
    report = BattleReportBuilder().build(
        snapshot=snapshot,
        result=result,
        behavior_templates=compiled_templates,
        template_config_version=STATIC_CONFIG.battle_templates.config_version,
        focus_unit_id="ally_blade",
        environment_snapshot={"trial_scene": "sword_pressure"},
    )

    presentation = BattleReplayService().build_presentation(
        battle_report_id=9001,
        result=report.summary.result,
        summary_payload=report.summary.to_payload(),
        detail_payload=report.detail.to_payload(),
        context=BattleReplayDisplayContext(
            source_name="突破秘境",
            scene_name="凝元试锋",
            group_name="根基试炼",
            environment_name="剑势压顶",
            focus_unit_name="青玄",
        ),
    )

    assert presentation is not None
    assert presentation.frames
    assert presentation.frames[0].title == "突破秘境｜凝元试锋"
    replay_text = "\n".join(
        line
        for frame in presentation.frames
        for line in frame.lines
    )
    assert "damage_resolved" not in replay_text
    assert "action_selected" not in replay_text
    assert "crit_check" not in replay_text
    assert "event_type" not in replay_text
    assert any(line.startswith("🌌") for line in presentation.frames[0].lines)
    assert any(
        keyword in replay_text
        for keyword in ("暴击", "气血", "溃散", "回稳", "后撤")
    )
    assert presentation.frames[-1].lines[-1].startswith(("🏁", "⏳"))
