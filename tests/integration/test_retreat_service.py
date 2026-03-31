"""闭关服务集成测试。"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
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
    """开始闭关后应允许主动结束，并按 30 分钟门槛与 12 小时进度展示收益。"""
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
        assert started.can_settle is True
        assert started.reward_available is False
        assert started.elapsed_seconds == 0
        assert started.settlement_seconds == 0
        assert started.minimum_reward_seconds == 30 * 60
        assert started.full_yield_seconds == 12 * 60 * 60
        assert started.yield_progress == Decimal("0.0000")
        assert "30 分钟" in started.status_hint
        assert started.pending_cultivation == 0
        assert started.pending_comprehension == 0
        assert started.pending_spirit_stone == 0

        status = retreat_service.get_retreat_status(
            character_id=created.character_id,
            now=start_time + timedelta(hours=6),
        )
        assert status is not None
        assert status.status == "running"
        assert status.can_settle is True
        assert status.reward_available is True
        assert status.elapsed_seconds == 6 * 60 * 60
        assert status.settlement_seconds == 6 * 60 * 60
        assert status.minimum_reward_seconds == 30 * 60
        assert status.full_yield_seconds == 12 * 60 * 60
        assert status.yield_progress == Decimal("0.5000")
        assert "50.0%" in status.status_hint
        assert status.pending_cultivation == 17
        assert status.pending_comprehension == 1
        assert status.pending_spirit_stone == 0

        with pytest.raises(RetreatAlreadyRunningError):
            retreat_service.start_retreat(
                character_id=created.character_id,
                now=start_time + timedelta(hours=1),
                duration=timedelta(hours=8),
            )


def test_settle_retreat_at_minimum_threshold_applies_minimum_reward_and_marks_completed(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """达到 30 分钟起算门槛后主动脱离，应按比例结算最小一档收益。"""
    database_url = _build_sqlite_url(tmp_path / "retreat_minimum_reward.db")
    _upgrade_database(database_url, monkeypatch)
    session_factory = create_session_factory(database_url)
    static_config = load_static_config()
    start_time = datetime(2026, 3, 26, 9, 0, 0)
    settle_time = start_time + timedelta(minutes=30)

    with session_scope(session_factory) as session:
        growth_service, retreat_service = _build_services(session, static_config)
        created = growth_service.create_character(
            discord_user_id="31002",
            player_display_name="临渊",
            character_name="闻道",
        )

        retreat_service.start_retreat(
            character_id=created.character_id,
            now=start_time,
            duration=timedelta(hours=12),
        )
        result = retreat_service.settle_retreat(character_id=created.character_id, now=settle_time)

        assert result.character_id == created.character_id
        assert result.realm_id == "mortal"
        assert result.started_at == start_time
        assert result.scheduled_end_at == start_time + timedelta(hours=12)
        assert result.settled_at == settle_time
        assert result.reward.actual_elapsed_seconds == 30 * 60
        assert result.reward.elapsed_seconds == 30 * 60
        assert result.reward.reward_seconds == 30 * 60
        assert result.reward.cultivation_amount == 1
        assert result.reward.comprehension_amount == 0
        assert result.reward.spirit_stone_amount == 0
        assert result.applied_cultivation == 1
        assert result.growth_snapshot.cultivation_value == 1
        assert result.growth_snapshot.comprehension_value == 0
        assert result.growth_snapshot.spirit_stone == 0
        assert result.growth_snapshot.stage_id == "early"
        assert result.reward_available is True
        assert result.minimum_reward_seconds == 30 * 60
        assert result.full_yield_seconds == 12 * 60 * 60

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


def test_settle_retreat_caps_rewards_after_twelve_hours_even_if_elapsed_longer(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """闭关超过 12 小时后单次收益不再增长，即使计划时长与实际脱离时间更长。"""
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

        assert result.reward.actual_elapsed_seconds == 5 * 24 * 60 * 60
        assert result.reward.elapsed_seconds == 3 * 24 * 60 * 60
        assert result.reward.reward_seconds == 12 * 60 * 60
        assert result.reward.cultivation_amount == 35
        assert result.reward.comprehension_amount == 2
        assert result.reward.spirit_stone_amount == 1
        assert result.applied_cultivation == 35
        assert result.growth_snapshot.cultivation_value == 35
        assert result.growth_snapshot.comprehension_value == 2
        assert result.growth_snapshot.spirit_stone == 1
        assert result.growth_snapshot.stage_id == "perfect"
        assert result.scheduled_end_at == start_time + timedelta(days=3)
        assert result.settled_at == settle_time
        assert result.minimum_reward_seconds == 30 * 60
        assert result.full_yield_seconds == 12 * 60 * 60


def test_retreat_duration_validation_and_sub_threshold_settlement_end_without_rewards(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """非法持续时间应拒绝开始，不足 30 分钟主动脱离则结束闭关但不给收益。"""
    database_url = _build_sqlite_url(tmp_path / "retreat_validation.db")
    _upgrade_database(database_url, monkeypatch)
    session_factory = create_session_factory(database_url)
    static_config = load_static_config()
    start_time = datetime(2026, 3, 26, 11, 0, 0)
    settle_time = start_time + timedelta(minutes=29)

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

        status = retreat_service.get_retreat_status(
            character_id=created.character_id,
            now=settle_time,
        )
        assert status is not None
        assert status.can_settle is True
        assert status.reward_available is False
        assert status.elapsed_seconds == 29 * 60
        assert status.settlement_seconds == 29 * 60
        assert status.pending_cultivation == 0
        assert status.pending_comprehension == 0
        assert status.pending_spirit_stone == 0
        assert "无收益" in status.status_hint

        result = retreat_service.settle_retreat(character_id=created.character_id, now=settle_time)
        assert result.reward.actual_elapsed_seconds == 29 * 60
        assert result.reward.elapsed_seconds == 29 * 60
        assert result.reward.reward_seconds == 29 * 60
        assert result.reward.cultivation_amount == 0
        assert result.reward.comprehension_amount == 0
        assert result.reward.spirit_stone_amount == 0
        assert result.applied_cultivation == 0
        assert result.growth_snapshot.cultivation_value == 0
        assert result.growth_snapshot.comprehension_value == 0
        assert result.growth_snapshot.spirit_stone == 0
        assert result.scheduled_end_at == start_time + timedelta(hours=10)
        assert result.settled_at == settle_time
        assert result.reward_available is False

        completed = retreat_service.get_retreat_status(character_id=created.character_id, now=settle_time)
        assert completed is not None
        assert completed.status == "completed"
        assert completed.can_settle is False

        with pytest.raises(RetreatNotReadyError):
            retreat_service.settle_retreat(character_id=created.character_id, now=settle_time)
