"""打坐恢复服务集成测试。"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from alembic import command
from alembic.config import Config
import pytest

from application.character import (
    CharacterGrowthService,
    CharacterProgressionService,
    CultivationPanelService,
    CultivationPracticeBlockedError,
    RetreatService,
)
from application.dungeon import EndlessDungeonService, EndlessDungeonServiceError
from application.healing import HealingPanelService, RecoveryActionBlockedError
from infrastructure.config.static import load_static_config
from infrastructure.db.repositories import (
    SqlAlchemyCharacterRepository,
    SqlAlchemyInventoryRepository,
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
    """创建恢复测试所需服务。"""
    player_repository = SqlAlchemyPlayerRepository(session)
    character_repository = SqlAlchemyCharacterRepository(session)
    inventory_repository = SqlAlchemyInventoryRepository(session)
    state_repository = SqlAlchemyStateRepository(session)
    growth_service = CharacterGrowthService(
        player_repository=player_repository,
        character_repository=character_repository,
        static_config=static_config,
    )
    progression_service = CharacterProgressionService(
        character_repository=character_repository,
        inventory_repository=inventory_repository,
        static_config=static_config,
    )
    retreat_service = RetreatService(
        state_repository=state_repository,
        character_repository=character_repository,
        growth_service=growth_service,
        static_config=static_config,
    )
    healing_service = HealingPanelService(
        character_repository=character_repository,
        state_repository=state_repository,
        static_config=static_config,
    )
    cultivation_panel_service = CultivationPanelService(
        growth_service=growth_service,
        progression_service=progression_service,
        retreat_service=retreat_service,
        healing_panel_service=healing_service,
        static_config=static_config,
    )
    endless_service = EndlessDungeonService(
        state_repository=state_repository,
        character_repository=character_repository,
        static_config=static_config,
        healing_panel_service=healing_service,
    )
    return SimpleNamespace(
        growth_service=growth_service,
        character_repository=character_repository,
        retreat_service=retreat_service,
        healing_service=healing_service,
        cultivation_panel_service=cultivation_panel_service,
        endless_service=endless_service,
    )


def _set_character_resources(
    *,
    character_repository: SqlAlchemyCharacterRepository,
    character_id: int,
    current_hp_ratio: str,
    current_mp_ratio: str,
    highest_endless_floor: int | None = None,
) -> None:
    """直接设置角色当前生命/灵力比例。"""
    aggregate = character_repository.get_aggregate(character_id)
    assert aggregate is not None
    assert aggregate.progress is not None
    aggregate.progress.current_hp_ratio = Decimal(current_hp_ratio)
    aggregate.progress.current_mp_ratio = Decimal(current_mp_ratio)
    if highest_endless_floor is not None:
        aggregate.progress.highest_endless_floor = highest_endless_floor
    character_repository.save_progress(aggregate.progress)


def _create_injured_character(services, *, discord_user_id: str, player_display_name: str, character_name: str) -> int:
    """创建一个受伤角色。"""
    created = services.growth_service.create_character(
        discord_user_id=discord_user_id,
        player_display_name=player_display_name,
        character_name=character_name,
    )
    _set_character_resources(
        character_repository=services.character_repository,
        character_id=created.character_id,
        current_hp_ratio="0.4000",
        current_mp_ratio="0.2000",
    )
    return created.character_id


def test_start_recovery_sets_fixed_twenty_minute_window(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """开始打坐恢复后，应固定写入 20 分钟恢复窗口。"""
    database_url = _build_sqlite_url(tmp_path / "healing_start.db")
    _upgrade_database(database_url, monkeypatch)
    session_factory = create_session_factory(database_url)
    static_config = load_static_config()
    start_time = datetime(2026, 3, 26, 12, 0, 0)

    with session_scope(session_factory) as session:
        services = _build_services(session, static_config)
        character_id = _create_injured_character(
            services,
            discord_user_id="33001",
            player_display_name="青岚",
            character_name="怀瑾",
        )

        before = services.healing_service.get_panel_snapshot(character_id=character_id, now=start_time)
        assert before.can_start_recovery is True
        assert before.recovery_full_seconds == 20 * 60
        assert before.current_hp_ratio == Decimal("0.4000")
        assert before.current_mp_ratio == Decimal("0.2000")
        assert "20 分钟恢复 100%" in before.status_hint

        result = services.healing_service.execute_recovery_action(character_id=character_id, now=start_time)

        assert result.action_type == "start"
        assert result.snapshot.healing_status == "running"
        assert result.snapshot.started_at == start_time
        assert result.snapshot.scheduled_end_at == start_time + timedelta(minutes=20)
        assert result.snapshot.can_start_recovery is False
        assert result.snapshot.can_interrupt_recovery is True
        assert result.snapshot.can_complete_recovery is False
        assert result.snapshot.recovery_full_seconds == 20 * 60
        assert result.snapshot.elapsed_recovery_seconds == 0
        assert result.snapshot.remaining_recovery_seconds == 20 * 60
        assert result.snapshot.start_hp_ratio == Decimal("0.4000")
        assert result.snapshot.start_mp_ratio == Decimal("0.2000")
        assert services.healing_service.is_recovery_running(character_id=character_id) is True


def test_interrupt_recovery_after_ten_minutes_restores_proportionally(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """打坐 10 分钟后主动结束，应按 50% 进度恢复生命与灵力。"""
    database_url = _build_sqlite_url(tmp_path / "healing_partial.db")
    _upgrade_database(database_url, monkeypatch)
    session_factory = create_session_factory(database_url)
    static_config = load_static_config()
    start_time = datetime(2026, 3, 26, 12, 30, 0)
    interrupt_time = start_time + timedelta(minutes=10)

    with session_scope(session_factory) as session:
        services = _build_services(session, static_config)
        character_id = _create_injured_character(
            services,
            discord_user_id="33002",
            player_display_name="秋水",
            character_name="观澜",
        )

        services.healing_service.execute_recovery_action(character_id=character_id, now=start_time)

        mid_snapshot = services.healing_service.get_panel_snapshot(character_id=character_id, now=interrupt_time)
        assert mid_snapshot.healing_status == "running"
        assert mid_snapshot.elapsed_recovery_seconds == 10 * 60
        assert mid_snapshot.remaining_recovery_seconds == 10 * 60
        assert mid_snapshot.recovery_progress == Decimal("0.5000")
        assert mid_snapshot.recovery_progress_percent == Decimal("50.0")
        assert mid_snapshot.expected_hp_ratio == Decimal("0.7000")
        assert mid_snapshot.expected_mp_ratio == Decimal("0.6000")
        assert mid_snapshot.can_interrupt_recovery is True
        assert mid_snapshot.can_complete_recovery is False

        result = services.healing_service.execute_recovery_action(character_id=character_id, now=interrupt_time)

        assert result.action_type == "interrupt"
        assert result.snapshot.healing_status == "completed"
        assert result.snapshot.current_hp_ratio == Decimal("0.7000")
        assert result.snapshot.current_mp_ratio == Decimal("0.6000")
        assert result.snapshot.can_start_recovery is True
        assert services.healing_service.is_recovery_running(character_id=character_id) is False


def test_complete_recovery_after_twenty_minutes_restores_to_full(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """打坐满 20 分钟后结束，应恢复到满状态。"""
    database_url = _build_sqlite_url(tmp_path / "healing_full.db")
    _upgrade_database(database_url, monkeypatch)
    session_factory = create_session_factory(database_url)
    static_config = load_static_config()
    start_time = datetime(2026, 3, 26, 13, 0, 0)
    complete_time = start_time + timedelta(minutes=20)

    with session_scope(session_factory) as session:
        services = _build_services(session, static_config)
        character_id = _create_injured_character(
            services,
            discord_user_id="33003",
            player_display_name="鸣泉",
            character_name="折霜",
        )

        services.healing_service.execute_recovery_action(character_id=character_id, now=start_time)

        completion_snapshot = services.healing_service.get_panel_snapshot(character_id=character_id, now=complete_time)
        assert completion_snapshot.healing_status == "running"
        assert completion_snapshot.elapsed_recovery_seconds == 20 * 60
        assert completion_snapshot.remaining_recovery_seconds == 0
        assert completion_snapshot.recovery_progress == Decimal("1.0000")
        assert completion_snapshot.recovery_progress_percent == Decimal("100.0")
        assert completion_snapshot.can_complete_recovery is True
        assert completion_snapshot.can_interrupt_recovery is False

        result = services.healing_service.execute_recovery_action(character_id=character_id, now=complete_time)

        assert result.action_type == "complete"
        assert result.snapshot.healing_status == "completed"
        assert result.snapshot.current_hp_ratio == Decimal("1.0000")
        assert result.snapshot.current_mp_ratio == Decimal("1.0000")
        assert result.snapshot.can_start_recovery is False
        assert result.snapshot.status_hint == "当前状态完好，无需打坐恢复。"
        assert services.healing_service.is_recovery_running(character_id=character_id) is False


def test_second_click_while_recovering_interrupts_current_session(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """恢复进行中再次点击同一动作，应结束当前恢复而不是报错。"""
    database_url = _build_sqlite_url(tmp_path / "healing_repeat_click.db")
    _upgrade_database(database_url, monkeypatch)
    session_factory = create_session_factory(database_url)
    static_config = load_static_config()
    start_time = datetime(2026, 3, 26, 13, 30, 0)

    with session_scope(session_factory) as session:
        services = _build_services(session, static_config)
        character_id = _create_injured_character(
            services,
            discord_user_id="33004",
            player_display_name="松隐",
            character_name="惊鸿",
        )

        first_result = services.healing_service.execute_recovery_action(character_id=character_id, now=start_time)
        second_result = services.healing_service.execute_recovery_action(character_id=character_id, now=start_time)

        assert first_result.action_type == "start"
        assert second_result.action_type == "interrupt"
        assert second_result.snapshot.healing_status == "completed"
        assert second_result.snapshot.current_hp_ratio == Decimal("0.4000")
        assert second_result.snapshot.current_mp_ratio == Decimal("0.2000")
        assert services.healing_service.is_recovery_running(character_id=character_id) is False


def test_recovery_cannot_start_while_retreat_is_running(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """闭关进行中时，恢复面板应阻止开始打坐恢复。"""
    database_url = _build_sqlite_url(tmp_path / "healing_blocked_by_retreat.db")
    _upgrade_database(database_url, monkeypatch)
    session_factory = create_session_factory(database_url)
    static_config = load_static_config()
    start_time = datetime(2026, 3, 26, 14, 0, 0)

    with session_scope(session_factory) as session:
        services = _build_services(session, static_config)
        character_id = _create_injured_character(
            services,
            discord_user_id="33005",
            player_display_name="流景",
            character_name="照川",
        )

        services.retreat_service.start_retreat(
            character_id=character_id,
            now=start_time,
            duration=timedelta(hours=12),
        )

        snapshot = services.healing_service.get_panel_snapshot(
            character_id=character_id,
            now=start_time + timedelta(minutes=5),
        )
        assert snapshot.retreat_running is True
        assert snapshot.endless_running is False
        assert snapshot.can_start_recovery is False
        assert "闭关" in snapshot.status_hint

        with pytest.raises(RecoveryActionBlockedError, match="闭关"):
            services.healing_service.execute_recovery_action(
                character_id=character_id,
                now=start_time + timedelta(minutes=5),
            )


def test_recovery_cannot_start_while_endless_run_is_running(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """无尽副本运行中时，恢复面板应阻止开始打坐恢复。"""
    database_url = _build_sqlite_url(tmp_path / "healing_blocked_by_endless.db")
    _upgrade_database(database_url, monkeypatch)
    session_factory = create_session_factory(database_url)
    static_config = load_static_config()
    start_time = datetime(2026, 3, 26, 14, 30, 0)

    with session_scope(session_factory) as session:
        services = _build_services(session, static_config)
        character_id = _create_injured_character(
            services,
            discord_user_id="33006",
            player_display_name="砚秋",
            character_name="听雪",
        )

        services.endless_service.start_run(character_id=character_id, now=start_time)

        snapshot = services.healing_service.get_panel_snapshot(
            character_id=character_id,
            now=start_time + timedelta(minutes=1),
        )
        assert snapshot.retreat_running is False
        assert snapshot.endless_running is True
        assert snapshot.can_start_recovery is False
        assert "无尽副本" in snapshot.status_hint

        with pytest.raises(RecoveryActionBlockedError, match="无尽副本"):
            services.healing_service.execute_recovery_action(
                character_id=character_id,
                now=start_time + timedelta(minutes=1),
            )


def test_recovery_running_blocks_practice_once_and_endless_entry(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """恢复进行中时，应拦截单次修炼与无尽副本开始。"""
    database_url = _build_sqlite_url(tmp_path / "healing_action_block.db")
    _upgrade_database(database_url, monkeypatch)
    session_factory = create_session_factory(database_url)
    static_config = load_static_config()
    start_time = datetime(2026, 3, 26, 15, 0, 0)

    with session_scope(session_factory) as session:
        services = _build_services(session, static_config)
        practice_character_id = _create_injured_character(
            services,
            discord_user_id="33007",
            player_display_name="行止",
            character_name="望舒",
        )
        endless_character_id = _create_injured_character(
            services,
            discord_user_id="33008",
            player_display_name="沉璧",
            character_name="见微",
        )

        services.healing_service.execute_recovery_action(character_id=practice_character_id, now=start_time)
        services.healing_service.execute_recovery_action(character_id=endless_character_id, now=start_time)

        with pytest.raises(CultivationPracticeBlockedError, match="单次修炼"):
            services.cultivation_panel_service.practice_once(character_id=practice_character_id)

        with pytest.raises(EndlessDungeonServiceError, match="打坐恢复"):
            services.endless_service.start_run(
                character_id=endless_character_id,
                now=start_time + timedelta(minutes=1),
            )
