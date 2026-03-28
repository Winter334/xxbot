"""阶段 9 PVP 应用层集成测试。"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from alembic import command
from alembic.config import Config
import pytest

from application.character.current_attribute_service import CurrentAttributeService
from application.pvp import HonorCoinService, PvpChallengeNotAllowedError, PvpDefenseSnapshotService, PvpService
from domain.battle import BattleOutcome
from infrastructure.config.static import load_static_config
from infrastructure.db.models import (
    BattleReport,
    Character,
    CharacterProgress,
    CharacterScoreSnapshot,
    CurrencyBalance,
    LeaderboardEntrySnapshot,
    LeaderboardSnapshot,
    Player,
    PvpChallengeRecord,
)
from infrastructure.db.repositories import (
    SqlAlchemyBattleRecordRepository,
    SqlAlchemyCharacterRepository,
    SqlAlchemyCharacterScoreSnapshotRepository,
    SqlAlchemyHonorCoinLedgerRepository,
    SqlAlchemyPlayerRepository,
    SqlAlchemyPvpChallengeRepository,
    SqlAlchemySnapshotRepository,
)
from infrastructure.db.session import create_session_factory, session_scope

PROJECT_ROOT = Path(__file__).resolve().parents[2]
_FULL_RATIO = Decimal("1.0000")


class _StubAutoBattleService:
    """为阶段 9 编排测试提供可控战斗结果。"""

    def __init__(self, battle_record_repository: SqlAlchemyBattleRecordRepository, *, outcome: BattleOutcome) -> None:
        self._battle_record_repository = battle_record_repository
        self._outcome = outcome
        self.requests = []

    def execute(self, *, request, persist: bool = True):
        self.requests.append(request)
        persisted_battle_report_id = None
        if persist:
            report = self._battle_record_repository.add_battle_report(
                BattleReport(
                    character_id=request.character_id,
                    battle_type=request.battle_type,
                    result="victory" if self._outcome is BattleOutcome.ALLY_VICTORY else "defeat",
                    opponent_ref=request.opponent_ref,
                    summary_json={
                        "outcome": self._outcome.value,
                        "focus_unit_id": request.focus_unit_id,
                    },
                    detail_log_json={
                        "environment_snapshot": dict(request.environment_snapshot or {}),
                        "seed": request.snapshot.seed,
                    },
                )
            )
            persisted_battle_report_id = report.id
        return SimpleNamespace(
            persisted_battle_report_id=persisted_battle_report_id,
            domain_result=SimpleNamespace(outcome=self._outcome),
        )



def _build_sqlite_url(database_path: Path) -> str:
    """构造测试用 SQLite 地址。"""
    return f"sqlite+pysqlite:///{database_path.as_posix()}"



def _upgrade_database(database_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """将测试数据库升级到最新迁移版本。"""
    monkeypatch.chdir(PROJECT_ROOT)
    monkeypatch.setenv("DATABASE_URL", database_url)
    command.upgrade(Config((PROJECT_ROOT / "alembic.ini").as_posix()), "head")



def _create_character_state(
    *,
    player_repository: SqlAlchemyPlayerRepository,
    character_repository: SqlAlchemyCharacterRepository,
    score_snapshot_repository: SqlAlchemyCharacterScoreSnapshotRepository,
    discord_user_id: str,
    player_display_name: str,
    character_name: str,
    public_power_score: int,
    hidden_pvp_score: int,
    realm_id: str = "foundation",
    stage_id: str = "middle",
    computed_at: datetime | None = None,
) -> int:
    """创建带成长、货币与评分快照的角色。"""
    player = player_repository.add(Player(discord_user_id=discord_user_id, display_name=player_display_name))
    character = character_repository.add(
        Character(
            player_id=player.id,
            name=character_name,
            title="试炼修士",
            total_power_score=public_power_score,
            public_power_score=public_power_score,
            hidden_pvp_score=hidden_pvp_score,
        )
    )
    character_repository.save_progress(
        CharacterProgress(
            character_id=character.id,
            realm_id=realm_id,
            stage_id=stage_id,
            cultivation_value=120,
            comprehension_value=80,
            breakthrough_qualification_obtained=False,
            highest_endless_floor=18,
            current_hp_ratio=_FULL_RATIO,
            current_mp_ratio=_FULL_RATIO,
        )
    )
    character_repository.save_currency_balance(
        CurrencyBalance(
            character_id=character.id,
            spirit_stone=0,
            honor_coin=0,
        )
    )
    score_snapshot_repository.upsert_snapshot(
        CharacterScoreSnapshot(
            character_id=character.id,
            score_version="stage8.v1",
            total_power_score=public_power_score,
            public_power_score=public_power_score,
            hidden_pvp_score=hidden_pvp_score,
            growth_score=320,
            equipment_score=260,
            skill_score=210,
            artifact_score=180,
            pvp_adjustment_score=hidden_pvp_score - public_power_score,
            breakdown_json={
                "skill": {
                    "main_path_id": "zhanqing_sword",
                    "main_path_name": "斩青剑",
                },
                "source_summary": {
                    "character_name": character_name,
                    "equipped_slot_ids": [],
                },
            },
            source_digest=f"digest-{character.id}",
            computed_at=computed_at or datetime(2026, 3, 27, 8, 0, 0),
        )
    )
    return character.id



def _persist_pvp_leaderboard(
    snapshot_repository: SqlAlchemySnapshotRepository,
    *,
    generated_at: datetime,
    ranked_entries: list[dict[str, object]],
) -> LeaderboardSnapshot:
    """写入一份可供阶段 9 服务直接消费的 PVP 正式榜。"""
    entries = []
    for rank_position, payload in enumerate(ranked_entries, start=1):
        character_id = int(payload["character_id"])
        public_power_score = int(payload["public_power_score"])
        hidden_pvp_score = int(payload["hidden_pvp_score"])
        character_name = str(payload["character_name"])
        realm_id = str(payload.get("realm_id") or "foundation")
        stage_id = str(payload.get("stage_id") or "middle")
        entries.append(
            LeaderboardEntrySnapshot(
                character_id=character_id,
                rank_position=rank_position,
                score=hidden_pvp_score,
                summary_json={
                    "character_name": character_name,
                    "realm_id": realm_id,
                    "stage_id": stage_id,
                    "public_power_score": public_power_score,
                    "best_rank": rank_position,
                    "protected_until": None,
                    "latest_defense_snapshot_version": None,
                    "score_version": "stage8.v1",
                    "challenge_tier": "top50",
                    "reward_preview_tier": "top50",
                    "display_score": f"top50·{public_power_score}",
                    "hidden_score_exposed": False,
                },
            )
        )
    return snapshot_repository.replace_leaderboard_snapshot(
        LeaderboardSnapshot(
            board_type="pvp_challenge",
            generated_at=generated_at,
            scope_json={
                "generated_by": "stage9-test",
                "score_version": "stage8.v1",
                "schema_version": "stage9.pvp.v1",
            },
            entries=entries,
        )
    )



def _build_services(session, *, outcome: BattleOutcome = BattleOutcome.ALLY_VICTORY):
    """构造阶段 9 服务与仓储。"""
    static_config = load_static_config()
    character_repository = SqlAlchemyCharacterRepository(session)
    snapshot_repository = SqlAlchemySnapshotRepository(session)
    pvp_challenge_repository = SqlAlchemyPvpChallengeRepository(session)
    battle_record_repository = SqlAlchemyBattleRecordRepository(session)
    honor_coin_ledger_repository = SqlAlchemyHonorCoinLedgerRepository(session)
    auto_battle_service = _StubAutoBattleService(battle_record_repository, outcome=outcome)
    current_attribute_service = CurrentAttributeService(
        character_repository=character_repository,
        static_config=static_config,
    )
    defense_snapshot_service = PvpDefenseSnapshotService(
        character_repository=character_repository,
        snapshot_repository=snapshot_repository,
        current_attribute_service=current_attribute_service,
        static_config=static_config,
    )
    honor_coin_service = HonorCoinService(
        character_repository=character_repository,
        honor_coin_ledger_repository=honor_coin_ledger_repository,
        static_config=static_config,
    )
    pvp_service = PvpService(
        character_repository=character_repository,
        snapshot_repository=snapshot_repository,
        pvp_challenge_repository=pvp_challenge_repository,
        auto_battle_service=auto_battle_service,
        defense_snapshot_service=defense_snapshot_service,
        honor_coin_service=honor_coin_service,
        static_config=static_config,
    )
    return SimpleNamespace(
        character_repository=character_repository,
        snapshot_repository=snapshot_repository,
        pvp_challenge_repository=pvp_challenge_repository,
        battle_record_repository=battle_record_repository,
        honor_coin_ledger_repository=honor_coin_ledger_repository,
        current_attribute_service=current_attribute_service,
        defense_snapshot_service=defense_snapshot_service,
        honor_coin_service=honor_coin_service,
        pvp_service=pvp_service,
        auto_battle_service=auto_battle_service,
    )



def test_pvp_service_can_select_target_from_pool_and_complete_settlement(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """目标池中的合法目标应可被挑战，且结算后榜单、流水与奖励结构可用。"""
    database_url = _build_sqlite_url(tmp_path / "stage9_pvp_service_challenge.db")
    _upgrade_database(database_url, monkeypatch)
    session_factory = create_session_factory(database_url)
    now = datetime(2026, 3, 27, 12, 0, 0)

    with session_scope(session_factory) as session:
        player_repository = SqlAlchemyPlayerRepository(session)
        character_repository = SqlAlchemyCharacterRepository(session)
        score_snapshot_repository = SqlAlchemyCharacterScoreSnapshotRepository(session)
        services = _build_services(session)

        ranked_entries: list[dict[str, object]] = []
        for index, public_power_score in enumerate([1050, 1030, 1020, 1010, 1000], start=1):
            character_id = _create_character_state(
                player_repository=player_repository,
                character_repository=character_repository,
                score_snapshot_repository=score_snapshot_repository,
                discord_user_id=f"9200{index}",
                player_display_name=f"榜单修士{index}",
                character_name=f"修士{index}",
                public_power_score=public_power_score,
                hidden_pvp_score=public_power_score + 20,
                computed_at=now - timedelta(minutes=30),
            )
            aggregate = character_repository.get_aggregate(character_id)
            assert aggregate is not None
            ranked_entries.append(
                {
                    "character_id": character_id,
                    "character_name": aggregate.character.name,
                    "public_power_score": aggregate.character.public_power_score,
                    "hidden_pvp_score": aggregate.character.hidden_pvp_score,
                    "realm_id": aggregate.progress.realm_id,
                    "stage_id": aggregate.progress.stage_id,
                }
            )

        _persist_pvp_leaderboard(
            services.snapshot_repository,
            generated_at=now - timedelta(minutes=5),
            ranked_entries=ranked_entries,
        )
        attacker_id = int(ranked_entries[4]["character_id"])
        defender_id = int(ranked_entries[1]["character_id"])
        first_rank_id = int(ranked_entries[0]["character_id"])
        middle_rank_ids = [int(ranked_entries[2]["character_id"]), int(ranked_entries[3]["character_id"])]

        target_list = services.pvp_service.list_targets(character_id=attacker_id, now=now)
        assert defender_id in [target.character_id for target in target_list.targets]

        result = services.pvp_service.challenge_target(
            character_id=attacker_id,
            target_character_id=defender_id,
            now=now,
            seed=77,
        )

        assert len(services.auto_battle_service.requests) == 1
        assert services.auto_battle_service.requests[0].persist_progress_writeback is False
        assert result.battle_outcome == "ally_victory"
        assert result.rank_before_attacker == 5
        assert result.rank_after_attacker == 2
        assert result.rank_before_defender == 2
        assert result.rank_after_defender == 3
        assert result.rank_effect_applied is True
        assert result.honor_coin_delta == 20
        assert result.honor_coin_balance_after == 20
        assert result.reward_preview is not None
        assert result.reward_preview["reward_tier_id"] == "top3"
        assert [item["reward_id"] for item in result.display_rewards] == [
            "top3:title",
            "top3:avatar_frame",
        ]
        assert result.settlement["challenge_record_id"] == result.challenge_record_id

        latest_board = services.snapshot_repository.get_latest_leaderboard("pvp_challenge")
        assert latest_board is not None
        latest_rank_order = [entry.character_id for entry in services.snapshot_repository.list_latest_leaderboard_entries("pvp_challenge")]
        assert latest_rank_order == [first_rank_id, attacker_id, defender_id, *middle_rank_ids]

        challenge_records = services.pvp_challenge_repository.list_challenge_records_by_attacker(
            attacker_id,
            result.cycle_anchor_date,
        )
        assert len(challenge_records) == 1
        assert challenge_records[0].id == result.challenge_record_id
        assert challenge_records[0].settlement_json["honor_coin"]["delta"] == 20

        honor_ledgers = services.honor_coin_ledger_repository.list_by_character_id(attacker_id, limit=1)
        assert len(honor_ledgers) == 1
        assert honor_ledgers[0].delta == 20
        assert honor_ledgers[0].detail_json["reward_preview"]["reward_tier_id"] == "top3"
        assert honor_ledgers[0].detail_json["components"][0]["component_id"] == "base"



def test_defense_snapshot_service_reuses_locked_snapshot_and_refreshes_after_expiry(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """防守快照应在锁定周期内复用，锁定失效后生成新版本。"""
    database_url = _build_sqlite_url(tmp_path / "stage9_pvp_snapshot_lock.db")
    _upgrade_database(database_url, monkeypatch)
    session_factory = create_session_factory(database_url)
    now = datetime(2026, 3, 27, 12, 0, 0)

    with session_scope(session_factory) as session:
        player_repository = SqlAlchemyPlayerRepository(session)
        character_repository = SqlAlchemyCharacterRepository(session)
        score_snapshot_repository = SqlAlchemyCharacterScoreSnapshotRepository(session)
        services = _build_services(session)

        character_id = _create_character_state(
            player_repository=player_repository,
            character_repository=character_repository,
            score_snapshot_repository=score_snapshot_repository,
            discord_user_id="92101",
            player_display_name="快照修士",
            character_name="镜华",
            public_power_score=980,
            hidden_pvp_score=1000,
            computed_at=now - timedelta(minutes=15),
        )
        aggregate = character_repository.get_aggregate(character_id)
        assert aggregate is not None
        _persist_pvp_leaderboard(
            services.snapshot_repository,
            generated_at=now - timedelta(minutes=2),
            ranked_entries=[
                {
                    "character_id": character_id,
                    "character_name": aggregate.character.name,
                    "public_power_score": aggregate.character.public_power_score,
                    "hidden_pvp_score": aggregate.character.hidden_pvp_score,
                    "realm_id": aggregate.progress.realm_id,
                    "stage_id": aggregate.progress.stage_id,
                }
            ],
        )

        first_bundle = services.defense_snapshot_service.ensure_snapshot(
            character_id=character_id,
            now=now,
            requested_reason="challenge_start",
        )
        reused_bundle = services.defense_snapshot_service.ensure_snapshot(
            character_id=character_id,
            now=now + timedelta(hours=1),
            requested_reason="challenge_start",
        )
        refreshed_bundle = services.defense_snapshot_service.ensure_snapshot(
            character_id=character_id,
            now=now + timedelta(hours=25),
            requested_reason="challenge_start",
        )

        assert first_bundle.snapshot_state.snapshot_version == 1
        assert first_bundle.snapshot_state.formation["skill_loadout_version"] == load_static_config().skill_generation.config_version
        assert first_bundle.snapshot_state.summary["skill_loadout_version"] == load_static_config().skill_generation.config_version
        assert first_bundle.display_summary["skill_loadout_version"] == load_static_config().skill_generation.config_version
        assert "skill_config_version" not in first_bundle.snapshot_state.formation
        assert "skill_config_version" not in first_bundle.snapshot_state.summary
        assert reused_bundle.usage_decision.reuse_existing is True
        assert reused_bundle.snapshot_state.snapshot_id == first_bundle.snapshot_state.snapshot_id
        assert reused_bundle.snapshot_state.snapshot_version == first_bundle.snapshot_state.snapshot_version
        assert refreshed_bundle.usage_decision.requires_new_snapshot is True
        assert refreshed_bundle.usage_decision.reason_code == "snapshot_expired"
        assert refreshed_bundle.snapshot_state.snapshot_version == 2
        latest_snapshot = services.snapshot_repository.get_latest_pvp_defense_snapshot(character_id)
        assert latest_snapshot is not None
        assert latest_snapshot.snapshot_version == 2
        assert latest_snapshot.formation_json["skill_loadout_version"] == load_static_config().skill_generation.config_version
        assert latest_snapshot.summary_json["skill_loadout_version"] == load_static_config().skill_generation.config_version



def test_pvp_service_enforces_daily_limit_and_repeat_target_limit(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """接近新上限的挑战在完成两次结算后，应同时触发每日次数与同目标重复限制。"""
    database_url = _build_sqlite_url(tmp_path / "stage9_pvp_limit_guard.db")
    _upgrade_database(database_url, monkeypatch)
    session_factory = create_session_factory(database_url)
    now = datetime(2026, 3, 27, 12, 0, 0)
    cycle_anchor_date = date(2026, 3, 27)

    with session_scope(session_factory) as session:
        player_repository = SqlAlchemyPlayerRepository(session)
        character_repository = SqlAlchemyCharacterRepository(session)
        score_snapshot_repository = SqlAlchemyCharacterScoreSnapshotRepository(session)
        services = _build_services(session)

        power_scores = [1080, 1070, 1060, 1050, 1040, 1030, 1020, 1010]
        ranked_entries: list[dict[str, object]] = []
        for index, public_power_score in enumerate(power_scores, start=1):
            character_id = _create_character_state(
                player_repository=player_repository,
                character_repository=character_repository,
                score_snapshot_repository=score_snapshot_repository,
                discord_user_id=f"9220{index}",
                player_display_name=f"限制修士{index}",
                character_name=f"限制角色{index}",
                public_power_score=public_power_score,
                hidden_pvp_score=public_power_score + 10,
                computed_at=now - timedelta(minutes=20),
            )
            aggregate = character_repository.get_aggregate(character_id)
            assert aggregate is not None
            ranked_entries.append(
                {
                    "character_id": character_id,
                    "character_name": aggregate.character.name,
                    "public_power_score": aggregate.character.public_power_score,
                    "hidden_pvp_score": aggregate.character.hidden_pvp_score,
                    "realm_id": aggregate.progress.realm_id,
                    "stage_id": aggregate.progress.stage_id,
                }
            )

        latest_board = _persist_pvp_leaderboard(
            services.snapshot_repository,
            generated_at=now - timedelta(minutes=3),
            ranked_entries=ranked_entries,
        )
        attacker_id = int(ranked_entries[7]["character_id"])
        defender_id = int(ranked_entries[3]["character_id"])

        prior_activity = services.pvp_challenge_repository.get_or_create_daily_activity(attacker_id, cycle_anchor_date)
        prior_activity.effective_challenge_count = 4
        prior_activity.successful_challenge_count = 1
        prior_activity.last_challenge_at = now - timedelta(hours=1)
        services.pvp_challenge_repository.save_daily_activity(prior_activity)

        defender_snapshot = services.defense_snapshot_service.ensure_snapshot(
            character_id=defender_id,
            now=now - timedelta(minutes=10),
            requested_reason="defense_on_demand",
        )
        prior_battle_report = services.battle_record_repository.add_battle_report(
            BattleReport(
                character_id=attacker_id,
                battle_type="pvp_challenge",
                result="victory",
                opponent_ref=f"pvp:{defender_id}:v{defender_snapshot.snapshot_state.snapshot_version}",
                summary_json={"outcome": "ally_victory"},
                detail_log_json={"seed": 31},
                occurred_at=now - timedelta(minutes=30),
            )
        )
        services.pvp_challenge_repository.add_challenge_record(
            PvpChallengeRecord(
                attacker_character_id=attacker_id,
                defender_character_id=defender_id,
                defender_snapshot_id=defender_snapshot.snapshot_state.snapshot_id,
                leaderboard_snapshot_id=latest_board.id,
                battle_report_id=prior_battle_report.id,
                cycle_anchor_date=cycle_anchor_date,
                battle_outcome="ally_victory",
                rank_before_attacker=8,
                rank_before_defender=4,
                rank_after_attacker=4,
                rank_after_defender=5,
                honor_coin_delta=20,
                rank_effect_applied=True,
                settlement_json={"battle_report_id": prior_battle_report.id},
                created_at=now - timedelta(minutes=30),
            )
        )

        first_result = services.pvp_service.challenge_target(
            character_id=attacker_id,
            target_character_id=defender_id,
            now=now,
            seed=99,
        )
        assert first_result.rank_after_attacker == 4
        assert services.pvp_challenge_repository.get_daily_activity(attacker_id, cycle_anchor_date).effective_challenge_count == 5
        assert services.pvp_challenge_repository.count_effective_challenges_against_target(
            attacker_id,
            defender_id,
            cycle_anchor_date,
        ) == 2

        second_result = services.pvp_service.challenge_target(
            character_id=attacker_id,
            target_character_id=defender_id,
            now=now + timedelta(minutes=1),
            seed=100,
        )
        assert second_result.rank_after_attacker == 4
        assert services.pvp_challenge_repository.get_daily_activity(attacker_id, cycle_anchor_date).effective_challenge_count == 6
        assert services.pvp_challenge_repository.count_effective_challenges_against_target(
            attacker_id,
            defender_id,
            cycle_anchor_date,
        ) == 3

        with pytest.raises(PvpChallengeNotAllowedError) as exc_info:
            services.pvp_service.challenge_target(
                character_id=attacker_id,
                target_character_id=defender_id,
                now=now + timedelta(minutes=2),
                seed=101,
            )

        message = str(exc_info.value)
        assert "daily_challenge_limit_reached" in message
        assert "repeat_target_limit_reached" in message



def test_pvp_service_uses_lower_rank_fixed_honor_coin_reward_and_failure_base(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """挑战更低名次目标时胜利应固定获得 4 荣誉币，失败应收敛为 2 荣誉币。"""
    database_url = _build_sqlite_url(tmp_path / "stage9_pvp_lower_rank_reward.db")
    _upgrade_database(database_url, monkeypatch)
    session_factory = create_session_factory(database_url)
    now = datetime(2026, 3, 27, 12, 30, 0)

    with session_scope(session_factory) as session:
        player_repository = SqlAlchemyPlayerRepository(session)
        character_repository = SqlAlchemyCharacterRepository(session)
        score_snapshot_repository = SqlAlchemyCharacterScoreSnapshotRepository(session)
        victory_services = _build_services(session)

        ranked_entries: list[dict[str, object]] = []
        for index, public_power_score in enumerate([1100, 1080, 1060, 1040, 1020], start=1):
            character_id = _create_character_state(
                player_repository=player_repository,
                character_repository=character_repository,
                score_snapshot_repository=score_snapshot_repository,
                discord_user_id=f"9230{index}",
                player_display_name=f"低位修士{index}",
                character_name=f"低位角色{index}",
                public_power_score=public_power_score,
                hidden_pvp_score=public_power_score + 15,
                computed_at=now - timedelta(minutes=10),
            )
            aggregate = character_repository.get_aggregate(character_id)
            assert aggregate is not None
            ranked_entries.append(
                {
                    "character_id": character_id,
                    "character_name": aggregate.character.name,
                    "public_power_score": aggregate.character.public_power_score,
                    "hidden_pvp_score": aggregate.character.hidden_pvp_score,
                    "realm_id": aggregate.progress.realm_id,
                    "stage_id": aggregate.progress.stage_id,
                }
            )

        _persist_pvp_leaderboard(
            victory_services.snapshot_repository,
            generated_at=now - timedelta(minutes=2),
            ranked_entries=ranked_entries,
        )
        attacker_id = int(ranked_entries[1]["character_id"])
        lower_rank_defender_id = int(ranked_entries[3]["character_id"])

        victory_result = victory_services.pvp_service.challenge_target(
            character_id=attacker_id,
            target_character_id=lower_rank_defender_id,
            now=now,
            seed=55,
        )
        assert victory_result.battle_outcome == "ally_victory"
        assert victory_result.honor_coin_delta == 4
        assert victory_result.settlement["honor_coin"]["delta"] == 4
        components = victory_result.settlement["honor_coin"]["components"]
        assert len(components) == 1
        assert components[0]["component_id"] == "base"
        assert components[0]["applied_delta"] == 4

    with session_scope(session_factory) as session:
        defeat_services = _build_services(session, outcome=BattleOutcome.ENEMY_VICTORY)
        defeat_result = defeat_services.pvp_service.challenge_target(
            character_id=attacker_id,
            target_character_id=int(ranked_entries[0]["character_id"]),
            now=now + timedelta(minutes=5),
            seed=56,
        )
        assert defeat_result.battle_outcome == "enemy_victory"
        assert defeat_result.honor_coin_delta == 2
        assert defeat_result.settlement["honor_coin"]["delta"] == 2
        components = defeat_result.settlement["honor_coin"]["components"]
        assert len(components) == 1
        assert components[0]["component_id"] == "loss_floor"
        assert components[0]["applied_delta"] == 2



def test_pvp_service_stops_recording_top_rank_defense_failures_after_new_caps(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """前 3 / 前 10 名防守失败记录达到新上限后，不再继续累计。"""
    database_url = _build_sqlite_url(tmp_path / "stage9_pvp_defense_caps.db")
    _upgrade_database(database_url, monkeypatch)
    session_factory = create_session_factory(database_url)
    now = datetime(2026, 3, 27, 15, 0, 0)
    cycle_anchor_date = date(2026, 3, 27)

    with session_scope(session_factory) as session:
        player_repository = SqlAlchemyPlayerRepository(session)
        character_repository = SqlAlchemyCharacterRepository(session)
        score_snapshot_repository = SqlAlchemyCharacterScoreSnapshotRepository(session)
        services = _build_services(session)

        ranked_entries: list[dict[str, object]] = []
        for index, public_power_score in enumerate([1120, 1100, 1080, 1060, 1040, 1020, 1000, 980], start=1):
            character_id = _create_character_state(
                player_repository=player_repository,
                character_repository=character_repository,
                score_snapshot_repository=score_snapshot_repository,
                discord_user_id=f"9240{index}",
                player_display_name=f"守榜修士{index}",
                character_name=f"守榜角色{index}",
                public_power_score=public_power_score,
                hidden_pvp_score=public_power_score + 18,
                computed_at=now - timedelta(minutes=10),
            )
            aggregate = character_repository.get_aggregate(character_id)
            assert aggregate is not None
            ranked_entries.append(
                {
                    "character_id": character_id,
                    "character_name": aggregate.character.name,
                    "public_power_score": aggregate.character.public_power_score,
                    "hidden_pvp_score": aggregate.character.hidden_pvp_score,
                    "realm_id": aggregate.progress.realm_id,
                    "stage_id": aggregate.progress.stage_id,
                }
            )

        _persist_pvp_leaderboard(
            services.snapshot_repository,
            generated_at=now - timedelta(minutes=2),
            ranked_entries=ranked_entries,
        )

        top3_defender_id = int(ranked_entries[1]["character_id"])
        top10_defender_id = int(ranked_entries[5]["character_id"])
        attacker_for_top3 = int(ranked_entries[7]["character_id"])
        attacker_for_top10 = int(ranked_entries[6]["character_id"])

        top3_ledger = services.pvp_challenge_repository.get_or_create_daily_activity(top3_defender_id, cycle_anchor_date)
        top3_ledger.defense_failure_count = 3
        services.pvp_challenge_repository.save_daily_activity(top3_ledger)

        top10_ledger = services.pvp_challenge_repository.get_or_create_daily_activity(top10_defender_id, cycle_anchor_date)
        top10_ledger.defense_failure_count = 5
        services.pvp_challenge_repository.save_daily_activity(top10_ledger)

        target_list_for_top3 = services.pvp_service.list_targets(character_id=attacker_for_top3, now=now)
        top3_candidate_id = target_list_for_top3.targets[0].character_id
        target_list_for_top10 = services.pvp_service.list_targets(character_id=attacker_for_top10, now=now + timedelta(minutes=1))
        top10_candidate_id = target_list_for_top10.targets[0].character_id

        services.pvp_service.challenge_target(
            character_id=attacker_for_top3,
            target_character_id=top3_candidate_id,
            now=now,
            seed=87,
        )
        services.pvp_service.challenge_target(
            character_id=attacker_for_top10,
            target_character_id=top10_candidate_id,
            now=now + timedelta(minutes=1),
            seed=88,
        )

        refreshed_top3 = services.pvp_challenge_repository.get_daily_activity(top3_defender_id, cycle_anchor_date)
        refreshed_top10 = services.pvp_challenge_repository.get_daily_activity(top10_defender_id, cycle_anchor_date)
        assert refreshed_top3 is not None
        assert refreshed_top10 is not None
        assert refreshed_top3.defense_failure_count == 3
        assert refreshed_top10.defense_failure_count == 5
