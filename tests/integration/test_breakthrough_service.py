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

from application.breakthrough import (
    BreakthroughDynamicDifficultyService,
    BreakthroughMaterialTrialService,
    BreakthroughRewardService,
    BreakthroughTrialService,
)
from application.character import CharacterGrowthService, CharacterProgressionService
from application.character.current_attribute_service import CurrentAttributeService
from domain.battle import BattleOutcome
from infrastructure.config.static import load_static_config
from infrastructure.db.models import EquipmentItem, InventoryItem
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
class _StubSummaryArtifacts:
    """模拟战报摘要载荷。"""

    def to_payload(self) -> dict[str, object]:
        return {
            "completed_rounds": 3,
            "focus_unit_name": "玄衡",
            "final_hp_ratio": "0.8125",
        }


@dataclass(frozen=True, slots=True)
class _StubDetailArtifacts:
    """模拟战报明细载荷。"""

    def to_payload(self) -> dict[str, object]:
        return {
            "units": [
                {"unit_id": "character:1", "unit_name": "玄衡", "side": "ally"},
                {"unit_id": "enemy:1", "unit_name": "守境灵影", "side": "enemy"},
            ],
            "actions": [
                {"action_id": "slash", "action_name": "斩击"},
            ],
            "event_sequence": [
                {
                    "round_index": 1,
                    "sequence": 1,
                    "event_type": "action_started",
                    "actor_unit_id": "character:1",
                    "action_id": "slash",
                },
                {
                    "round_index": 1,
                    "sequence": 2,
                    "event_type": "damage_resolved",
                    "actor_unit_id": "character:1",
                    "target_unit_id": "enemy:1",
                    "action_id": "slash",
                    "detail": {"final_damage": 120},
                },
            ],
        }


@dataclass(frozen=True, slots=True)
class _StubReportArtifacts:
    """模拟自动战斗的回放工件。"""

    summary: _StubSummaryArtifacts
    detail: _StubDetailArtifacts


@dataclass(frozen=True, slots=True)
class _StubAutoBattleExecutionResult:
    """模拟突破服务所需的自动战斗返回结构。"""

    domain_result: _StubDomainResult
    persistence_mapping: _StubPersistenceMapping
    report_artifacts: _StubReportArtifacts
    persisted_battle_report_id: int | None = None


