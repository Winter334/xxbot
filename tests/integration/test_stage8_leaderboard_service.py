"""阶段 8 榜单查询与刷新集成测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from alembic import command
from alembic.config import Config
import pytest

from application.character import CharacterGrowthService
from application.ranking import LeaderboardQueryService, LeaderboardRefreshService
from domain.ranking import LeaderboardBoardType
from infrastructure.config.static import load_static_config
from infrastructure.db.models import Character, CharacterProgress, CharacterScoreSnapshot, Player
from infrastructure.db.repositories import (
    SqlAlchemyCharacterRepository,
    SqlAlchemyCharacterScoreSnapshotRepository,
    SqlAlchemyPlayerRepository,
    SqlAlchemySnapshotRepository,
)
from infrastructure.db.session import create_engine_from_url, create_session_factory, session_scope

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _build_sqlite_url(database_path: Path) -> str:
    """构造测试用 SQLite 地址。"""
    return f"sqlite+pysqlite:///{database_path.as_posix()}"


def _upgrade_database(database_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """将测试数据库升级到最新迁移版本。"""
    monkeypatch.chdir(PROJECT_ROOT)
    monkeypatch.setenv("DATABASE_URL", database_url)
    command.upgrade(Config((PROJECT_ROOT / "alembic.ini").as_posix()), "head")


def _create_character_with_score(
    growth_service: CharacterGrowthService,
    character_repository: SqlAlchemyCharacterRepository,
    score_snapshot_repository: SqlAlchemyCharacterScoreSnapshotRepository,
    *,
    discord_user_id: str,
    player_display_name: str,
    character_name: str,
    total_power_score: int,
    public_power_score: int,
    hidden_pvp_score: int,
    growth_score: int,
    equipment_score: int,
    skill_score: int,
    artifact_score: int,
    pvp_adjustment_score: int,
    highest_endless_floor: int,
    realm_id: str,
    stage_id: str,
    main_path_id: str,
    main_path_name: str,
) -> int:
    created = growth_service.create_character(
        discord_user_id=discord_user_id,
        player_display_name=player_display_name,
        character_name=character_name,
    )
    character_id = created.character_id
    character = character_repository.get(character_id)
    aggregate = character_repository.get_aggregate(character_id)
    assert character is not None
    assert aggregate is not None
    progress = aggregate.progress
    assert progress is not None
    progress.realm_id = realm_id
    progress.stage_id = stage_id
    progress.highest_endless_floor = highest_endless_floor
    character_repository.save_progress(progress)
    character_repository.save_score_cache(
        character=character,
        total_power_score=total_power_score,
        public_power_score=public_power_score,
        hidden_pvp_score=hidden_pvp_score,
    )
    score_snapshot_repository.upsert_snapshot(
        CharacterScoreSnapshot(
            character_id=character_id,
            score_version="stage8.v1",
            total_power_score=total_power_score,
            public_power_score=public_power_score,
            hidden_pvp_score=hidden_pvp_score,
            growth_score=growth_score,
            equipment_score=equipment_score,
            skill_score=skill_score,
            artifact_score=artifact_score,
            pvp_adjustment_score=pvp_adjustment_score,
            breakdown_json={
                "skill": {
                    "main_path_id": main_path_id,
                    "main_path_name": main_path_name,
                    "main_skill_name": main_path_name,
                    "main_skill": {
                        "item_id": character_id * 10 + 1,
                        "skill_name": main_path_name,
                        "path_id": main_path_id,
                        "path_name": main_path_name,
                        "rank_name": "三阶",
                        "quality_name": "珍品",
                        "total_budget": 18,
                    },
                    "guard_skill": {
                        "item_id": character_id * 10 + 2,
                        "skill_name": f"{main_path_name}护体",
                        "path_id": main_path_id,
                        "path_name": main_path_name,
                        "rank_name": "二阶",
                        "quality_name": "良品",
                        "total_budget": 6,
                    },
                    "movement_skill": {
                        "item_id": character_id * 10 + 3,
                        "skill_name": f"{main_path_name}身法",
                        "path_id": main_path_id,
                        "path_name": main_path_name,
                        "rank_name": "二阶",
                        "quality_name": "良品",
                        "total_budget": 6,
                    },
                    "spirit_skill": {
                        "item_id": character_id * 10 + 4,
                        "skill_name": f"{main_path_name}灵技",
                        "path_id": main_path_id,
                        "path_name": main_path_name,
                        "rank_name": "二阶",
                        "quality_name": "良品",
                        "total_budget": 6,
                    },
                },
                "totals": {
                    "public_power_score": public_power_score,
                    "hidden_pvp_score": hidden_pvp_score,
                },
            },
            source_digest=f"digest-{character_id}",
        )
    )
    return character_id


def test_refresh_and_query_launch_leaderboards(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """三张首发榜单应可刷新并按最新快照查询。"""
    database_url = _build_sqlite_url(tmp_path / "stage8_leaderboard_refresh.db")
    _upgrade_database(database_url, monkeypatch)
    session_factory = create_session_factory(database_url)
    static_config = load_static_config()

    with session_scope(session_factory) as session:
        player_repository = SqlAlchemyPlayerRepository(session)
        character_repository = SqlAlchemyCharacterRepository(session)
        score_snapshot_repository = SqlAlchemyCharacterScoreSnapshotRepository(session)
        snapshot_repository = SqlAlchemySnapshotRepository(session)
        growth_service = CharacterGrowthService(
            player_repository=player_repository,
            character_repository=character_repository,
            static_config=static_config,
            score_service=None,
        )
        refresh_service = LeaderboardRefreshService(
            character_repository=character_repository,
            snapshot_repository=snapshot_repository,
            static_config=static_config,
            stale_after_seconds=180,
        )
        query_service = LeaderboardQueryService(
            snapshot_repository=snapshot_repository,
            stale_after_seconds=180,
        )

        first_id = _create_character_with_score(
            growth_service,
            character_repository,
            score_snapshot_repository,
            discord_user_id="82001",
            player_display_name="榜单甲",
            character_name="青岳",
            total_power_score=980,
            public_power_score=980,
            hidden_pvp_score=1080,
            growth_score=320,
            equipment_score=280,
            skill_score=200,
            artifact_score=180,
            pvp_adjustment_score=100,
            highest_endless_floor=58,
            realm_id="foundation",
            stage_id="late",
            main_path_id="zhanqing_sword",
            main_path_name="斩青剑",
        )
        second_id = _create_character_with_score(
            growth_service,
            character_repository,
            score_snapshot_repository,
            discord_user_id="82002",
            player_display_name="榜单乙",
            character_name="流炎",
            total_power_score=1020,
            public_power_score=1020,
            hidden_pvp_score=1050,
            growth_score=340,
            equipment_score=300,
            skill_score=210,
            artifact_score=170,
            pvp_adjustment_score=30,
            highest_endless_floor=54,
            realm_id="foundation",
            stage_id="perfect",
            main_path_id="wangchuan_spell",
            main_path_name="忘川术",
        )
        third_id = _create_character_with_score(
            growth_service,
            character_repository,
            score_snapshot_repository,
            discord_user_id="82003",
            player_display_name="榜单丙",
            character_name="玄璃",
            total_power_score=900,
            public_power_score=900,
            hidden_pvp_score=990,
            growth_score=310,
            equipment_score=250,
            skill_score=180,
            artifact_score=160,
            pvp_adjustment_score=90,
            highest_endless_floor=63,
            realm_id="foundation",
            stage_id="middle",
            main_path_id="changsheng_body",
            main_path_name="长生体",
        )

        results = refresh_service.refresh_launch_boards()

        assert len(results) == 3
        power_page = query_service.query_leaderboard(board_type=LeaderboardBoardType.POWER, page=1, page_size=3)
        pvp_page = query_service.query_leaderboard(board_type=LeaderboardBoardType.PVP_CHALLENGE, page=1, page_size=3)
        endless_page = query_service.query_leaderboard(board_type=LeaderboardBoardType.ENDLESS_DEPTH, page=1, page_size=3)

        assert power_page.status == "ready"
        assert power_page.stale is False
        assert power_page.total_entries == 3
        assert [entry.character_id for entry in power_page.entries] == [second_id, first_id, third_id]
        assert power_page.entries[0].display_score == "1020"
        assert power_page.entries[0].summary["growth_score"] == 340
        assert power_page.entries[0].summary["main_skill_name"] == "忘川术"
        assert power_page.entries[0].summary["main_skill"]["rank_name"] == "三阶"

        assert pvp_page.status == "ready"
        assert [entry.character_id for entry in pvp_page.entries] == [first_id, second_id, third_id]
        assert pvp_page.entries[0].summary["hidden_score_exposed"] is False
        assert "hidden_pvp_score" not in pvp_page.entries[0].summary
        assert pvp_page.entries[0].summary["challenge_tier"]
        assert pvp_page.entries[0].summary["main_skill_name"] == "斩青剑"
        assert len(pvp_page.entries[0].summary["auxiliary_skills"]) == 3

        assert endless_page.status == "ready"
        assert [entry.character_id for entry in endless_page.entries] == [third_id, first_id, second_id]
        assert endless_page.entries[0].score == 63
        assert endless_page.entries[0].summary["highest_region_name"]
        assert endless_page.entries[0].display_score == "第 63 层"


def test_query_stale_snapshot_returns_latest_page_and_requests_refresh(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """过期快照应直接返回旧榜并请求后台刷新。"""
    database_url = _build_sqlite_url(tmp_path / "stage8_leaderboard_stale.db")
    _upgrade_database(database_url, monkeypatch)
    session_factory = create_session_factory(database_url)
    static_config = load_static_config()

    class StubRefreshCoordinator:
        def __init__(self) -> None:
            self.requested_board_types: list[LeaderboardBoardType] = []

        def request_refresh(self, *, board_type: LeaderboardBoardType) -> None:
            self.requested_board_types.append(board_type)

    with session_scope(session_factory) as session:
        player_repository = SqlAlchemyPlayerRepository(session)
        character_repository = SqlAlchemyCharacterRepository(session)
        score_snapshot_repository = SqlAlchemyCharacterScoreSnapshotRepository(session)
        snapshot_repository = SqlAlchemySnapshotRepository(session)
        growth_service = CharacterGrowthService(
            player_repository=player_repository,
            character_repository=character_repository,
            static_config=static_config,
            score_service=None,
        )
        refresh_service = LeaderboardRefreshService(
            character_repository=character_repository,
            snapshot_repository=snapshot_repository,
            static_config=static_config,
            stale_after_seconds=180,
        )

        character_id = _create_character_with_score(
            growth_service,
            character_repository,
            score_snapshot_repository,
            discord_user_id="82011",
            player_display_name="过期榜单甲",
            character_name="临渊",
            total_power_score=960,
            public_power_score=960,
            hidden_pvp_score=1010,
            growth_score=330,
            equipment_score=260,
            skill_score=210,
            artifact_score=160,
            pvp_adjustment_score=50,
            highest_endless_floor=48,
            realm_id="foundation",
            stage_id="late",
            main_path_id="zhanqing_sword",
            main_path_name="斩青剑",
        )
        refresh_service.refresh_board(board_type=LeaderboardBoardType.POWER)
        latest_snapshot = snapshot_repository.get_latest_leaderboard(LeaderboardBoardType.POWER.value)
        assert latest_snapshot is not None
        latest_snapshot.generated_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=10)
        session.flush()

        refresh_coordinator = StubRefreshCoordinator()
        query_service = LeaderboardQueryService(
            snapshot_repository=snapshot_repository,
            refresh_coordinator=refresh_coordinator,
            stale_after_seconds=30,
        )

        page = query_service.query_leaderboard(board_type=LeaderboardBoardType.POWER, page=1, page_size=10)

        assert page.status == "ready"
        assert page.stale is True
        assert page.total_entries == 1
        assert [entry.character_id for entry in page.entries] == [character_id]
        assert page.entries[0].display_score == "960"
        assert refresh_coordinator.requested_board_types == [LeaderboardBoardType.POWER]


def test_query_without_snapshot_returns_preparing_and_requests_refresh(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """冷启动无快照时应返回准备中状态并请求后台刷新。"""
    database_url = _build_sqlite_url(tmp_path / "stage8_leaderboard_prepare.db")
    _upgrade_database(database_url, monkeypatch)
    session_factory = create_session_factory(database_url)

    class StubRefreshCoordinator:
        def __init__(self) -> None:
            self.requested_board_types: list[LeaderboardBoardType] = []

        def request_refresh(self, *, board_type: LeaderboardBoardType) -> None:
            self.requested_board_types.append(board_type)

    with session_scope(session_factory) as session:
        snapshot_repository = SqlAlchemySnapshotRepository(session)
        refresh_coordinator = StubRefreshCoordinator()
        query_service = LeaderboardQueryService(
            snapshot_repository=snapshot_repository,
            refresh_coordinator=refresh_coordinator,
        )

        page = query_service.query_leaderboard(board_type=LeaderboardBoardType.POWER, page=1, page_size=10)

        assert page.status == "preparing"
        assert page.snapshot_generated_at is None
        assert page.total_entries == 0
        assert refresh_coordinator.requested_board_types == [LeaderboardBoardType.POWER]
