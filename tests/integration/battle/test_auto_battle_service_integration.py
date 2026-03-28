"""阶段 4 自动战斗应用服务集成测试。"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path

from alembic import command
from alembic.config import Config
import pytest

from application.battle import AutoBattleRequest, AutoBattleService
from application.character import CharacterGrowthService
from application.character.skill_runtime_support import SkillRuntimeSupport
from domain.battle import BattleSide, BattleSnapshot, BattleUnitSnapshot
from infrastructure.config.static import load_static_config
from infrastructure.db.repositories import (
    SqlAlchemyBattleRecordRepository,
    SqlAlchemyCharacterRepository,
    SqlAlchemyPlayerRepository,
    SqlAlchemySkillRepository,
)
from infrastructure.db.session import create_session_factory, session_scope

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _build_sqlite_url(database_path: Path) -> str:
    """构造测试用 SQLite 地址。"""
    return f"sqlite+pysqlite:///{database_path.as_posix()}"


def _upgrade_database(database_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """把测试数据库升级到最新迁移版本。"""
    monkeypatch.chdir(PROJECT_ROOT)
    monkeypatch.setenv("DATABASE_URL", database_url)
    command.upgrade(Config((PROJECT_ROOT / "alembic.ini").as_posix()), "head")


def _make_unit_snapshot(
    *,
    unit_id: str,
    side: BattleSide,
    path_id: str,
    unit_name: str | None = None,
    max_hp: int = 100,
    current_hp: int | None = None,
    max_resource: int = 100,
    current_resource: int | None = None,
    attack_power: int = 60,
    guard_power: int = 20,
    speed: int = 30,
    counter_rate_permille: int = 0,
) -> BattleUnitSnapshot:
    """构造测试用战斗单位快照。"""
    return BattleUnitSnapshot(
        unit_id=unit_id,
        unit_name=unit_name or unit_id,
        side=side,
        behavior_template_id=path_id,
        realm_id="foundation",
        stage_id="middle",
        max_hp=max_hp,
        current_hp=max_hp if current_hp is None else current_hp,
        current_shield=0,
        max_resource=max_resource,
        current_resource=max_resource if current_resource is None else current_resource,
        attack_power=attack_power,
        guard_power=guard_power,
        speed=speed,
        crit_rate_permille=0,
        crit_damage_bonus_permille=0,
        hit_rate_permille=1000,
        dodge_rate_permille=0,
        control_bonus_permille=0,
        control_resist_permille=0,
        healing_power_permille=0,
        shield_power_permille=0,
        damage_bonus_permille=0,
        damage_reduction_permille=0,
        counter_rate_permille=counter_rate_permille,
    )


def _make_snapshot(
    *,
    seed: int,
    allies: Sequence[BattleUnitSnapshot],
    enemies: Sequence[BattleUnitSnapshot],
    round_limit: int = 3,
) -> BattleSnapshot:
    """构造自动战斗输入快照。"""
    return BattleSnapshot(
        seed=seed,
        allies=tuple(allies),
        enemies=tuple(enemies),
        round_limit=round_limit,
        environment_tags=("integration", "stage4"),
    )


def _normalize_domain_result(result) -> tuple[object, ...]:
    """把领域结果转换为稳定可比较结构。"""
    return (
        result.outcome.value,
        result.completed_rounds,
        tuple(
            (
                unit.unit_id,
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
            )
            for unit in result.final_units
        ),
        tuple(
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
            for event in result.events
        ),
        tuple(
            (
                call.sequence,
                call.purpose,
                call.minimum,
                call.maximum,
                call.result,
            )
            for call in result.random_calls
        ),
    )


def _build_services(session, static_config):
    """创建自动战斗集成测试所需服务与仓储。"""
    player_repository = SqlAlchemyPlayerRepository(session)
    character_repository = SqlAlchemyCharacterRepository(session)
    battle_record_repository = SqlAlchemyBattleRecordRepository(session)
    growth_service = CharacterGrowthService(
        player_repository=player_repository,
        character_repository=character_repository,
        static_config=static_config,
    )
    auto_battle_service = AutoBattleService(
        character_repository=character_repository,
        battle_record_repository=battle_record_repository,
        static_config=static_config,
    )
    return growth_service, auto_battle_service, character_repository, battle_record_repository


def test_auto_battle_service_returns_stable_structured_output_for_fixed_request(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """固定请求下，应用服务的结构化输出应完全稳定。"""
    database_url = _build_sqlite_url(tmp_path / "auto_battle_stable.db")
    _upgrade_database(database_url, monkeypatch)
    session_factory = create_session_factory(database_url)
    static_config = load_static_config()

    with session_scope(session_factory) as session:
        growth_service, auto_battle_service, _, _ = _build_services(session, static_config)
        created = growth_service.create_character(
            discord_user_id="41001",
            player_display_name="玄庭",
            character_name="洛书",
        )
        request = AutoBattleRequest(
            character_id=created.character_id,
            battle_type="endless",
            snapshot=_make_snapshot(
                seed=20260326,
                allies=(
                    _make_unit_snapshot(
                        unit_id="hero",
                        side=BattleSide.ALLY,
                        path_id="zhanqing_sword",
                        max_hp=180,
                        current_hp=180,
                        current_resource=100,
                        attack_power=72,
                        guard_power=22,
                        speed=36,
                    ),
                ),
                enemies=(
                    _make_unit_snapshot(
                        unit_id="enemy_front",
                        side=BattleSide.ENEMY,
                        path_id="manhuang_body",
                        max_hp=200,
                        current_hp=200,
                        current_resource=90,
                        attack_power=58,
                        guard_power=32,
                        speed=25,
                        counter_rate_permille=1000,
                    ),
                    _make_unit_snapshot(
                        unit_id="enemy_back",
                        side=BattleSide.ENEMY,
                        path_id="wangchuan_spell",
                        max_hp=150,
                        current_hp=150,
                        current_resource=100,
                        attack_power=64,
                        guard_power=18,
                        speed=27,
                    ),
                ),
                round_limit=3,
            ),
            opponent_ref="integration_guardian",
            environment_snapshot={
                "weather": "clear",
                "hazard_level": 0,
                "hard_mode": False,
            },
        )

        first_result = auto_battle_service.execute(request=request, persist=False)
        second_result = auto_battle_service.execute(request=request, persist=False)

        assert first_result.persisted_battle_report_id is None
        assert second_result.persisted_battle_report_id is None
        assert _normalize_domain_result(first_result.domain_result) == _normalize_domain_result(second_result.domain_result)
        assert first_result.report_artifacts.summary.to_payload() == second_result.report_artifacts.summary.to_payload()
        assert first_result.report_artifacts.detail.to_payload() == second_result.report_artifacts.detail.to_payload()
        assert first_result.report_artifacts.loss.to_payload() == second_result.report_artifacts.loss.to_payload()
        assert (
            first_result.persistence_mapping.progress_writeback.to_payload()
            == second_result.persistence_mapping.progress_writeback.to_payload()
        )


def test_auto_battle_service_persists_battle_report_and_progress_writeback(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """应用服务应把战报与战损回写写入现有阶段 2 持久化边界。"""
    database_url = _build_sqlite_url(tmp_path / "auto_battle_persist.db")
    _upgrade_database(database_url, monkeypatch)
    session_factory = create_session_factory(database_url)
    static_config = load_static_config()

    with session_scope(session_factory) as session:
        growth_service, auto_battle_service, character_repository, battle_record_repository = _build_services(
            session,
            static_config,
        )
        created = growth_service.create_character(
            discord_user_id="41002",
            player_display_name="青衡",
            character_name="照夜",
        )
        request = AutoBattleRequest(
            character_id=created.character_id,
            battle_type="endless",
            snapshot=_make_snapshot(
                seed=9001,
                allies=(
                    _make_unit_snapshot(
                        unit_id="hero",
                        side=BattleSide.ALLY,
                        path_id="wenxin_sword",
                        max_hp=100,
                        current_hp=72,
                        max_resource=100,
                        current_resource=100,
                        attack_power=78,
                        guard_power=18,
                        speed=40,
                    ),
                ),
                enemies=(
                    _make_unit_snapshot(
                        unit_id="enemy",
                        side=BattleSide.ENEMY,
                        path_id="manhuang_body",
                        max_hp=100,
                        current_hp=12,
                        max_resource=100,
                        current_resource=60,
                        attack_power=42,
                        guard_power=12,
                        speed=20,
                    ),
                ),
                round_limit=1,
            ),
            opponent_ref="persist_target",
        )

        execution_result = auto_battle_service.execute(request=request, persist=True)
        aggregate = character_repository.get_aggregate(created.character_id)
        reports = battle_record_repository.list_battle_reports(created.character_id)

        assert aggregate is not None
        assert aggregate.progress is not None
        assert execution_result.persisted_battle_report_id is not None
        assert len(reports) == 1
        persisted_report = reports[0]

        assert persisted_report.id == execution_result.persisted_battle_report_id
        assert persisted_report.character_id == created.character_id
        assert persisted_report.battle_type == "endless"
        assert persisted_report.result == execution_result.report_artifacts.summary.result
        assert persisted_report.opponent_ref == "persist_target"
        assert persisted_report.summary_json == execution_result.report_artifacts.summary.to_payload()
        assert persisted_report.detail_log_json == execution_result.report_artifacts.detail.to_payload()
        assert aggregate.progress.current_hp_ratio == Decimal(execution_result.report_artifacts.loss.final_hp_ratio)
        assert aggregate.progress.current_mp_ratio == Decimal(execution_result.report_artifacts.loss.final_mp_ratio)
        assert aggregate.progress.current_hp_ratio == Decimal("0.7200")
        assert aggregate.progress.current_mp_ratio < Decimal("1.0000")


def test_auto_battle_service_uses_runtime_template_mapping_and_auxiliary_patches(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """自动战斗应按运行期模板映射编译主修模板，并应用辅助补丁。"""
    database_url = _build_sqlite_url(tmp_path / "auto_battle_skill_runtime.db")
    _upgrade_database(database_url, monkeypatch)
    session_factory = create_session_factory(database_url)
    static_config = load_static_config()

    with session_scope(session_factory) as session:
        growth_service, auto_battle_service, _, _ = _build_services(session, static_config)
        created = growth_service.create_character(
            discord_user_id="41003",
            player_display_name="离衡",
            character_name="听雪",
        )
        skill_runtime_support = SkillRuntimeSupport(
            character_repository=SqlAlchemyCharacterRepository(session),
            skill_repository=SqlAlchemySkillRepository(session),
            static_config=static_config,
        )
        spirit_item = skill_runtime_support.generate_skill_item(
            character_id=created.character_id,
            lineage_id="sword_heart_lock",
            rank_id="mortal",
            quality_id="ordinary",
            source_type="integration_test",
            source_record_id="spirit_patch",
            seed=1,
        )

        request = AutoBattleRequest(
            character_id=created.character_id,
            battle_type="integration_skill_runtime",
            snapshot=_make_snapshot(
                seed=20260327,
                allies=(
                    _make_unit_snapshot(
                        unit_id="hero",
                        side=BattleSide.ALLY,
                        path_id="runtime:hero:1",
                        max_hp=180,
                        current_hp=180,
                        current_resource=100,
                        attack_power=72,
                        guard_power=22,
                        speed=36,
                    ),
                ),
                enemies=(
                    _make_unit_snapshot(
                        unit_id="enemy",
                        side=BattleSide.ENEMY,
                        path_id="manhuang_body",
                        max_hp=160,
                        current_hp=160,
                        current_resource=100,
                        attack_power=55,
                        guard_power=20,
                        speed=24,
                    ),
                ),
                round_limit=1,
            ),
            opponent_ref="runtime_template_target",
            template_patches_by_template_id={
                "runtime:hero:1": skill_runtime_support.build_template_patches(item=spirit_item),
            },
            template_path_id_by_template_id={
                "runtime:hero:1": "zhanqing_sword",
            },
        )

        result = auto_battle_service.execute(request=request, persist=False)
        compiled_template = result.compiled_templates["runtime:hero:1"]
        finish_action = next(action for action in compiled_template.actions if action.action_id == "zhanqing_finish_drive")

        assert compiled_template.path_id == "zhanqing_sword"
        assert "spirit_control_hit_bonus" not in compiled_template.applied_patch_ids
        assert "spirit_crit_bonus" not in compiled_template.applied_patch_ids
        assert finish_action.damage_scale_permille == 1720