class _StubAutoBattleService:
    """用固定胜负结果替代真实自动战斗，聚焦突破链路编排与结算。"""

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
            report_artifacts=_StubReportArtifacts(
                summary=_StubSummaryArtifacts(),
                detail=_StubDetailArtifacts(),
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
    """创建突破链路集成测试所需的真实仓储与应用服务。"""
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
    difficulty_service = BreakthroughDynamicDifficultyService(
        current_attribute_service=current_attribute_service,
        static_config=static_config,
    )
    progression_service = CharacterProgressionService(
        character_repository=character_repository,
        inventory_repository=inventory_repository,
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
        difficulty_service=difficulty_service,
        static_config=static_config,
    )
    material_trial_service = BreakthroughMaterialTrialService(
        state_repository=state_repository,
        character_repository=character_repository,
        inventory_repository=inventory_repository,
        battle_record_repository=battle_record_repository,
        auto_battle_service=auto_battle_service,
        current_attribute_service=current_attribute_service,
        difficulty_service=difficulty_service,
        static_config=static_config,
    )
    return {
        "growth_service": growth_service,
        "trial_service": trial_service,
        "material_trial_service": material_trial_service,
        "progression_service": progression_service,
        "character_repository": character_repository,
        "breakthrough_repository": breakthrough_repository,
        "inventory_repository": inventory_repository,
        "battle_record_repository": battle_record_repository,
        "auto_battle_service": auto_battle_service,
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



def _seed_inventory_item(
    *,
    inventory_repository: SqlAlchemyInventoryRepository,
    character_id: int,
    item_id: str,
    quantity: int,
) -> None:
    inventory_repository.upsert_item(
        InventoryItem(
            character_id=character_id,
            item_type="material",
            item_id=item_id,
            quantity=quantity,
            item_payload_json={"bound": True},
        )
    )



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



def test_breakthrough_trial_current_gate_stays_single_track_before_first_clear(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """未首通前只应允许当前境界对应的唯一试炼作为主线玄关。"""
    database_url = _build_sqlite_url(tmp_path / "breakthrough_current_gate.db")
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
            discord_user_id="71004",
            player_display_name="守关者",
            character_name="问岚",
        )
        _set_character_progress(
            character_repository=services["character_repository"],
            character_id=created.character_id,
            realm_id="mortal",
            stage_id="perfect",
            qualification_obtained=False,
        )

        hub = services["trial_service"].get_trial_hub(character_id=created.character_id)

        assert hub.current_trial is not None
        assert hub.current_trial.mapping_id == "mortal_to_qi_refining"
        assert hub.current_trial.can_challenge is True
        assert hub.repeatable_trials == ()
        locked_trial = next(trial for group in hub.groups for trial in group.trials if trial.mapping_id == "foundation_to_core")
        assert locked_trial.can_challenge is False



def test_breakthrough_trial_first_clear_aftermath_keeps_gate_in_repeatable_history(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """首通获得资格后，低层试炼服务仍应保留该玄关的已通关历史快照供新前台自行筛选。"""
    database_url = _build_sqlite_url(tmp_path / "breakthrough_gate_after_clear.db")
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
            discord_user_id="71005",
            player_display_name="得关者",
            character_name="澹宁",
        )
        _set_character_progress(
            character_repository=services["character_repository"],
            character_id=created.character_id,
            realm_id="mortal",
            stage_id="perfect",
            qualification_obtained=False,
        )

        services["trial_service"].challenge_trial(
            character_id=created.character_id,
            mapping_id="mortal_to_qi_refining",
            seed=10,
            now=datetime(2026, 3, 26, 21, 20, 0),
            persist_battle_report=False,
        )
        hub = services["trial_service"].get_trial_hub(character_id=created.character_id)

        assert hub.current_trial is not None
        assert hub.current_trial.mapping_id == "mortal_to_qi_refining"
        assert hub.current_trial.is_cleared is True
        assert hub.current_trial.can_challenge is True
        assert hub.repeatable_trials == (hub.current_trial,)
        assert hub.qualification_obtained is True



def test_material_trial_victory_adds_required_items_into_inventory(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """材料秘境胜利后，掉落应直接写入现有库存体系。"""
    database_url = _build_sqlite_url(tmp_path / "breakthrough_material_inventory.db")
    _upgrade_database(database_url, monkeypatch)
    session_factory = create_session_factory(database_url)
    static_config = load_static_config()

    with session_scope(session_factory) as session:
        services = _build_services(
            session,
            static_config=static_config,
            auto_battle_service=_StubAutoBattleService(outcome=BattleOutcome.ALLY_VICTORY, battle_report_id=9201),
        )
        created = services["growth_service"].create_character(
            discord_user_id="71006",
            player_display_name="采材者",
            character_name="栖玄",
        )
        _set_character_progress(
            character_repository=services["character_repository"],
            character_id=created.character_id,
            realm_id="qi_refining",
            stage_id="perfect",
            qualification_obtained=False,
        )
        _seed_inventory_item(
            inventory_repository=services["inventory_repository"],
            character_id=created.character_id,
            item_id="spirit_pattern_stone",
            quantity=1,
        )

        result = services["material_trial_service"].challenge_material_trial(
            character_id=created.character_id,
            mapping_id="qi_refining_to_foundation",
            seed=11,
            now=datetime(2026, 3, 27, 9, 0, 0),
            persist_battle_report=True,
        )

        foundation_pill = services["inventory_repository"].get_item(created.character_id, "material", "foundation_pill")
        spirit_pattern_stone = services["inventory_repository"].get_item(created.character_id, "material", "spirit_pattern_stone")
        drop_records = services["battle_record_repository"].list_drop_records(created.character_id)
        precheck = services["progression_service"].get_breakthrough_precheck(character_id=created.character_id)

        assert result.victory is True
        assert result.battle_report_id == 9201
        assert foundation_pill is not None and foundation_pill.quantity == 1
        assert spirit_pattern_stone is not None and spirit_pattern_stone.quantity == 2
        assert {item.item_id: item.quantity for item in result.drop_items} == {
            "foundation_pill": 1,
            "spirit_pattern_stone": 1,
        }
        assert len(drop_records) == 1
        assert drop_records[0].source_type == "breakthrough_material_trial"
        material_gaps = [gap for gap in precheck.gaps if gap.gap_type == "material_insufficient"]
        assert len(material_gaps) == 1
        assert material_gaps[0].item_id == "spirit_pattern_stone"
        assert material_gaps[0].missing_value == 1



def test_material_trial_target_victory_curve_controls_core_drop_allocation(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """材料掉落应围绕目标胜利次数曲线收束，且单次不超过当前缺口。"""
    database_url = _build_sqlite_url(tmp_path / "breakthrough_material_curve.db")
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
            discord_user_id="71007",
            player_display_name="循材者",
            character_name="岳川",
        )
        _set_character_progress(
            character_repository=services["character_repository"],
            character_id=created.character_id,
            realm_id="great_vehicle",
            stage_id="perfect",
            qualification_obtained=False,
        )

        first = services["material_trial_service"].challenge_material_trial(
            character_id=created.character_id,
            mapping_id="great_vehicle_to_tribulation",
            seed=12,
            now=datetime(2026, 3, 27, 10, 0, 0),
            persist_battle_report=False,
        )
        second = services["material_trial_service"].challenge_material_trial(
            character_id=created.character_id,
            mapping_id="great_vehicle_to_tribulation",
            seed=13,
            now=datetime(2026, 3, 27, 10, 5, 0),
            persist_battle_report=False,
        )

        first_drop_by_item = {item.item_id: item.quantity for item in first.drop_items}
        second_drop_by_item = {item.item_id: item.quantity for item in second.drop_items}
        inventory_lightning = services["inventory_repository"].get_item(created.character_id, "material", "tribulation_lightning_talisman")
        inventory_marrow = services["inventory_repository"].get_item(created.character_id, "material", "immortal_marrow_liquid")

        assert first_drop_by_item == {
            "tribulation_lightning_talisman": 1,
            "immortal_marrow_liquid": 3,
        }
        assert second_drop_by_item == {
            "tribulation_lightning_talisman": 1,
            "immortal_marrow_liquid": 3,
        }
        assert inventory_lightning is not None and inventory_lightning.quantity == 2
        assert inventory_marrow is not None and inventory_marrow.quantity == 6
        assert second.all_satisfied_after is False
        assert second.remaining_gap_summary == "劫雷符 ×3；仙髓液 ×9"



def test_dynamic_difficulty_applies_to_breakthrough_and_material_trials(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """轻量动态难度修正应同时作用于突破秘境与材料秘境。"""
    database_url = _build_sqlite_url(tmp_path / "breakthrough_dynamic_difficulty.db")
    _upgrade_database(database_url, monkeypatch)
    session_factory = create_session_factory(database_url)
    static_config = load_static_config()

    with session_scope(session_factory) as session:
        auto_battle_service = _StubAutoBattleService(outcome=BattleOutcome.ALLY_VICTORY)
        services = _build_services(session, static_config=static_config, auto_battle_service=auto_battle_service)
        created = services["growth_service"].create_character(
            discord_user_id="71008",
            player_display_name="校难者",
            character_name="行策",
        )
        _set_character_progress(
            character_repository=services["character_repository"],
            character_id=created.character_id,
            realm_id="foundation",
            stage_id="perfect",
            qualification_obtained=False,
        )

        services["trial_service"].challenge_trial(
            character_id=created.character_id,
            mapping_id="foundation_to_core",
            seed=14,
            now=datetime(2026, 3, 27, 11, 0, 0),
            persist_battle_report=False,
        )
        breakthrough_snapshot = dict(services["auto_battle_service"].last_request.environment_snapshot)

        services["material_trial_service"].challenge_material_trial(
            character_id=created.character_id,
            mapping_id="foundation_to_core",
            seed=15,
            now=datetime(2026, 3, 27, 11, 5, 0),
            persist_battle_report=False,
        )
        material_snapshot = dict(services["auto_battle_service"].last_request.environment_snapshot)

        assert breakthrough_snapshot["boss_scale_permille"] == (
            breakthrough_snapshot["base_boss_scale_permille"] + breakthrough_snapshot["difficulty_adjustment_permille"]
        )
        assert material_snapshot["boss_scale_permille"] == (
            material_snapshot["base_boss_scale_permille"] + material_snapshot["difficulty_adjustment_permille"]
        )
        assert isinstance(breakthrough_snapshot["player_power_ratio_permille"], int)
        assert isinstance(material_snapshot["player_power_ratio_permille"], int)
        assert material_snapshot["boss_scale_permille"] < breakthrough_snapshot["boss_scale_permille"]



def test_character_progression_precheck_detects_material_trial_inventory_writeback(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """材料秘境入库后，现有突破预检应能直接感知库存变化。"""
    database_url = _build_sqlite_url(tmp_path / "breakthrough_precheck_after_material.db")
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
            discord_user_id="71010",
            player_display_name="回读者",
            character_name="长澜",
        )
        _set_character_progress(
            character_repository=services["character_repository"],
            character_id=created.character_id,
            realm_id="mortal",
            stage_id="perfect",
            qualification_obtained=False,
        )
        before = services["progression_service"].get_breakthrough_precheck(character_id=created.character_id)
        services["material_trial_service"].challenge_material_trial(
            character_id=created.character_id,
            mapping_id="mortal_to_qi_refining",
            seed=16,
            now=datetime(2026, 3, 27, 12, 0, 0),
            persist_battle_report=False,
        )
        after = services["progression_service"].get_breakthrough_precheck(character_id=created.character_id)

        material_gap_before = next(gap for gap in before.gaps if gap.gap_type == "material_insufficient")
        assert material_gap_before.missing_value == 2
        assert all(gap.gap_type != "material_insufficient" for gap in after.gaps)
