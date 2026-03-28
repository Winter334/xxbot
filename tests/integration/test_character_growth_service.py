"""角色成长服务集成测试。"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
import pytest

from application.character import CharacterAlreadyExistsError, CharacterGrowthService, InvalidGrowthAmountError
from infrastructure.config.static import load_static_config
from infrastructure.db.session import create_session_factory, session_scope
from infrastructure.db.repositories import SqlAlchemyCharacterRepository, SqlAlchemyPlayerRepository

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _build_sqlite_url(database_path: Path) -> str:
    """构造测试用 SQLite 地址。"""
    return f"sqlite+pysqlite:///{database_path.as_posix()}"


def _upgrade_database(database_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """将测试数据库升级到最新迁移版本。"""
    monkeypatch.chdir(PROJECT_ROOT)
    monkeypatch.setenv("DATABASE_URL", database_url)
    command.upgrade(Config((PROJECT_ROOT / "alembic.ini").as_posix()), "head")


def test_create_character_initializes_player_progress_and_currency(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """创建角色时应初始化玩家、角色、成长状态与货币余额。"""
    database_url = _build_sqlite_url(tmp_path / "character_growth_create.db")
    _upgrade_database(database_url, monkeypatch)
    session_factory = create_session_factory(database_url)
    static_config = load_static_config()

    with session_scope(session_factory) as session:
        service = CharacterGrowthService(
            player_repository=SqlAlchemyPlayerRepository(session),
            character_repository=SqlAlchemyCharacterRepository(session),
            static_config=static_config,
        )

        snapshot = service.create_character(
            discord_user_id="30001",
            player_display_name="流云",
            character_name="青玄",
            title="问道者",
        )

        assert snapshot.discord_user_id == "30001"
        assert snapshot.player_display_name == "流云"
        assert snapshot.character_name == "青玄"
        assert snapshot.character_title == "问道者"
        assert snapshot.realm_id == "mortal"
        assert snapshot.stage_id == "early"
        assert snapshot.cultivation_value == 0
        assert snapshot.comprehension_value == 0
        assert snapshot.spirit_stone == 0
        assert snapshot.honor_coin == 0
        assert snapshot.realm_total_cultivation == 50
        assert snapshot.current_stage_entry_cultivation == 0
        assert snapshot.next_stage_id == "middle"
        assert snapshot.next_stage_entry_cultivation == 7
        assert tuple(threshold.stage_id for threshold in snapshot.stage_thresholds) == (
            "early",
            "middle",
            "late",
            "perfect",
        )
        assert tuple(threshold.entry_cultivation for threshold in snapshot.stage_thresholds) == (0, 7, 17, 31)

        with pytest.raises(CharacterAlreadyExistsError):
            service.create_character(
                discord_user_id="30001",
                player_display_name="流云",
                character_name="重名角色",
            )


def test_add_cultivation_advances_stage_within_current_realm(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """修为增加后应按当前大境界门槛推进小阶段，但不处理大境界突破。"""
    database_url = _build_sqlite_url(tmp_path / "character_growth_cultivation.db")
    _upgrade_database(database_url, monkeypatch)
    session_factory = create_session_factory(database_url)
    static_config = load_static_config()

    with session_scope(session_factory) as session:
        service = CharacterGrowthService(
            player_repository=SqlAlchemyPlayerRepository(session),
            character_repository=SqlAlchemyCharacterRepository(session),
            static_config=static_config,
        )

        created = service.create_character(
            discord_user_id="30002",
            player_display_name="寒川",
            character_name="玄岳",
        )

        middle_result = service.add_cultivation(character_id=created.character_id, amount=7)
        assert middle_result.requested_amount == 7
        assert middle_result.applied_amount == 7
        assert middle_result.previous_stage_id == "early"
        assert middle_result.stage_changed is True
        assert middle_result.snapshot.stage_id == "middle"
        assert middle_result.snapshot.cultivation_value == 7
        assert middle_result.snapshot.next_stage_id == "late"
        assert middle_result.snapshot.next_stage_entry_cultivation == 17

        capped_result = service.add_cultivation(character_id=created.character_id, amount=100)
        assert capped_result.requested_amount == 100
        assert capped_result.applied_amount == 43
        assert capped_result.previous_stage_id == "middle"
        assert capped_result.stage_changed is True
        assert capped_result.snapshot.cultivation_value == 50
        assert capped_result.snapshot.stage_id == "perfect"
        assert capped_result.snapshot.next_stage_id is None
        assert capped_result.snapshot.next_stage_entry_cultivation is None
        assert capped_result.snapshot.realm_id == "mortal"

        with pytest.raises(InvalidGrowthAmountError):
            service.add_cultivation(character_id=created.character_id, amount=0)


def test_add_comprehension_only_updates_comprehension_value(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """感悟增加只更新感悟累计，不修改修为与小阶段。"""
    database_url = _build_sqlite_url(tmp_path / "character_growth_comprehension.db")
    _upgrade_database(database_url, monkeypatch)
    session_factory = create_session_factory(database_url)
    static_config = load_static_config()

    with session_scope(session_factory) as session:
        service = CharacterGrowthService(
            player_repository=SqlAlchemyPlayerRepository(session),
            character_repository=SqlAlchemyCharacterRepository(session),
            static_config=static_config,
        )

        created = service.create_character(
            discord_user_id="30003",
            player_display_name="青竹",
            character_name="明澈",
        )
        service.add_cultivation(character_id=created.character_id, amount=18)

        snapshot = service.add_comprehension(character_id=created.character_id, amount=12)
        assert snapshot.comprehension_value == 12
        assert snapshot.cultivation_value == 18
        assert snapshot.stage_id == "late"

        with pytest.raises(InvalidGrowthAmountError):
            service.add_comprehension(character_id=created.character_id, amount=-1)
