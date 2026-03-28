"""阶段 7 突破秘境仓储集成测试。"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, select

from domain.breakthrough import (
    BreakthroughRewardCycleType,
    BreakthroughRewardDirection,
    BreakthroughTrialProgressStatus,
)
from infrastructure.db.models import BreakthroughRewardLedger, Character, Player
from infrastructure.db.repositories import (
    SqlAlchemyBreakthroughRepository,
    SqlAlchemyBreakthroughRewardLedgerRepository,
    SqlAlchemyCharacterRepository,
    SqlAlchemyPlayerRepository,
)
from infrastructure.db.session import create_engine_from_url, create_session_factory, session_scope

PROJECT_ROOT = Path(__file__).resolve().parents[3]



def _build_sqlite_url(database_path: Path) -> str:
    """构造测试用 SQLite 地址。"""
    return f"sqlite+pysqlite:///{database_path.as_posix()}"



def _upgrade_database(database_url: str, monkeypatch) -> None:
    """将测试数据库升级到最新迁移版本。"""
    monkeypatch.chdir(PROJECT_ROOT)
    monkeypatch.setenv("DATABASE_URL", database_url)
    command.upgrade(Config((PROJECT_ROOT / "alembic.ini").as_posix()), "head")



def _create_character(session) -> int:
    """创建突破仓储测试所需的最小角色上下文。"""
    player_repo = SqlAlchemyPlayerRepository(session)
    character_repo = SqlAlchemyCharacterRepository(session)

    player = player_repo.add(Player(discord_user_id="70001", display_name="试炼者"))
    character = character_repo.add(
        Character(
            player_id=player.id,
            name="沈渊",
            title="破境人",
            total_power_score=0,
            public_power_score=0,
            hidden_pvp_score=0,
        )
    )
    return character.id



def test_stage7_migration_adds_breakthrough_progress_columns_and_reward_ledger_table(tmp_path, monkeypatch) -> None:
    """阶段 7 迁移应补齐突破进度扩展字段与方向级账本表。"""
    database_url = _build_sqlite_url(tmp_path / "stage7_migration.db")
    _upgrade_database(database_url, monkeypatch)

    engine = create_engine_from_url(database_url)
    try:
        inspector = inspect(engine)
        table_names = set(inspector.get_table_names())
        assert "breakthrough_reward_ledgers" in table_names

        progress_columns = {column["name"] for column in inspector.get_columns("breakthrough_trial_progress")}
        assert {
            "cleared_count",
            "first_cleared_at",
            "last_cleared_at",
            "qualification_granted_at",
            "last_reward_direction",
        }.issubset(progress_columns)

        progress_indexes = {index["name"] for index in inspector.get_indexes("breakthrough_trial_progress")}
        assert "ix_breakthrough_trial_progress_character_id_status" in progress_indexes

        ledger_columns = {column["name"] for column in inspector.get_columns("breakthrough_reward_ledgers")}
        assert {
            "character_id",
            "reward_direction",
            "cycle_type",
            "cycle_anchor_date",
            "high_yield_settlement_count",
            "last_settled_at",
        }.issubset(ledger_columns)

        ledger_unique_constraints = {
            constraint["name"]
            for constraint in inspector.get_unique_constraints("breakthrough_reward_ledgers")
        }
        assert "uq_breakthrough_reward_ledgers_character_direction_cycle" in ledger_unique_constraints
    finally:
        engine.dispose()



def test_breakthrough_repositories_round_trip_progress_and_direction_ledger(tmp_path, monkeypatch) -> None:
    """突破进度应支持读取与创建，方向级账本应按唯一键共享同一行。"""
    database_url = _build_sqlite_url(tmp_path / "stage7_repositories.db")
    _upgrade_database(database_url, monkeypatch)
    session_factory = create_session_factory(database_url)

    with session_scope(session_factory) as session:
        character_id = _create_character(session)
        breakthrough_repo = SqlAlchemyBreakthroughRepository(session)
        ledger_repo = SqlAlchemyBreakthroughRewardLedgerRepository(session)

        assert breakthrough_repo.get_progress(character_id, "foundation_to_core") is None

        created_progress = breakthrough_repo.get_or_create_progress(
            character_id,
            "foundation_to_core",
            group_id="entry_trials",
        )
        assert created_progress.status == BreakthroughTrialProgressStatus.FAILED.value
        assert created_progress.attempt_count == 0
        assert created_progress.cleared_count == 0
        assert created_progress.last_result_json == {}

        same_progress = breakthrough_repo.get_or_create_progress(
            character_id,
            "foundation_to_core",
            group_id="entry_trials",
        )
        assert same_progress.id == created_progress.id

        created_progress.status = BreakthroughTrialProgressStatus.CLEARED.value
        created_progress.attempt_count = 2
        created_progress.cleared_count = 1
        created_progress.last_reward_direction = "reforge_material"
        breakthrough_repo.save_progress(created_progress)

        second_progress = breakthrough_repo.get_or_create_progress(
            character_id,
            "qi_refining_to_foundation",
            group_id="entry_trials",
        )
        second_progress.status = BreakthroughTrialProgressStatus.CLEARED.value
        second_progress.attempt_count = 1
        second_progress.cleared_count = 1
        breakthrough_repo.save_progress(second_progress)

        loaded_progress = breakthrough_repo.get_progress(character_id, "foundation_to_core")
        assert loaded_progress is not None
        assert loaded_progress.id == created_progress.id
        assert loaded_progress.status == BreakthroughTrialProgressStatus.CLEARED.value
        assert loaded_progress.last_reward_direction == "reforge_material"

        all_progress = breakthrough_repo.list_by_character_id(character_id)
        assert [entry.mapping_id for entry in all_progress] == ["foundation_to_core", "qi_refining_to_foundation"]

        cleared_progress = breakthrough_repo.list_cleared_by_character_id(character_id)
        assert [entry.mapping_id for entry in cleared_progress] == ["foundation_to_core", "qi_refining_to_foundation"]

        cycle_anchor = date(2026, 3, 26)
        first_ledger = ledger_repo.get_or_create_ledger(
            character_id,
            BreakthroughRewardDirection.COMPREHENSION_MATERIAL,
            BreakthroughRewardCycleType.DAILY,
            cycle_anchor,
        )
        assert first_ledger.high_yield_settlement_count == 0

        first_ledger.high_yield_settlement_count = 3
        ledger_repo.save_ledger(first_ledger)

        same_direction_same_cycle_ledger = ledger_repo.get_or_create_ledger(
            character_id,
            BreakthroughRewardDirection.COMPREHENSION_MATERIAL,
            BreakthroughRewardCycleType.DAILY,
            cycle_anchor,
        )
        assert same_direction_same_cycle_ledger.id == first_ledger.id
        assert same_direction_same_cycle_ledger.high_yield_settlement_count == 3

        ledger_rows = session.scalars(
            select(BreakthroughRewardLedger).where(BreakthroughRewardLedger.character_id == character_id)
        ).all()
        assert len(ledger_rows) == 1
        assert ledger_rows[0].reward_direction == BreakthroughRewardDirection.COMPREHENSION_MATERIAL.value
