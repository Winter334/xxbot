"""闭关服务集成测试。"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from alembic import command
from alembic.config import Config
import pytest

from application.character import (
    InvalidRetreatDurationError,
    RetreatAlreadyRunningError,
    RetreatNotReadyError,
    RetreatService,
)
from application.character.growth_service import CharacterGrowthService
from infrastructure.config.static import load_static_config
from infrastructure.db.repositories import (
    SqlAlchemyCharacterRepository,
    SqlAlchemyPlayerRepository,
    SqlAlchemyStateRepository,
)
from infrastructure.db.session import create_session_factory, session_scope

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _build_sqlite_url(database_path: Path) -> str:
    """构造测试用 SQLite 地址。"""
    return f"sqlite+pysqlite:///{database_path.as_posix()}"


def _upgrade_database(database_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """将测试数据库升级到最新迁移版本。"""
    monkeypatch.chdir(PROJECT_ROOT)
    monkeypatch.setenv("DATABASE_URL", database_url)
    command.upgrade(Config((PROJECT_ROOT / "alembic.ini").as_posix()), "head")


def _build_services(session, static_config):
    """创建测试所需的角色成长与闭关服务。"""
    player_repository = SqlAlchemyPlayerRepository(session)
    character_repository = SqlAlchemyCharacterRepository(session)
    growth_service = CharacterGrowthService(
        player_repository=player_repository,
        character_repository=character_repository,
        static_config=static_config,
    )
    retreat_service = RetreatService(
        state_repository=SqlAlchemyStateRepository(session),
        character_repository=character_repository,
        growth_service=growth_service,
        static_config=static_config,
    )
    return growth_service, retreat_service


def test_start_retreat_and_read_status(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """开始闭关后应写入状态，并能读取未完成状态。"""
    database_url = _build_sqlite_url(tmp_path / "retreat_start.db")
    _upgrade_database(database_url, monkeypatch)
    session_factory = create_session_factory(database_url)
    static_config = load_static_config()
    start_time = datetime(2026, 3, 26, 8, 0, 0)

    with session_scope(session_factory) as session:
        growth_service, retreat_service = _build_services(session, static_config)
        created = growth_service.create_character(
            discord_user_id="31001",
            player_display_name="星河",
            character_name="陆沉",
        )

        started = retreat_service.start_retreat(
            character_id=created.character_id,
            now=start_time,
            duration=timedelta(hours=12),
        )

        assert started.status == "running"
        assert started.realm_id == "mortal"
        assert started.started_at == start_time
        assert started.scheduled_end_at == start_time + timedelta(hours=12)
        assert started.settled_at is None
        assert started.can_settle is False
        assert started.pending_cultivation == 0
        assert started.pending_comprehension == 0
        assert started.pending_spirit_stone == 0

        status = retreat_service.get_retreat_status(
            character_id=created.character_id,
            now=start_time + timedelta(hours=6),
        )
        assert status is not None
        assert status.status == "running"
        assert status.can_settle is False
        assert status.pending_cultivation == 0

        with pytest.raises(RetreatAlreadyRunningError):
            retreat_service.start_retreat(
                character_id=created.character_id,
                now=start_time + timedelta(hours=1),
                duration=timedelta(hours=8),
            )


def test_settle_retreat_applies_rewards_and_marks_completed(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """闭关完成后应按配置结算修为、感悟与灵石，并避免重复结算。"""
    database_url = _build_sqlite_url(tmp_path / "retreat_settle.db")
    _upgrade_database(database_url, monkeypatch)
    session_factory = create_session_factory(database_url)
    static_config = load_static_config()
    start_time = datetime(2026, 3, 26, 9, 0, 0)
    settle_time = start_time + timedelta(days=1)

    with session_scope(session_factory) as session:
        growth_service, retreat_service = _build_services(session, static_config)
        created = growth_service.create_character(
            discord_user_id="31002",
            player_display_name="临渊",
            character_name="闻道",
        )

        retreat_service.start_retreat(character_id=created.character_id, now=start_time)
        result = retreat_service.settle_retreat(character_id=created.character_id, now=settle_time)

        assert result.character_id == created.character_id
        assert result.realm_id == "mortal"
        assert result.started_at == start_time
        assert result.scheduled_end_at == settle_time
        assert result.settled_at == settle_time
        assert result.reward.elapsed_seconds == 86400
        assert result.reward.cultivation_amount == 35
        assert result.reward.comprehension_amount == 2
        assert result.reward.spirit_stone_amount == 1
        assert result.applied_cultivation == 35
        assert result.growth_snapshot.cultivation_value == 35
        assert result.growth_snapshot.comprehension_value == 2
        assert result.growth_snapshot.spirit_stone == 1
        assert result.growth_snapshot.stage_id == "perfect"

        status = retreat_service.get_retreat_status(character_id=created.character_id, now=settle_time)
        assert status is not None
        assert status.status == "completed"
        assert status.can_settle is False
        assert status.settled_at == settle_time
        assert status.pending_cultivation == 0
        assert status.pending_comprehension == 0
        assert status.pending_spirit_stone == 0

        with pytest.raises(RetreatNotReadyError):
            retreat_service.settle_retreat(character_id=created.character_id, now=settle_time)


def test_settle_retreat_respects_max_claim_days(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """闭关收益结算应受配置中的最大结算天数限制。"""
    database_url = _build_sqlite_url(tmp_path / "retreat_cap.db")
    _upgrade_database(database_url, monkeypatch)
    session_factory = create_session_factory(database_url)
    static_config = load_static_config()
    start_time = datetime(2026, 3, 26, 10, 0, 0)
    settle_time = start_time + timedelta(days=5)

    with session_scope(session_factory) as session:
        growth_service, retreat_service = _build_services(session, static_config)
        created = growth_service.create_character(
            discord_user_id="31003",
            player_display_name="归舟",
            character_name="清越",
        )

        retreat_service.start_retreat(
            character_id=created.character_id,
            now=start_time,
            duration=timedelta(days=3),
        )
        result = retreat_service.settle_retreat(character_id=created.character_id, now=settle_time)

        assert result.reward.elapsed_seconds == 259200
        assert result.reward.cultivation_amount == 105
        assert result.reward.comprehension_amount == 8
        assert result.reward.spirit_stone_amount == 5
        assert result.applied_cultivation == 50
        assert result.growth_snapshot.cultivation_value == 50
        assert result.growth_snapshot.comprehension_value == 8
        assert result.growth_snapshot.spirit_stone == 5
        assert result.settled_at == start_time + timedelta(days=3)


def test_retreat_duration_validation_and_early_settlement_guard(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """闭关持续时间非法或未到结束时间时应拒绝操作。"""
    database_url = _build_sqlite_url(tmp_path / "retreat_validation.db")
    _upgrade_database(database_url, monkeypatch)
    session_factory = create_session_factory(database_url)
    static_config = load_static_config()
    start_time = datetime(2026, 3, 26, 11, 0, 0)

    with session_scope(session_factory) as session:
        growth_service, retreat_service = _build_services(session, static_config)
        created = growth_service.create_character(
            discord_user_id="31004",
            player_display_name="白石",
            character_name="昭明",
        )

        with pytest.raises(InvalidRetreatDurationError):
            retreat_service.start_retreat(
                character_id=created.character_id,
                now=start_time,
                duration=timedelta(0),
            )

        with pytest.raises(InvalidRetreatDurationError):
            retreat_service.start_retreat(
                character_id=created.character_id,
                now=start_time,
                duration=timedelta(days=4),
            )

        retreat_service.start_retreat(
            character_id=created.character_id,
            now=start_time,
            duration=timedelta(hours=10),
        )

        assert retreat_service.can_settle(
            character_id=created.character_id,
            now=start_time + timedelta(hours=9),
        ) is False

        with pytest.raises(RetreatNotReadyError):
            retreat_service.settle_retreat(
                character_id=created.character_id,
                now=start_time + timedelta(hours=9),
            )
