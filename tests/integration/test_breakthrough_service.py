"""阶段 7 突破秘境应用服务集成测试。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import select
import pytest

from application.breakthrough import BreakthroughRewardService, BreakthroughTrialService
from application.character import CharacterGrowthService
from application.character.current_attribute_service import CurrentAttributeService
from domain.battle import BattleOutcome
from infrastructure.config.static import load_static_config
from infrastructure.db.models import EquipmentItem
from infrastructure.db.repositories import (
    SqlAlchemyBattleRecordRepository,
    SqlAlchemyBreakthroughRepository,
    SqlAlchemyBreakthroughRewardLedgerRepository,
    SqlAlchemyCharacterRepository,
    SqlAlchemyInventoryRepository,
    SqlAlchemyPlayerRepository,
    SqlAlchemyStateRepository,
)
from infrastructure.db.session import create_session_factory, session_scope

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True, slots=True)
class _StubDomainResult:
    """模拟自动战斗返回的最小领域结果。"""

    outcome: BattleOutcome


@dataclass(frozen=True, slots=True)
class _StubProgressWriteback:
    """模拟自动战斗对角色血蓝比的回写载荷。"""

    current_hp_ratio: Decimal
    current_mp_ratio: Decimal


@dataclass(frozen=True, slots=True)
class _StubPersistenceMapping:
    """模拟突破服务实际读取的持久化映射。"""

    progress_writeback: _StubProgressWriteback


@dataclass(frozen=True, slots=True)
class _StubAutoBattleExecutionResult:
    """模拟突破服务所需的自动战斗返回结构。"""

    domain_result: _StubDomainResult
    persistence_mapping: _StubPersistenceMapping
    persisted_battle_report_id: int | None = None


class _StubAutoBattleService:
    """用固定胜负结果替代真实自动战斗，聚焦阶段 7 编排与结算。"""

    def __init__(
        self,
        *,
        outcome: BattleOutcome,
        current_hp_ratio: str = "0.8125",
        current_mp_ratio: str = "0.4375",
        battle_report_id: int | None = None,
    ) -> None:
        self._outcome = outcome
        self._current_hp_ratio = Decimal(current_hp_ratio)
        self._current_mp_ratio = Decimal(current_mp_ratio)
        self._battle_report_id = battle_report_id
        self.last_request = None

    def execute(self, *, request, persist: bool = True) -> _StubAutoBattleExecutionResult:
        """返回固定结果，并保留最近一次请求以便必要时检查。"""
        self.last_request = request
        return _StubAutoBattleExecutionResult(
            domain_result=_StubDomainResult(outcome=self._outcome),
            persistence_mapping=_StubPersistenceMapping(
                progress_writeback=_StubProgressWriteback(
                    current_hp_ratio=self._current_hp_ratio,
                    current_mp_ratio=self._current_mp_ratio,
                )
            ),
            persisted_battle_report_id=self._battle_report_id if persist else None,
        )



def _build_sqlite_url(database_path: Path) -> str:
    """构造测试用 SQLite 地址。"""
    return f"sqlite+pysqlite:///{database_path.as_posix()}"



def _upgrade_database(database_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """将测试数据库升级到最新迁移版本。"""
    monkeypatch.chdir(PROJECT_ROOT)
    monkeypatch.setenv("DATABASE_URL", database_url)
    command.upgrade(Config((PROJECT_ROOT / "alembic.ini").as_posix()), "head")



def _build_services(session, *, static_config, auto_battle_service: _StubAutoBattleService):
    """创建突破秘境集成测试所需的真实仓储与应用服务。"""
    player_repository = SqlAlchemyPlayerRepository(session)
    character_repository = SqlAlchemyCharacterRepository(session)
    breakthrough_repository = SqlAlchemyBreakthroughRepository(session)
    reward_ledger_repository = SqlAlchemyBreakthroughRewardLedgerRepository(session)
    inventory_repository = SqlAlchemyInventoryRepository(session)
    battle_record_repository = SqlAlchemyBattleRecordRepository(session)
    state_repository = SqlAlchemyStateRepository(session)

    growth_service = CharacterGrowthService(
        player_repository=player_repository,
        character_repository=character_repository,
        static_config=static_config,
    )
    current_attribute_service = CurrentAttributeService(
        character_repository=character_repository,
        static_config=static_config,
    )
    reward_service = BreakthroughRewardService(
        character_repository=character_repository,
        breakthrough_repository=breakthrough_repository,
        reward_ledger_repository=reward_ledger_repository,
        inventory_repository=inventory_repository,
        battle_record_repository=battle_record_repository,
        static_config=static_config,
    )
    trial_service = BreakthroughTrialService(
        state_repository=state_repository,
        character_repository=character_repository,
        breakthrough_repository=breakthrough_repository,
        auto_battle_service=auto_battle_service,
        reward_service=reward_service,
        current_attribute_service=current_attribute_service,
        static_config=static_config,
    )
    return {
        "growth_service": growth_service,
        "trial_service": trial_service,
        "character_repository": character_repository,
        "breakthrough_repository": breakthrough_repository,
        "inventory_repository": inventory_repository,
        "battle_record_repository": battle_record_repository,
    }



def _set_character_progress(
    *,
    character_repository: SqlAlchemyCharacterRepository,
    character_id: int,
    realm_id: str,
    stage_id: str,
    qualification_obtained: bool = False,
    current_hp_ratio: str = "1.0000",
    current_mp_ratio: str = "1.0000",
) -> None:
    """覆盖测试角色成长状态，使其落到目标突破上下文。"""
    aggregate = character_repository.get_aggregate(character_id)
    assert aggregate is not None
    assert aggregate.progress is not None
    aggregate.progress.realm_id = realm_id
    aggregate.progress.stage_id = stage_id
    aggregate.progress.breakthrough_qualification_obtained = qualification_obtained
    aggregate.progress.current_hp_ratio = Decimal(current_hp_ratio)
    aggregate.progress.current_mp_ratio = Decimal(current_mp_ratio)
    character_repository.save_progress(aggregate.progress)



def _seed_cleared_progress(
    *,
    breakthrough_repository: SqlAlchemyBreakthroughRepository,
    character_id: int,
    mapping_id: str,
    group_id: str,
    attempt_count: int = 1,
    cleared_count: int = 1,
) -> None:
    """预置已首通关卡，用于覆盖重复挑战与失败分支。"""
    progress = breakthrough_repository.get_or_create_progress(
        character_id,
        mapping_id,
        group_id=group_id,
    )
    progress.status = "cleared"
    progress.attempt_count = attempt_count
    progress.cleared_count = cleared_count
    breakthrough_repository.save_progress(progress)



def test_breakthrough_trial_first_clear_grants_qualification(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """首通当前突破关卡后，应获得突破资格并写入 cleared 进度。"""
    database_url = _build_sqlite_url(tmp_path / "breakthrough_first_clear.db")
    _upgrade_database(database_url, monkeypatch)
    session_factory = create_session_factory(database_url)
    static_config = load_static_config()

    with session_scope(session_factory) as session:
        services = _build_services(
            session,
            static_config=static_config,
            auto_battle_service=_StubAutoBattleService(outcome=BattleOutcome.ALLY_VICTORY),
        )
        created = services["growth_service"].create_character(
            discord_user_id="71001",
            player_display_name="问道者",
            character_name="玄衡",
        )
        _set_character_progress(
            character_repository=services["character_repository"],
            character_id=created.character_id,
            realm_id="foundation",
            stage_id="perfect",
            qualification_obtained=False,
        )

        result = services["trial_service"].challenge_trial(
            character_id=created.character_id,
            mapping_id="foundation_to_core",
            seed=7,
            now=datetime(2026, 3, 26, 21, 0, 0),
            persist_battle_report=False,
        )

        aggregate = services["character_repository"].get_aggregate(created.character_id)
        progress_entry = services["breakthrough_repository"].get_progress(created.character_id, "foundation_to_core")
        drop_records = services["battle_record_repository"].list_drop_records(created.character_id)

        assert aggregate is not None
        assert aggregate.progress is not None
        assert progress_entry is not None
        assert result.settlement.settlement_type == "first_clear"
        assert result.settlement.victory is True
        assert result.settlement.qualification_granted is True
        assert result.qualification_obtained is True
        assert aggregate.progress.breakthrough_qualification_obtained is True
        assert progress_entry.status == "cleared"
        assert progress_entry.cleared_count == 1
        assert progress_entry.qualification_granted_at == datetime(2026, 3, 26, 21, 0, 0)
        assert result.settlement.reward_payload["items"] == [{"reward_kind": "qualification"}]
        assert result.current_hp_ratio == "0.8125"
        assert result.current_mp_ratio == "0.4375"
        assert len(drop_records) == 1
        assert drop_records[0].items_json == []
        assert drop_records[0].currencies_json == {}



def test_breakthrough_trial_repeat_clear_only_grants_bound_main_resources_and_no_endgame_drop(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """重复挑战应只发绑定主资源，不生成装备或其他终局掉落实体。"""
    database_url = _build_sqlite_url(tmp_path / "breakthrough_repeat_clear.db")
    _upgrade_database(database_url, monkeypatch)
    session_factory = create_session_factory(database_url)
    static_config = load_static_config()

    with session_scope(session_factory) as session:
        services = _build_services(
            session,
            static_config=static_config,
            auto_battle_service=_StubAutoBattleService(outcome=BattleOutcome.ALLY_VICTORY),
        )
        created = services["growth_service"].create_character(
            discord_user_id="71002",
            player_display_name="回刷者",
            character_name="照玄",
        )
        _set_character_progress(
            character_repository=services["character_repository"],
            character_id=created.character_id,
            realm_id="core",
            stage_id="perfect",
            qualification_obtained=False,
        )
        _seed_cleared_progress(
            breakthrough_repository=services["breakthrough_repository"],
            character_id=created.character_id,
            mapping_id="foundation_to_core",
            group_id="entry_trials",
        )

        result = services["trial_service"].challenge_trial(
            character_id=created.character_id,
            mapping_id="foundation_to_core",
            seed=8,
            now=datetime(2026, 3, 26, 21, 5, 0),
            persist_battle_report=False,
        )

        progress_entry = services["breakthrough_repository"].get_progress(created.character_id, "foundation_to_core")
        drop_records = services["battle_record_repository"].list_drop_records(created.character_id)
        wash_dust = services["inventory_repository"].get_item(created.character_id, "material", "wash_dust")
        spirit_sand = services["inventory_repository"].get_item(created.character_id, "material", "spirit_sand")
        equipment_rows = session.scalars(select(EquipmentItem)).all()

        assert progress_entry is not None
        assert result.settlement.settlement_type == "repeat_clear"
        assert result.settlement.qualification_granted is False
        assert result.qualification_obtained is False
        assert result.settlement.currency_changes == {}
        assert result.settlement.item_changes == (
            {
                "reward_kind": "material",
                "item_type": "material",
                "item_id": "wash_dust",
                "quantity": 3,
                "total_quantity": 3,
                "bound": True,
            },
            {
                "reward_kind": "material",
                "item_type": "material",
                "item_id": "spirit_sand",
                "quantity": 6,
                "total_quantity": 6,
                "bound": True,
            },
        )
        assert progress_entry.attempt_count == 2
        assert progress_entry.cleared_count == 2
        assert progress_entry.last_reward_direction == "reforge_material"
        assert wash_dust is not None and wash_dust.quantity == 3 and wash_dust.item_payload_json["bound"] is True
        assert spirit_sand is not None and spirit_sand.quantity == 6 and spirit_sand.item_payload_json["bound"] is True
        assert len(drop_records) == 1
        assert drop_records[0].items_json == [
            {
                "reward_kind": "material",
                "item_type": "material",
                "item_id": "wash_dust",
                "quantity": 3,
                "bound": True,
            },
            {
                "reward_kind": "material",
                "item_type": "material",
                "item_id": "spirit_sand",
                "quantity": 6,
                "bound": True,
            },
        ]
        assert drop_records[0].currencies_json == {}
        assert equipment_rows == []



def test_breakthrough_trial_defeat_grants_no_qualification_and_no_repeat_reward(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """失败不会发资格，也不会发放重复挑战资源。"""
    database_url = _build_sqlite_url(tmp_path / "breakthrough_defeat.db")
    _upgrade_database(database_url, monkeypatch)
    session_factory = create_session_factory(database_url)
    static_config = load_static_config()

    with session_scope(session_factory) as session:
        services = _build_services(
            session,
            static_config=static_config,
            auto_battle_service=_StubAutoBattleService(outcome=BattleOutcome.ENEMY_VICTORY),
        )
        created = services["growth_service"].create_character(
            discord_user_id="71003",
            player_display_name="折戟者",
            character_name="临渊",
        )
        _set_character_progress(
            character_repository=services["character_repository"],
            character_id=created.character_id,
            realm_id="core",
            stage_id="perfect",
            qualification_obtained=False,
        )
        _seed_cleared_progress(
            breakthrough_repository=services["breakthrough_repository"],
            character_id=created.character_id,
            mapping_id="foundation_to_core",
            group_id="entry_trials",
        )

        result = services["trial_service"].challenge_trial(
            character_id=created.character_id,
            mapping_id="foundation_to_core",
            seed=9,
            now=datetime(2026, 3, 26, 21, 10, 0),
            persist_battle_report=False,
        )

        aggregate = services["character_repository"].get_aggregate(created.character_id)
        progress_entry = services["breakthrough_repository"].get_progress(created.character_id, "foundation_to_core")
        drop_records = services["battle_record_repository"].list_drop_records(created.character_id)
        inventory_items = services["inventory_repository"].list_by_character_id(created.character_id)

        assert aggregate is not None
        assert aggregate.progress is not None
        assert progress_entry is not None
        assert result.battle_outcome == BattleOutcome.ENEMY_VICTORY.value
        assert result.settlement.settlement_type == "defeat"
        assert result.settlement.victory is False
        assert result.settlement.qualification_granted is False
        assert result.qualification_obtained is False
        assert aggregate.progress.breakthrough_qualification_obtained is False
        assert result.settlement.currency_changes == {}
        assert result.settlement.item_changes == ()
        assert progress_entry.status == "cleared"
        assert progress_entry.attempt_count == 2
        assert progress_entry.cleared_count == 1
        assert progress_entry.last_result_json["settlement_type"] == "defeat"
        assert inventory_items == []
        assert len(drop_records) == 1
        assert drop_records[0].items_json == []
        assert drop_records[0].currencies_json == {}
