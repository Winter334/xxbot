"""无尽副本运行态应用服务集成测试。"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path

from alembic import command
from alembic.config import Config
import pytest

from application.battle import AutoBattleService
from application.character import CharacterGrowthService
from application.character.skill_drop_service import SkillDropService
from application.dungeon import (
    EndlessDungeonService,
    EndlessRunAlreadyRunningError,
    EndlessRunNotFoundError,
    InvalidEndlessStartFloorError,
)
from application.equipment.equipment_service import EquipmentService
from application.naming import ItemNamingBatchRequest, ItemNamingBatchResult, ItemNamingBatchService, ItemNamingProvider
from infrastructure.config.static import load_static_config
from infrastructure.db.repositories import (
    SqlAlchemyBattleRecordRepository,
    SqlAlchemyCharacterRepository,
    SqlAlchemyEquipmentRepository,
    SqlAlchemyInventoryRepository,
    SqlAlchemyPlayerRepository,
    SqlAlchemySkillRepository,
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



class _DeterministicNamingProvider:
    provider_name = "deterministic_test_provider"

    def __init__(self, *, failures: set[int] | None = None) -> None:
        self.failures = failures or set()
        self.requests: list[ItemNamingBatchRequest] = []

    def generate_names(self, *, request: ItemNamingBatchRequest) -> tuple[ItemNamingBatchResult, ...]:
        self.requests.append(request)
        results: list[ItemNamingBatchResult] = []
        for candidate in request.candidates:
            if candidate.instance_id in self.failures:
                results.append(
                    ItemNamingBatchResult(
                        target_type=candidate.target_type,
                        instance_id=candidate.instance_id,
                        error_message="forced_failure",
                    )
                )
                continue
            results.append(
                ItemNamingBatchResult(
                    target_type=candidate.target_type,
                    instance_id=candidate.instance_id,
                    generated_name=f"AI·{candidate.fallback_name}",
                )
            )
        return tuple(results)



def _build_services(session, static_config, *, naming_provider: ItemNamingProvider | None = None):
    """创建测试所需服务。"""
    player_repository = SqlAlchemyPlayerRepository(session)
    character_repository = SqlAlchemyCharacterRepository(session)
    state_repository = SqlAlchemyStateRepository(session)
    battle_record_repository = SqlAlchemyBattleRecordRepository(session)
    skill_repository = SqlAlchemySkillRepository(session)
    equipment_repository = SqlAlchemyEquipmentRepository(session)
    inventory_repository = SqlAlchemyInventoryRepository(session)
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
    skill_drop_service = SkillDropService(
        character_repository=character_repository,
        skill_repository=skill_repository,
        static_config=static_config,
    )
    equipment_service = EquipmentService(
        character_repository=character_repository,
        equipment_repository=equipment_repository,
        inventory_repository=inventory_repository,
        static_config=static_config,
    )
    naming_batch_service = ItemNamingBatchService(
        state_repository=state_repository,
        equipment_service=equipment_service,
        skill_repository=skill_repository,
        skill_runtime_support=skill_drop_service._skill_runtime_support,
        provider=naming_provider,
        static_config=static_config,
    )
    endless_service = EndlessDungeonService(
        state_repository=state_repository,
        character_repository=character_repository,
        static_config=static_config,
        auto_battle_service=auto_battle_service,
        battle_record_repository=battle_record_repository,
        skill_drop_service=skill_drop_service,
        equipment_service=equipment_service,
        naming_batch_service=naming_batch_service,
    )
    return (
        growth_service,
        endless_service,
        character_repository,
        state_repository,
        battle_record_repository,
        naming_batch_service,
        equipment_service,
        skill_repository,
    )



def _set_character_progress(
    *,
    character_repository: SqlAlchemyCharacterRepository,
    character_id: int,
    realm_id: str,
    stage_id: str,
    current_hp_ratio: str = "1.0000",
    current_mp_ratio: str = "1.0000",
    highest_endless_floor: int | None = None,
) -> None:
    """覆盖测试角色成长状态。"""
    aggregate = character_repository.get_aggregate(character_id)
    assert aggregate is not None
    assert aggregate.progress is not None
    aggregate.progress.realm_id = realm_id
    aggregate.progress.stage_id = stage_id
    aggregate.progress.current_hp_ratio = Decimal(current_hp_ratio)
    aggregate.progress.current_mp_ratio = Decimal(current_mp_ratio)
    if highest_endless_floor is not None:
        aggregate.progress.highest_endless_floor = highest_endless_floor
    character_repository.save_progress(aggregate.progress)



def _set_run_floor(
    *,
    state_repository: SqlAlchemyStateRepository,
    character_id: int,
    floor: int,
    node_type: str,
) -> None:
    """直接把运行态调整到指定楼层，便于覆盖目标分支。"""
    run_state = state_repository.get_endless_run_state(character_id)
    assert run_state is not None
    run_state.current_floor = floor
    run_state.highest_floor_reached = max(run_state.highest_floor_reached, floor)
    run_state.current_node_type = node_type
    state_repository.save_endless_run_state(run_state)



def test_start_run_creates_structured_endless_state(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """开始无尽副本运行后应能读取结构化状态。"""
    database_url = _build_sqlite_url(tmp_path / "endless_start.db")
    _upgrade_database(database_url, monkeypatch)
    session_factory = create_session_factory(database_url)
    static_config = load_static_config()
    start_time = datetime(2026, 3, 26, 12, 0, 0)

    with session_scope(session_factory) as session:
        growth_service, endless_service, _, state_repository, _, _, _, _ = _build_services(session, static_config)
        created = growth_service.create_character(
            discord_user_id="32001",
            player_display_name="远山",
            character_name="沉舟",
        )

        snapshot = endless_service.start_run(
            character_id=created.character_id,
            selected_start_floor=1,
            seed=20260326,
            now=start_time,
        )

        assert snapshot.has_active_run is True
        assert snapshot.status == "running"
        assert snapshot.selected_start_floor == 1
        assert snapshot.current_floor == 1
        assert snapshot.highest_floor_reached == 1
        assert snapshot.current_node_type is not None
        assert snapshot.current_node_type.value == "normal"
        assert snapshot.current_region is not None
        assert snapshot.current_region.region_id == "wind"
        assert snapshot.current_region.region_bias_id == "wind"
        assert snapshot.anchor_status.highest_unlocked_anchor_floor == 0
        assert snapshot.anchor_status.available_start_floors == (1,)
        assert snapshot.anchor_status.selected_start_floor == 1
        assert snapshot.anchor_status.selected_start_floor_unlocked is True
        assert snapshot.anchor_status.current_anchor_floor == 0
        assert snapshot.anchor_status.next_anchor_floor == 10
        assert snapshot.run_seed == 20260326
        assert snapshot.reward_ledger is not None
        assert snapshot.reward_ledger.stable_cultivation == 0
        assert snapshot.reward_ledger.pending_equipment_score == 0
        assert snapshot.reward_ledger.last_reward_floor is None
        assert snapshot.reward_ledger.drop_display == ()
        assert snapshot.reward_ledger.latest_node_result is None
        assert snapshot.reward_ledger.advanced_floor_count == 0
        assert snapshot.reward_ledger.latest_anchor_unlock is None
        assert snapshot.encounter_history == ()
        assert snapshot.started_at == start_time

        persisted = state_repository.get_endless_run_state(created.character_id)
        assert persisted is not None
        assert persisted.selected_start_floor == 1
        assert persisted.current_floor == 1
        assert persisted.current_node_type == "normal"
        assert persisted.run_seed == 20260326
        assert persisted.pending_rewards_json["stable_totals"]["cultivation"] == 0
        assert persisted.pending_rewards_json["drop_display"] == []
        assert persisted.pending_rewards_json["advanced_floor_count"] == 0
        assert persisted.run_snapshot_json["has_active_run"] is True
        assert persisted.run_snapshot_json["selected_start_floor"] == 1
        assert persisted.run_snapshot_json["current_floor"] == 1
        assert persisted.run_snapshot_json["current_region"]["region_id"] == "wind"
        assert persisted.run_snapshot_json["anchor_status"]["available_start_floors"] == [1]
        assert persisted.run_snapshot_json["encounter_history"] == []

        with pytest.raises(EndlessRunAlreadyRunningError):
            endless_service.start_run(character_id=created.character_id, selected_start_floor=1, now=start_time)



def test_resume_run_reads_existing_anchor_based_state(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """恢复运行时应按已解锁锚点返回当前结构化状态。"""
    database_url = _build_sqlite_url(tmp_path / "endless_resume.db")
    _upgrade_database(database_url, monkeypatch)
    session_factory = create_session_factory(database_url)
    static_config = load_static_config()
    start_time = datetime(2026, 3, 26, 13, 30, 0)

    with session_scope(session_factory) as session:
        growth_service, endless_service, character_repository, _, _, _, _, _ = _build_services(session, static_config)
        created = growth_service.create_character(
            discord_user_id="32002",
            player_display_name="临川",
            character_name="照影",
        )

        progress = character_repository.get_aggregate(created.character_id)
        assert progress is not None
        assert progress.progress is not None
        progress.progress.highest_endless_floor = 37
        character_repository.save_progress(progress.progress)

        started = endless_service.start_run(
            character_id=created.character_id,
            selected_start_floor=30,
            seed=77,
            now=start_time,
        )
        resumed = endless_service.resume_run(character_id=created.character_id)
        current = endless_service.get_current_run_state(character_id=created.character_id)

        assert started.current_floor == 31
        assert resumed.has_active_run is True
        assert resumed.selected_start_floor == 30
        assert resumed.current_floor == 31
        assert resumed.highest_floor_reached == 31
        assert resumed.current_region is not None
        assert resumed.current_region.region_id == "flame"
        assert resumed.anchor_status.highest_unlocked_anchor_floor == 30
        assert resumed.anchor_status.available_start_floors == (1, 10, 20, 30)
        assert resumed.anchor_status.selected_start_floor_unlocked is True
        assert resumed.anchor_status.current_anchor_floor == 30
        assert resumed.anchor_status.next_anchor_floor == 40
        assert resumed.run_seed == 77
        assert current == resumed



def test_invalid_start_floor_and_missing_run_are_rejected(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """非法起点与不存在的运行应被拒绝。"""
    database_url = _build_sqlite_url(tmp_path / "endless_validation.db")
    _upgrade_database(database_url, monkeypatch)
    session_factory = create_session_factory(database_url)
    static_config = load_static_config()
    start_time = datetime(2026, 3, 26, 14, 0, 0)

    with session_scope(session_factory) as session:
        growth_service, endless_service, character_repository, _, _, _, _, _ = _build_services(session, static_config)
        created = growth_service.create_character(
            discord_user_id="32003",
            player_display_name="闻溪",
            character_name="玄澜",
        )

        with pytest.raises(InvalidEndlessStartFloorError):
            endless_service.start_run(
                character_id=created.character_id,
                selected_start_floor=10,
                seed=10,
                now=start_time,
            )

        aggregate = character_repository.get_aggregate(created.character_id)
        assert aggregate is not None
        assert aggregate.progress is not None
        aggregate.progress.highest_endless_floor = 40
        character_repository.save_progress(aggregate.progress)

        with pytest.raises(InvalidEndlessStartFloorError):
            endless_service.start_run(
                character_id=created.character_id,
                selected_start_floor=25,
                seed=11,
                now=start_time,
            )

        empty_snapshot = endless_service.get_current_run_state(character_id=created.character_id)
        assert empty_snapshot.has_active_run is False
        assert empty_snapshot.anchor_status.available_start_floors == (1, 10, 20, 30, 40)
        assert empty_snapshot.reward_ledger is None

        with pytest.raises(EndlessRunNotFoundError):
            endless_service.resume_run(character_id=created.character_id)



def test_advance_next_floor_accumulates_process_ledger_without_drop_record_write(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """连续推进多层后应累计过程账本，并只写战报不写掉落记录。"""
    database_url = _build_sqlite_url(tmp_path / "endless_advance_normal.db")
    _upgrade_database(database_url, monkeypatch)
    session_factory = create_session_factory(database_url)
    static_config = load_static_config()
    start_time = datetime(2026, 3, 26, 15, 0, 0)

    with session_scope(session_factory) as session:
        (
            growth_service,
            endless_service,
            character_repository,
            state_repository,
            battle_record_repository,
            _,
            _,
            _,
        ) = _build_services(session, static_config)
        created = growth_service.create_character(
            discord_user_id="32004",
            player_display_name="折霜",
            character_name="归棹",
        )
        _set_character_progress(
            character_repository=character_repository,
            character_id=created.character_id,
            realm_id="core",
            stage_id="middle",
        )
        started = endless_service.start_run(
            character_id=created.character_id,
            selected_start_floor=1,
            seed=20260326,
            now=start_time,
        )

        first_result = endless_service.advance_next_floor(character_id=created.character_id)
        second_result = endless_service.advance_next_floor(character_id=created.character_id)
        current = endless_service.get_current_run_state(character_id=created.character_id)
        aggregate = character_repository.get_aggregate(created.character_id)
        persisted = state_repository.get_endless_run_state(created.character_id)
        reports = battle_record_repository.list_battle_reports(created.character_id)
        drops = battle_record_repository.list_drop_records(created.character_id)

        assert started.current_floor == 1
        assert first_result.cleared_floor == 1
        assert second_result.cleared_floor == 2
        assert first_result.reward_granted is True
        assert second_result.reward_granted is True
        assert first_result.battle_outcome == "ally_victory"
        assert second_result.battle_outcome == "ally_victory"
        assert current.status == "running"
        assert current.current_floor == 3
        assert current.current_node_type is not None
        assert current.current_node_type.value == "normal"
        assert current.reward_ledger is not None
        assert current.reward_ledger.last_reward_floor == 2
        assert current.reward_ledger.advanced_floor_count == 2
        assert current.reward_ledger.stable_cultivation == 22
        assert current.reward_ledger.stable_insight == 2
        assert current.reward_ledger.stable_refining_essence == 16
        assert current.reward_ledger.pending_equipment_score == 0
        assert current.reward_ledger.pending_artifact_score == 0
        assert current.reward_ledger.pending_dao_pattern_score == 0
        assert len(current.reward_ledger.drop_display) == 2
        assert current.reward_ledger.drop_display[0]["floor"] == 1
        assert current.reward_ledger.drop_display[1]["floor"] == 2
        assert len(current.encounter_history) == 2
        assert current.encounter_history[0]["battle_outcome"] == "ally_victory"
        assert current.encounter_history[1]["battle_outcome"] == "ally_victory"
        assert current.reward_ledger.latest_node_result is not None
        assert current.reward_ledger.latest_node_result["floor"] == 2
        assert current.reward_ledger.latest_node_result["reward_granted"] is True
        assert current.reward_ledger.latest_anchor_unlock is not None
        assert current.reward_ledger.latest_anchor_unlock["unlocked"] is False
        assert persisted is not None
        assert persisted.status == "running"
        assert persisted.current_floor == 3
        assert len(persisted.pending_rewards_json["drop_display"]) == 2
        assert len(reports) == 2
        assert len(drops) == 0
        assert aggregate is not None
        assert aggregate.progress is not None
        assert aggregate.progress.highest_endless_floor == 2



def test_advance_next_floor_handles_elite_and_anchor_boss_branches_and_unlocks_anchor(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """精英层与锚点首领层应接入战斗，并在锚点首领获胜后推进锚点解锁。"""
    database_url = _build_sqlite_url(tmp_path / "endless_advance_anchor.db")
    _upgrade_database(database_url, monkeypatch)
    session_factory = create_session_factory(database_url)
    static_config = load_static_config()
    start_time = datetime(2026, 3, 26, 16, 0, 0)

    with session_scope(session_factory) as session:
        (
            growth_service,
            endless_service,
            character_repository,
            state_repository,
            battle_record_repository,
            _,
            _,
            _,
        ) = _build_services(session, static_config)

        elite_character = growth_service.create_character(
            discord_user_id="32005",
            player_display_name="商羽",
            character_name="停云",
        )
        _set_character_progress(
            character_repository=character_repository,
            character_id=elite_character.character_id,
            realm_id="great_vehicle",
            stage_id="perfect",
        )
        endless_service.start_run(
            character_id=elite_character.character_id,
            selected_start_floor=1,
            seed=77,
            now=start_time,
        )
        _set_run_floor(
            state_repository=state_repository,
            character_id=elite_character.character_id,
            floor=5,
            node_type="elite",
        )
        elite_result = endless_service.advance_next_floor(character_id=elite_character.character_id)
        elite_snapshot = endless_service.get_current_run_state(character_id=elite_character.character_id)

        boss_character = growth_service.create_character(
            discord_user_id="32006",
            player_display_name="惊鸿",
            character_name="素问",
        )
        _set_character_progress(
            character_repository=character_repository,
            character_id=boss_character.character_id,
            realm_id="great_vehicle",
            stage_id="perfect",
        )
        endless_service.start_run(
            character_id=boss_character.character_id,
            selected_start_floor=1,
            seed=91,
            now=start_time,
        )
        _set_run_floor(
            state_repository=state_repository,
            character_id=boss_character.character_id,
            floor=10,
            node_type="anchor_boss",
        )
        boss_result = endless_service.advance_next_floor(character_id=boss_character.character_id)
        boss_snapshot = endless_service.get_current_run_state(character_id=boss_character.character_id)
        boss_persisted = state_repository.get_endless_run_state(boss_character.character_id)
        boss_reports = battle_record_repository.list_battle_reports(boss_character.character_id)

        assert elite_result.reward_granted is True
        assert elite_result.encounter["node_type"] == "elite"
        assert elite_result.encounter["enemy_count"] == 2
        assert elite_result.next_floor == 6
        assert elite_snapshot.status == "running"
        assert elite_snapshot.current_floor == 6
        assert elite_snapshot.reward_ledger is not None
        assert elite_snapshot.reward_ledger.advanced_floor_count == 1
        assert elite_snapshot.reward_ledger.pending_equipment_score == 30
        assert elite_snapshot.reward_ledger.pending_dao_pattern_score == 6

        assert boss_result.reward_granted is True
        assert boss_result.encounter["node_type"] == "anchor_boss"
        assert boss_result.encounter["enemy_count"] == 3
        assert boss_result.next_floor == 11
        assert boss_result.anchor_unlock_result is not None
        assert boss_result.anchor_unlock_result["unlocked"] is True
        assert boss_result.anchor_unlock_result["anchor_floor"] == 10
        assert boss_snapshot.status == "running"
        assert boss_snapshot.current_floor == 11
        assert boss_snapshot.anchor_status.highest_unlocked_anchor_floor == 10
        assert boss_snapshot.anchor_status.available_start_floors == (1, 10)
        assert boss_snapshot.reward_ledger is not None
        assert boss_snapshot.reward_ledger.advanced_floor_count == 1
        assert boss_snapshot.reward_ledger.pending_equipment_score == 80
        assert boss_snapshot.reward_ledger.pending_artifact_score == 18
        assert boss_snapshot.reward_ledger.pending_dao_pattern_score == 16
        assert boss_snapshot.reward_ledger.latest_node_result is not None
        assert boss_snapshot.reward_ledger.latest_node_result["floor"] == 10
        assert boss_snapshot.reward_ledger.latest_anchor_unlock is not None
        assert boss_snapshot.reward_ledger.latest_anchor_unlock["anchor_floor"] == 10
        assert boss_persisted is not None
        assert boss_persisted.current_floor == 11
        assert boss_persisted.pending_rewards_json["latest_anchor_unlock"]["anchor_floor"] == 10
        assert len(boss_reports) == 1



def test_advance_next_floor_moves_to_pending_defeat_settlement_on_failure(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """单层战斗失败后应进入待战败结算终态，并保留过程账本与上下文。"""
    database_url = _build_sqlite_url(tmp_path / "endless_advance_failure.db")
    _upgrade_database(database_url, monkeypatch)
    session_factory = create_session_factory(database_url)
    static_config = load_static_config()
    start_time = datetime(2026, 3, 26, 17, 0, 0)

    with session_scope(session_factory) as session:
        (
            growth_service,
            endless_service,
            character_repository,
            state_repository,
            battle_record_repository,
            _,
            _,
            _,
        ) = _build_services(session, static_config)
        created = growth_service.create_character(
            discord_user_id="32007",
            player_display_name="问潮",
            character_name="白砚",
        )
        _set_character_progress(
            character_repository=character_repository,
            character_id=created.character_id,
            realm_id="mortal",
            stage_id="early",
            current_hp_ratio="0.0500",
            current_mp_ratio="0.0000",
        )
        endless_service.start_run(
            character_id=created.character_id,
            selected_start_floor=1,
            seed=1,
            now=start_time,
        )
        _set_run_floor(
            state_repository=state_repository,
            character_id=created.character_id,
            floor=50,
            node_type="anchor_boss",
        )

        result = endless_service.advance_next_floor(character_id=created.character_id)
        current = endless_service.get_current_run_state(character_id=created.character_id)
        persisted = state_repository.get_endless_run_state(created.character_id)
        reports = battle_record_repository.list_battle_reports(created.character_id)
        drops = battle_record_repository.list_drop_records(created.character_id)

        assert result.reward_granted is False
        assert result.battle_outcome == "enemy_victory"
        assert result.next_floor is None
        assert current.status == "pending_defeat_settlement"
        assert current.current_floor == 50
        assert current.reward_ledger is not None
        assert current.reward_ledger.advanced_floor_count == 0
        assert current.reward_ledger.last_reward_floor is None
        assert current.reward_ledger.drop_display == ()
        assert len(current.encounter_history) == 1
        assert current.encounter_history[0]["battle_outcome"] == "enemy_victory"
        assert current.reward_ledger.latest_node_result is not None
        assert current.reward_ledger.latest_node_result["floor"] == 50
        assert current.reward_ledger.latest_node_result["reward_granted"] is False
        assert current.reward_ledger.latest_node_result["battle_outcome"] == "enemy_victory"
        assert persisted is not None
        assert persisted.status == "pending_defeat_settlement"
        assert persisted.current_floor == 50
        assert persisted.last_enemy_template_id is not None
        assert persisted.run_snapshot_json["status"] == "pending_defeat_settlement"
        assert persisted.run_snapshot_json["encounter_history"][0]["battle_outcome"] == "enemy_victory"
        assert len(reports) == 1
        assert len(drops) == 0



def test_retreat_settlement_keeps_full_rewards_and_is_idempotent(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """主动撤离应全额保留收益，只在结算时写入掉落，并允许重复读取。"""
    database_url = _build_sqlite_url(tmp_path / "endless_retreat_settlement.db")
    _upgrade_database(database_url, monkeypatch)
    session_factory = create_session_factory(database_url)
    static_config = load_static_config()
    start_time = datetime(2026, 3, 26, 18, 0, 0)
    settle_time = datetime(2026, 3, 26, 18, 10, 0)

    with session_scope(session_factory) as session:
        (
            growth_service,
            endless_service,
            character_repository,
            state_repository,
            battle_record_repository,
            _,
            _,
            _,
        ) = _build_services(session, static_config)
        created = growth_service.create_character(
            discord_user_id="32008",
            player_display_name="照川",
            character_name="临渊",
        )
        _set_character_progress(
            character_repository=character_repository,
            character_id=created.character_id,
            realm_id="great_vehicle",
            stage_id="perfect",
        )
        endless_service.start_run(
            character_id=created.character_id,
            selected_start_floor=1,
            seed=88,
            now=start_time,
        )
        _set_run_floor(
            state_repository=state_repository,
            character_id=created.character_id,
            floor=5,
            node_type="elite",
        )

        advance_result = endless_service.advance_next_floor(character_id=created.character_id)
        aggregate_before_settlement = character_repository.get_aggregate(created.character_id)
        assert aggregate_before_settlement is not None
        assert aggregate_before_settlement.progress is not None
        aggregate_before_settlement.progress.highest_endless_floor = 0
        character_repository.save_progress(aggregate_before_settlement.progress)
        assert battle_record_repository.list_drop_records(created.character_id) == []

        settlement = endless_service.settle_retreat(character_id=created.character_id, now=settle_time)
        repeated = endless_service.settle_retreat(
            character_id=created.character_id,
            now=datetime(2026, 3, 26, 18, 11, 0),
        )
        panel = endless_service.get_settlement_result(character_id=created.character_id)
        current = endless_service.get_current_run_state(character_id=created.character_id)
        aggregate = character_repository.get_aggregate(created.character_id)
        persisted = state_repository.get_endless_run_state(created.character_id)
        drops = battle_record_repository.list_drop_records(created.character_id)

        assert advance_result.reward_granted is True
        assert settlement == repeated
        assert settlement == panel
        assert settlement.settlement_type == "retreat"
        assert settlement.terminated_floor == 5
        assert settlement.current_region.region_id == "wind"
        assert settlement.stable_rewards.original == {
            "cultivation": 6300,
            "insight": 13,
            "refining_essence": 14,
        }
        assert settlement.stable_rewards.deducted == {
            "cultivation": 0,
            "insight": 0,
            "refining_essence": 0,
        }
        assert settlement.stable_rewards.settled == settlement.stable_rewards.original
        assert settlement.pending_rewards.original == {
            "equipment_score": 30,
            "artifact_score": 0,
            "dao_pattern_score": 6,
        }
        assert settlement.pending_rewards.deducted == {
            "equipment_score": 0,
            "artifact_score": 0,
            "dao_pattern_score": 0,
        }
        assert settlement.pending_rewards.settled == settlement.pending_rewards.original
        assert settlement.accounting_completed is True
        assert settlement.can_repeat_read is True
        assert settlement.settled_at == settle_time
        assert len(settlement.final_drop_list) == 4
        assert settlement.final_drop_list[0]["entry_type"] == "stable_reward_bundle"
        assert settlement.final_drop_list[0]["original"]["cultivation"] == 6300
        assert settlement.final_drop_list[1]["entry_type"] == "pending_reward_bundle"
        assert settlement.final_drop_list[1]["settled"]["equipment_score"] == 30
        assert settlement.final_drop_list[2]["entry_type"] == "equipment_drop"
        assert settlement.final_drop_list[2]["item_id"] > 0
        assert settlement.final_drop_list[2]["display_name"] != ""
        assert settlement.final_drop_list[2]["is_artifact"] is False
        assert settlement.final_drop_list[2]["source_score"] == 30
        assert settlement.final_drop_list[3]["entry_type"] == "skill_drop"
        assert settlement.final_drop_list[3]["source_type"] == "endless_skill_drop"
        assert settlement.final_drop_list[3]["source_record_id"] == "endless:retreat:floor_5"
        assert settlement.final_drop_list[3]["skill_name"] != ""
        assert current.has_active_run is False
        assert persisted is not None
        assert persisted.status == "completed"
        assert persisted.pending_rewards_json["advanced_floor_count"] == 0
        assert persisted.pending_rewards_json["drop_display"] == []
        assert persisted.run_snapshot_json["settlement_result"]["settlement_type"] == "retreat"
        assert persisted.run_snapshot_json["settlement_result"]["can_repeat_read"] is True
        assert aggregate is not None
        assert aggregate.progress is not None
        assert aggregate.progress.cultivation_value == 6300
        assert aggregate.progress.comprehension_value == 13
        assert aggregate.progress.highest_endless_floor == 5
        assert len(drops) == 1
        assert drops[0].source_ref == "endless:retreat:floor_5"
        assert drops[0].currencies_json == {
            "cultivation": 6300,
            "insight": 13,
            "refining_essence": 14,
        }
        assert len(drops[0].items_json) == 4
        assert drops[0].items_json[1]["settled"]["equipment_score"] == 30
        assert drops[0].items_json[2]["entry_type"] == "equipment_drop"
        assert drops[0].items_json[2]["item_id"] == settlement.final_drop_list[2]["item_id"]
        assert drops[0].items_json[3]["entry_type"] == "skill_drop"
        assert drops[0].items_json[3]["source_record_id"] == "endless:retreat:floor_5"

        with pytest.raises(EndlessRunNotFoundError):
            endless_service.advance_next_floor(character_id=created.character_id)



def test_defeat_settlement_deducts_pending_rewards_and_prevents_double_accounting(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """战败结算应折损未稳收益，并且重复结算不得重复入账。"""
    database_url = _build_sqlite_url(tmp_path / "endless_defeat_settlement.db")
    _upgrade_database(database_url, monkeypatch)
    session_factory = create_session_factory(database_url)
    static_config = load_static_config()
    start_time = datetime(2026, 3, 26, 19, 0, 0)
    settle_time = datetime(2026, 3, 26, 19, 10, 0)

    with session_scope(session_factory) as session:
        (
            growth_service,
            endless_service,
            character_repository,
            state_repository,
            battle_record_repository,
            _,
            _,
            _,
        ) = _build_services(session, static_config)
        created = growth_service.create_character(
            discord_user_id="32009",
            player_display_name="长汀",
            character_name="怀霜",
        )
        _set_character_progress(
            character_repository=character_repository,
            character_id=created.character_id,
            realm_id="great_vehicle",
            stage_id="perfect",
        )
        endless_service.start_run(
            character_id=created.character_id,
            selected_start_floor=1,
            seed=99,
            now=start_time,
        )
        _set_run_floor(
            state_repository=state_repository,
            character_id=created.character_id,
            floor=5,
            node_type="elite",
        )
        first_advance = endless_service.advance_next_floor(character_id=created.character_id)
        _set_character_progress(
            character_repository=character_repository,
            character_id=created.character_id,
            realm_id="mortal",
            stage_id="early",
            current_hp_ratio="0.0500",
            current_mp_ratio="0.0000",
            highest_endless_floor=5,
        )
        _set_run_floor(
            state_repository=state_repository,
            character_id=created.character_id,
            floor=50,
            node_type="anchor_boss",
        )

        failed = endless_service.advance_next_floor(character_id=created.character_id)
        assert battle_record_repository.list_drop_records(created.character_id) == []

        settlement = endless_service.settle_defeat(character_id=created.character_id, now=settle_time)
        repeated = endless_service.settle_defeat(
            character_id=created.character_id,
            now=datetime(2026, 3, 26, 19, 11, 0),
        )
        panel = endless_service.get_settlement_result(character_id=created.character_id)
        current = endless_service.get_current_run_state(character_id=created.character_id)
        aggregate = character_repository.get_aggregate(created.character_id)
        persisted = state_repository.get_endless_run_state(created.character_id)
        drops = battle_record_repository.list_drop_records(created.character_id)
        reports = battle_record_repository.list_battle_reports(created.character_id)

        assert first_advance.reward_granted is True
        assert failed.reward_granted is False
        assert failed.battle_outcome == "enemy_victory"
        assert settlement == repeated
        assert settlement == panel
        assert settlement.settlement_type == "defeat"
        assert settlement.terminated_floor == 50
        assert settlement.current_region.region_id == "frost"
        assert settlement.stable_rewards.original == {
            "cultivation": 6300,
            "insight": 13,
            "refining_essence": 14,
        }
        assert settlement.stable_rewards.deducted == {
            "cultivation": 0,
            "insight": 0,
            "refining_essence": 0,
        }
        assert settlement.stable_rewards.settled == {
            "cultivation": 50,
            "insight": 13,
            "refining_essence": 14,
        }
        assert settlement.pending_rewards.original == {
            "equipment_score": 30,
            "artifact_score": 0,
            "dao_pattern_score": 6,
        }
        assert settlement.pending_rewards.deducted == {
            "equipment_score": 30,
            "artifact_score": 0,
            "dao_pattern_score": 6,
        }
        assert settlement.pending_rewards.settled == {
            "equipment_score": 0,
            "artifact_score": 0,
            "dao_pattern_score": 0,
        }
        assert settlement.accounting_completed is True
        assert settlement.can_repeat_read is True
        assert settlement.settled_at == settle_time
        assert len(settlement.final_drop_list) == 3
        assert settlement.final_drop_list[0]["settled"]["cultivation"] == 50
        assert settlement.final_drop_list[1]["settled"]["equipment_score"] == 0
        assert settlement.final_drop_list[2]["entry_type"] == "skill_drop"
        assert settlement.final_drop_list[2]["source_type"] == "endless_skill_drop"
        assert settlement.final_drop_list[2]["source_record_id"] == "endless:defeat:floor_50"
        assert settlement.final_drop_list[2]["skill_name"] != ""
        assert settlement.final_drop_list[2]["skill_type"] in {"main", "auxiliary"}
        if settlement.final_drop_list[2]["skill_type"] == "main":
            assert settlement.final_drop_list[2]["auxiliary_slot_id"] is None
        else:
            assert settlement.final_drop_list[2]["auxiliary_slot_id"] in {"guard", "movement", "spirit"}
        assert current.has_active_run is False
        assert persisted is not None
        assert persisted.status == "completed"
        assert persisted.run_snapshot_json["settlement_result"]["settlement_type"] == "defeat"
        assert aggregate is not None
        assert aggregate.progress is not None
        assert aggregate.progress.cultivation_value == 50
        assert aggregate.progress.comprehension_value == 13
        assert aggregate.progress.highest_endless_floor == 5
        assert len(drops) == 1
        assert len(reports) == 2
        assert drops[0].source_ref == "endless:defeat:floor_50"
        assert drops[0].currencies_json == {
            "cultivation": 50,
            "insight": 13,
            "refining_essence": 14,
        }
        assert drops[0].items_json[1]["deducted"]["equipment_score"] == 30
        assert drops[0].items_json[1]["settled"]["dao_pattern_score"] == 0
        assert drops[0].items_json[2]["entry_type"] == "skill_drop"
        assert drops[0].items_json[2]["source_record_id"] == "endless:defeat:floor_50"
        assert drops[0].items_json[2]["skill_type"] in {"main", "auxiliary"}

        with pytest.raises(EndlessRunNotFoundError):
            endless_service.advance_next_floor(character_id=created.character_id)



def test_repeated_settlement_read_does_not_duplicate_equipment_instances(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """同一结算重复读取时不得重复生成装备或法宝实例。"""
    database_url = _build_sqlite_url(tmp_path / "endless_settlement_repeat_read.db")
    _upgrade_database(database_url, monkeypatch)
    session_factory = create_session_factory(database_url)
    static_config = load_static_config()
    start_time = datetime(2026, 3, 26, 20, 0, 0)
    settle_time = datetime(2026, 3, 26, 20, 10, 0)

    with session_scope(session_factory) as session:
        (
            growth_service,
            endless_service,
            character_repository,
            state_repository,
            battle_record_repository,
            _,
            _,
            _,
        ) = _build_services(session, static_config)
        created = growth_service.create_character(
            discord_user_id="32010",
            player_display_name="渡川",
            character_name="青岚",
        )
        _set_character_progress(
            character_repository=character_repository,
            character_id=created.character_id,
            realm_id="great_vehicle",
            stage_id="perfect",
        )
        endless_service.start_run(
            character_id=created.character_id,
            selected_start_floor=1,
            seed=123,
            now=start_time,
        )
        _set_run_floor(
            state_repository=state_repository,
            character_id=created.character_id,
            floor=10,
            node_type="anchor_boss",
        )

        advance_result = endless_service.advance_next_floor(character_id=created.character_id)
        assert advance_result.reward_granted is True
        settlement = endless_service.settle_retreat(character_id=created.character_id, now=settle_time)
        aggregate_after_settlement = character_repository.get_aggregate(created.character_id)
        assert aggregate_after_settlement is not None
        initial_equipment_ids = tuple(item.id for item in aggregate_after_settlement.equipment_items)
        repeated = endless_service.settle_retreat(
            character_id=created.character_id,
            now=datetime(2026, 3, 26, 20, 11, 0),
        )
        panel = endless_service.get_settlement_result(character_id=created.character_id)
        aggregate_after_repeated = character_repository.get_aggregate(created.character_id)
        drops = battle_record_repository.list_drop_records(created.character_id)

        assert settlement == repeated
        assert settlement == panel
        assert aggregate_after_repeated is not None
        assert tuple(item.id for item in aggregate_after_repeated.equipment_items) == initial_equipment_ids
        assert len(initial_equipment_ids) == 2
        assert len(drops) == 1
        equipment_entries = [entry for entry in settlement.final_drop_list if entry["entry_type"] == "equipment_drop"]
        artifact_entries = [entry for entry in settlement.final_drop_list if entry["entry_type"] == "artifact_drop"]
        assert len(equipment_entries) == 1
        assert len(artifact_entries) == 1
        assert equipment_entries[0]["item_id"] != artifact_entries[0]["item_id"]



def test_endless_settlement_skips_batch_when_provider_missing_and_keeps_fallback_names(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """无 AI 提供方时应跳过批次处理且不阻塞结算。"""
    database_url = _build_sqlite_url(tmp_path / "endless_naming_skip.db")
    _upgrade_database(database_url, monkeypatch)
    session_factory = create_session_factory(database_url)
    static_config = load_static_config()
    start_time = datetime(2026, 3, 26, 20, 30, 0)
    settle_time = datetime(2026, 3, 26, 20, 40, 0)

    with session_scope(session_factory) as session:
        (
            growth_service,
            endless_service,
            character_repository,
            state_repository,
            _,
            naming_batch_service,
            _,
            skill_repository,
        ) = _build_services(session, static_config)
        created = growth_service.create_character(
            discord_user_id="32011",
            player_display_name="平沙",
            character_name="怀光",
        )
        _set_character_progress(
            character_repository=character_repository,
            character_id=created.character_id,
            realm_id="great_vehicle",
            stage_id="perfect",
        )
        endless_service.start_run(
            character_id=created.character_id,
            selected_start_floor=1,
            seed=123,
            now=start_time,
        )
        _set_run_floor(
            state_repository=state_repository,
            character_id=created.character_id,
            floor=10,
            node_type="anchor_boss",
        )

        advance = endless_service.advance_next_floor(character_id=created.character_id)
        settlement = endless_service.settle_retreat(character_id=created.character_id, now=settle_time)
        batch = naming_batch_service._state_repository.get_item_naming_batch_by_source(
            character_id=created.character_id,
            source_type="endless_settlement",
            source_ref="endless:retreat:floor_10",
        )
        assert advance.reward_granted is True
        assert batch is not None
        assert batch.status == "skipped"
        assert batch.result_payload_json["skipped_reason"] == "provider_unavailable"
        equipment_entries = [entry for entry in settlement.final_drop_list if entry.get("entry_type") == "equipment_drop"]
        artifact_entries = [entry for entry in settlement.final_drop_list if entry.get("entry_type") == "artifact_drop"]
        skill_entries = [entry for entry in settlement.final_drop_list if entry.get("entry_type") == "skill_drop"]
        if equipment_entries:
            equipment_item = naming_batch_service._equipment_service.get_equipment_detail(
                character_id=created.character_id,
                equipment_item_id=equipment_entries[0]["item_id"],
            )
            assert equipment_entries[0]["display_name"] == equipment_item.display_name
        if artifact_entries:
            artifact_item = naming_batch_service._equipment_service.get_equipment_detail(
                character_id=created.character_id,
                equipment_item_id=artifact_entries[0]["item_id"],
            )
            assert artifact_entries[0]["display_name"] == artifact_item.display_name
        assert skill_entries
        skill_item = skill_repository.get_skill_item(skill_entries[0]["item_id"])
        assert skill_item is not None
        assert skill_item.naming_source == "lineage_static"
        assert skill_item.naming_metadata_json["lineage_id"] == skill_item.lineage_id
        assert skill_entries[0]["skill_name"] == skill_item.skill_name



def test_endless_settlement_processes_single_batch_and_partial_failures_keep_fallbacks(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """有 AI 提供方时应按单批次处理，多实例部分失败不影响其他实例。"""
    database_url = _build_sqlite_url(tmp_path / "endless_naming_batch.db")
    _upgrade_database(database_url, monkeypatch)
    session_factory = create_session_factory(database_url)
    static_config = load_static_config()
    provider = _DeterministicNamingProvider()
    start_time = datetime(2026, 3, 26, 21, 0, 0)
    settle_time = datetime(2026, 3, 26, 21, 10, 0)

    with session_scope(session_factory) as session:
        (
            growth_service,
            endless_service,
            character_repository,
            state_repository,
            _,
            naming_batch_service,
            equipment_service,
            skill_repository,
        ) = _build_services(session, static_config, naming_provider=provider)
        created = growth_service.create_character(
            discord_user_id="32012",
            player_display_name="孤舟",
            character_name="沉霄",
        )
        _set_character_progress(
            character_repository=character_repository,
            character_id=created.character_id,
            realm_id="great_vehicle",
            stage_id="perfect",
        )
        endless_service.start_run(
            character_id=created.character_id,
            selected_start_floor=1,
            seed=123,
            now=start_time,
        )
        _set_run_floor(
            state_repository=state_repository,
            character_id=created.character_id,
            floor=10,
            node_type="anchor_boss",
        )

        advance = endless_service.advance_next_floor(character_id=created.character_id)
        assert advance.reward_granted is True
        settlement = endless_service.settle_retreat(character_id=created.character_id, now=settle_time)
        batch = naming_batch_service._state_repository.get_item_naming_batch_by_source(
            character_id=created.character_id,
            source_type="endless_settlement",
            source_ref="endless:retreat:floor_10",
        )
        assert batch is not None
        assert batch.status == "pending"

        refreshed_before = endless_service.get_settlement_result(character_id=created.character_id)
        batch_candidate_ids = [int(payload["instance_id"]) for payload in batch.request_payload_json]
        assert len(batch_candidate_ids) >= 2
        failure_ids = {batch_candidate_ids[0]}
        provider.failures = failure_ids
        processed = naming_batch_service.process_batch(batch_id=batch.id)

        assert len(provider.requests) == 1
        assert provider.requests[0].source_ref == "endless:retreat:floor_10"
        assert len(provider.requests[0].candidates) == len(batch_candidate_ids)
        assert processed.status == "completed"
        assert processed.result_payload_json["provider_name"] == provider.provider_name
        assert len(processed.result_payload_json["failed"]) == 1
        assert len(processed.result_payload_json["renamed"]) >= 1

        refreshed_after = endless_service.get_settlement_result(character_id=created.character_id)
        renamed_entries = [
            entry
            for entry in refreshed_after.final_drop_list
            if int(entry.get("item_id") or 0) in batch_candidate_ids
        ]
        assert renamed_entries
        for entry in renamed_entries:
            item_id = int(entry.get("item_id") or 0)
            entry_type = entry.get("entry_type")
            if item_id in failure_ids:
                original_entry = next(
                    candidate
                    for candidate in refreshed_before.final_drop_list
                    if int(candidate.get("item_id") or 0) == item_id
                )
                if entry_type == "skill_drop":
                    assert entry["skill_name"] == original_entry["skill_name"]
                    skill_item = skill_repository.get_skill_item(item_id)
                    assert skill_item is not None
                    assert skill_item.naming_source == "lineage_static"
                else:
                    assert entry["display_name"] == original_entry["display_name"]
            else:
                if entry_type == "skill_drop":
                    assert str(entry["skill_name"]).startswith("AI·")
                    skill_item = skill_repository.get_skill_item(item_id)
                    assert skill_item is not None
                    assert skill_item.naming_source == "ai_batch"
                    assert skill_item.naming_metadata_json["batch_id"] == str(batch.id)
                else:
                    assert str(entry["display_name"]).startswith("AI·")
                    equipment_item = equipment_service.get_equipment_detail(
                        character_id=created.character_id,
                        equipment_item_id=item_id,
                    )
                    assert equipment_item.display_name == entry["display_name"]
                    assert equipment_item.naming is not None
                    assert equipment_item.naming.naming_source == "ai_batch"
