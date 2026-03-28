"""阶段 9 PVP 仓储与迁移集成测试。"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

from infrastructure.db.models import (
    BattleReport,
    Character,
    HonorCoinLedger,
    LeaderboardEntrySnapshot,
    LeaderboardSnapshot,
    Player,
    PvpChallengeRecord,
    PvpDefenseSnapshot,
)
from infrastructure.db.repositories import (
    SqlAlchemyBattleRecordRepository,
    SqlAlchemyCharacterRepository,
    SqlAlchemyHonorCoinLedgerRepository,
    SqlAlchemyPlayerRepository,
    SqlAlchemyPvpChallengeRepository,
    SqlAlchemySnapshotRepository,
)
from infrastructure.db.session import create_engine_from_url, create_session_factory, session_scope

PROJECT_ROOT = Path(__file__).resolve().parents[3]
EXPECTED_STAGE9_TABLES = {
    "pvp_daily_activity_ledgers",
    "pvp_challenge_records",
    "honor_coin_ledgers",
}



def _build_sqlite_url(database_path: Path) -> str:
    """构造测试用 SQLite 地址。"""
    return f"sqlite+pysqlite:///{database_path.as_posix()}"



def _upgrade_database(database_url: str, monkeypatch) -> None:
    """将测试数据库升级到最新迁移版本。"""
    monkeypatch.chdir(PROJECT_ROOT)
    monkeypatch.setenv("DATABASE_URL", database_url)
    command.upgrade(Config((PROJECT_ROOT / "alembic.ini").as_posix()), "head")



def test_stage9_migration_adds_pvp_tables_columns_and_indexes(tmp_path, monkeypatch) -> None:
    """阶段 9 迁移应补齐快照扩展、日账本、挑战记录与荣誉币流水结构。"""
    database_url = _build_sqlite_url(tmp_path / "stage9_migration.db")
    _upgrade_database(database_url, monkeypatch)

    engine = create_engine_from_url(database_url)
    try:
        inspector = inspect(engine)
        table_names = set(inspector.get_table_names())
        assert EXPECTED_STAGE9_TABLES.issubset(table_names)

        defense_columns = {column["name"] for column in inspector.get_columns("pvp_defense_snapshots")}
        assert {
            "public_power_score",
            "hidden_pvp_score",
            "score_version",
            "snapshot_reason",
            "build_fingerprint",
            "summary_json",
            "lock_started_at",
            "lock_expires_at",
        }.issubset(defense_columns)

        defense_indexes = {index["name"] for index in inspector.get_indexes("pvp_defense_snapshots")}
        assert "ix_pvp_defense_snapshots_character_lock_expires_at" in defense_indexes
        assert "ix_pvp_defense_snapshots_build_fingerprint" in defense_indexes

        challenge_indexes = {index["name"] for index in inspector.get_indexes("pvp_challenge_records")}
        assert {
            "ix_pvp_challenge_records_attacker_cycle_created_at",
            "ix_pvp_challenge_records_defender_cycle_created_at",
            "ix_pvp_challenge_records_attacker_defender_cycle",
            "ix_pvp_challenge_records_leaderboard_snapshot_id",
            "ix_pvp_challenge_records_defender_snapshot_id",
        }.issubset(challenge_indexes)

        honor_indexes = {index["name"] for index in inspector.get_indexes("honor_coin_ledgers")}
        assert "ix_honor_coin_ledgers_character_id_created_at" in honor_indexes
        assert "ix_honor_coin_ledgers_source_type" in honor_indexes
    finally:
        engine.dispose()



def test_stage9_repositories_round_trip_pvp_snapshots_leaderboard_daily_activity_and_ledgers(
    tmp_path,
    monkeypatch,
) -> None:
    """阶段 9 仓储应能完成防守快照、正式榜、挑战记录、日账本与荣誉币流水读写。"""
    database_url = _build_sqlite_url(tmp_path / "stage9_repositories.db")
    _upgrade_database(database_url, monkeypatch)
    session_factory = create_session_factory(database_url)
    cycle_anchor_date = date(2026, 3, 27)
    now = datetime(2026, 3, 27, 12, 0, 0)

    with session_scope(session_factory) as session:
        player_repository = SqlAlchemyPlayerRepository(session)
        character_repository = SqlAlchemyCharacterRepository(session)
        battle_record_repository = SqlAlchemyBattleRecordRepository(session)
        snapshot_repository = SqlAlchemySnapshotRepository(session)
        pvp_challenge_repository = SqlAlchemyPvpChallengeRepository(session)
        honor_coin_ledger_repository = SqlAlchemyHonorCoinLedgerRepository(session)

        attacker_player = player_repository.add(Player(discord_user_id="91001", display_name="天枢"))
        defender_player = player_repository.add(Player(discord_user_id="91002", display_name="天璇"))
        attacker = character_repository.add(
            Character(
                player_id=attacker_player.id,
                name="玄临",
                title="剑修",
                total_power_score=1200,
                public_power_score=1150,
                hidden_pvp_score=1180,
            )
        )
        defender = character_repository.add(
            Character(
                player_id=defender_player.id,
                name="青岚",
                title="法修",
                total_power_score=1190,
                public_power_score=1120,
                hidden_pvp_score=1160,
            )
        )

        expired_snapshot = snapshot_repository.add_pvp_defense_snapshot(
            PvpDefenseSnapshot(
                character_id=defender.id,
                snapshot_version=1,
                power_score=1190,
                public_power_score=1120,
                hidden_pvp_score=1160,
                score_version="stage8.v1",
                snapshot_reason="enter_ladder",
                build_fingerprint="build-v1",
                rank_position=2,
                formation_json={"behavior_template_id": "zhanqing_sword"},
                stats_json={"max_hp": 120, "attack_power": 28},
                summary_json={"character_name": "青岚", "display_summary": "旧快照"},
                source_updated_at=now - timedelta(days=1),
                lock_started_at=now - timedelta(days=1),
                lock_expires_at=now - timedelta(hours=1),
                created_at=now - timedelta(days=1),
            )
        )
        active_snapshot = snapshot_repository.add_pvp_defense_snapshot(
            PvpDefenseSnapshot(
                character_id=defender.id,
                snapshot_version=2,
                power_score=1210,
                public_power_score=1140,
                hidden_pvp_score=1175,
                score_version="stage9.v1",
                snapshot_reason="defense_on_demand",
                build_fingerprint="build-v2",
                rank_position=2,
                formation_json={"behavior_template_id": "zhanqing_sword"},
                stats_json={"max_hp": 140, "attack_power": 32},
                summary_json={"character_name": "青岚", "display_summary": "当前快照"},
                source_updated_at=now,
                lock_started_at=now - timedelta(minutes=30),
                lock_expires_at=now + timedelta(hours=23),
                created_at=now,
            )
        )

        latest_snapshot = snapshot_repository.get_latest_pvp_defense_snapshot(defender.id)
        current_snapshot = snapshot_repository.get_active_pvp_defense_snapshot(defender.id, now)
        assert latest_snapshot is not None
        assert current_snapshot is not None
        assert latest_snapshot.id == active_snapshot.id
        assert current_snapshot.id == active_snapshot.id
        assert snapshot_repository.get_pvp_defense_snapshot(active_snapshot.id).build_fingerprint == "build-v2"
        assert expired_snapshot.id != active_snapshot.id

        initial_board = snapshot_repository.add_leaderboard_snapshot(
            LeaderboardSnapshot(
                board_type="pvp_challenge",
                generated_at=now - timedelta(minutes=10),
                scope_json={"generated_by": "test-seed", "score_version": "stage8.v1"},
                entries=[
                    LeaderboardEntrySnapshot(
                        character_id=attacker.id,
                        rank_position=1,
                        score=1180,
                        summary_json={"character_name": "玄临", "public_power_score": 1150},
                    ),
                    LeaderboardEntrySnapshot(
                        character_id=defender.id,
                        rank_position=2,
                        score=1175,
                        summary_json={"character_name": "青岚", "public_power_score": 1140},
                    ),
                ],
            )
        )
        replaced_board = snapshot_repository.replace_leaderboard_snapshot(
            LeaderboardSnapshot(
                board_type="pvp_challenge",
                generated_at=now,
                scope_json={"generated_by": "test-refresh", "score_version": "stage9.v1"},
                entries=[
                    LeaderboardEntrySnapshot(
                        character_id=defender.id,
                        rank_position=1,
                        score=1175,
                        summary_json={"character_name": "青岚", "public_power_score": 1140},
                    ),
                    LeaderboardEntrySnapshot(
                        character_id=attacker.id,
                        rank_position=2,
                        score=1180,
                        summary_json={"character_name": "玄临", "public_power_score": 1150},
                    ),
                ],
            )
        )

        latest_board = snapshot_repository.get_latest_leaderboard("pvp_challenge")
        assert latest_board is not None
        assert latest_board.id == replaced_board.id
        assert latest_board.id != initial_board.id
        assert [entry.character_id for entry in snapshot_repository.list_latest_leaderboard_entries("pvp_challenge")] == [
            defender.id,
            attacker.id,
        ]
        latest_entry = snapshot_repository.get_latest_leaderboard_entry("pvp_challenge", attacker.id)
        assert latest_entry is not None
        assert latest_entry.rank_position == 2

        activity = pvp_challenge_repository.get_or_create_daily_activity(attacker.id, cycle_anchor_date)
        assert activity.effective_challenge_count == 0
        activity.effective_challenge_count = 1
        activity.successful_challenge_count = 1
        activity.last_challenge_at = now
        pvp_challenge_repository.save_daily_activity(activity)

        persisted_activity = pvp_challenge_repository.get_daily_activity(attacker.id, cycle_anchor_date)
        assert persisted_activity is not None
        assert persisted_activity.successful_challenge_count == 1
        assert persisted_activity.last_challenge_at == now

        first_battle_report = battle_record_repository.add_battle_report(
            BattleReport(
                character_id=attacker.id,
                battle_type="pvp_challenge",
                result="victory",
                opponent_ref=f"pvp:{defender.id}:v2",
                summary_json={"outcome": "ally_victory"},
                detail_log_json={"seed": 11},
                occurred_at=now,
            )
        )
        second_battle_report = battle_record_repository.add_battle_report(
            BattleReport(
                character_id=attacker.id,
                battle_type="pvp_challenge",
                result="victory",
                opponent_ref=f"pvp:{defender.id}:v2",
                summary_json={"outcome": "ally_victory"},
                detail_log_json={"seed": 12},
                occurred_at=now + timedelta(minutes=5),
            )
        )
        first_record = pvp_challenge_repository.add_challenge_record(
            PvpChallengeRecord(
                attacker_character_id=attacker.id,
                defender_character_id=defender.id,
                defender_snapshot_id=active_snapshot.id,
                leaderboard_snapshot_id=replaced_board.id,
                battle_report_id=first_battle_report.id,
                cycle_anchor_date=cycle_anchor_date,
                battle_outcome="ally_victory",
                rank_before_attacker=2,
                rank_before_defender=1,
                rank_after_attacker=1,
                rank_after_defender=2,
                honor_coin_delta=26,
                rank_effect_applied=True,
                settlement_json={"battle_report_id": first_battle_report.id},
                created_at=now,
            )
        )
        second_record = pvp_challenge_repository.add_challenge_record(
            PvpChallengeRecord(
                attacker_character_id=attacker.id,
                defender_character_id=defender.id,
                defender_snapshot_id=active_snapshot.id,
                leaderboard_snapshot_id=replaced_board.id,
                battle_report_id=second_battle_report.id,
                cycle_anchor_date=cycle_anchor_date,
                battle_outcome="enemy_victory",
                rank_before_attacker=1,
                rank_before_defender=2,
                rank_after_attacker=1,
                rank_after_defender=2,
                honor_coin_delta=6,
                rank_effect_applied=False,
                settlement_json={"battle_report_id": second_battle_report.id},
                created_at=now + timedelta(minutes=5),
            )
        )

        attacker_records = pvp_challenge_repository.list_challenge_records_by_attacker(attacker.id, cycle_anchor_date)
        assert [record.id for record in attacker_records] == [second_record.id, first_record.id]
        assert pvp_challenge_repository.count_effective_challenges_against_target(
            attacker.id,
            defender.id,
            cycle_anchor_date,
        ) == 2

        honor_coin_ledger_repository.add_ledger(
            HonorCoinLedger(
                character_id=attacker.id,
                source_type="pvp_challenge",
                source_ref=f"battle_report:{first_battle_report.id}",
                delta=26,
                balance_after=126,
                detail_json={"schema_version": "stage9.honor_coin.v1", "battle_report_id": first_battle_report.id},
                created_at=now,
            )
        )
        newer_ledger = honor_coin_ledger_repository.add_ledger(
            HonorCoinLedger(
                character_id=attacker.id,
                source_type="pvp_challenge",
                source_ref=f"battle_report:{second_battle_report.id}",
                delta=6,
                balance_after=132,
                detail_json={"schema_version": "stage9.honor_coin.v1", "battle_report_id": second_battle_report.id},
                created_at=now + timedelta(minutes=5),
            )
        )

        latest_ledgers = honor_coin_ledger_repository.list_by_character_id(attacker.id, limit=1)
        assert len(latest_ledgers) == 1
        assert latest_ledgers[0].id == newer_ledger.id
        assert latest_ledgers[0].detail_json["schema_version"] == "stage9.honor_coin.v1"
