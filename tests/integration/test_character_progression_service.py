"""角色突破前置条件与正式突破执行服务集成测试。"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
import pytest

from application.character import (
    BreakthroughExecutionBlockedError,
    CharacterGrowthService,
    CharacterProgressionService,
)
from domain.character import CharacterGrowthProgression
from infrastructure.config.static import load_static_config
from infrastructure.db.models import InventoryItem
from infrastructure.db.repositories import (
    SqlAlchemyCharacterRepository,
    SqlAlchemyInventoryRepository,
    SqlAlchemyPlayerRepository,
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
    """创建测试所需的成长服务、突破服务与仓储。"""
    player_repository = SqlAlchemyPlayerRepository(session)
    character_repository = SqlAlchemyCharacterRepository(session)
    inventory_repository = SqlAlchemyInventoryRepository(session)
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
    return growth_service, progression_service, character_repository, inventory_repository


def test_breakthrough_precheck_returns_all_missing_gaps(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """普通前置条件不足时应返回缺口清单，而不是抛出业务异常。"""
    database_url = _build_sqlite_url(tmp_path / "character_progression_gaps.db")
    _upgrade_database(database_url, monkeypatch)
    session_factory = create_session_factory(database_url)
    static_config = load_static_config()

    with session_scope(session_factory) as session:
        growth_service, progression_service, _, inventory_repository = _build_services(session, static_config)
        created = growth_service.create_character(
            discord_user_id="32001",
            player_display_name="沧溟",
            character_name="景行",
        )
        growth_service.add_cultivation(character_id=created.character_id, amount=20)
        growth_service.add_comprehension(character_id=created.character_id, amount=4)
        inventory_repository.upsert_item(
            InventoryItem(
                character_id=created.character_id,
                item_type="material",
                item_id="qi_condensation_grass",
                quantity=1,
                item_payload_json={},
            )
        )

        result = progression_service.get_breakthrough_precheck(character_id=created.character_id)

        assert result.character_id == created.character_id
        assert result.current_realm_id == "mortal"
        assert result.target_realm_id == "qi_refining"
        assert result.mapping_id == "mortal_to_qi_refining"
        assert result.passed is False
        assert result.required_cultivation_value == 50
        assert result.required_comprehension_value == 10
        assert result.qualification_obtained is False

        gap_map = {gap.gap_type: gap for gap in result.gaps if gap.item_id is None}
        material_gap = next(gap for gap in result.gaps if gap.gap_type == "material_insufficient")

        assert gap_map["cultivation_insufficient"].missing_value == 30
        assert gap_map["comprehension_insufficient"].missing_value == 6
        assert gap_map["qualification_missing"].missing_value == 1
        assert material_gap.item_type == "material"
        assert material_gap.item_id == "qi_condensation_grass"
        assert material_gap.current_value == 1
        assert material_gap.required_value == 2
        assert material_gap.missing_value == 1


def test_breakthrough_precheck_passes_when_all_conditions_are_met(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """修为、感悟、资格与材料齐全时应返回通过结果。"""
    database_url = _build_sqlite_url(tmp_path / "character_progression_pass.db")
    _upgrade_database(database_url, monkeypatch)
    session_factory = create_session_factory(database_url)
    static_config = load_static_config()

    with session_scope(session_factory) as session:
        growth_service, progression_service, character_repository, inventory_repository = _build_services(session, static_config)
        created = growth_service.create_character(
            discord_user_id="32002",
            player_display_name="青崖",
            character_name="玄照",
        )
        growth_service.add_cultivation(character_id=created.character_id, amount=100)
        growth_service.add_comprehension(character_id=created.character_id, amount=15)

        aggregate = character_repository.get_aggregate(created.character_id)
        assert aggregate is not None
        assert aggregate.progress is not None
        aggregate.progress.breakthrough_qualification_obtained = True
        character_repository.save_progress(aggregate.progress)

        inventory_repository.upsert_item(
            InventoryItem(
                character_id=created.character_id,
                item_type="material",
                item_id="qi_condensation_grass",
                quantity=2,
                item_payload_json={"bound": True},
            )
        )

        result = progression_service.get_breakthrough_precheck(character_id=created.character_id)

        assert result.current_realm_id == "mortal"
        assert result.target_realm_id == "qi_refining"
        assert result.mapping_id == "mortal_to_qi_refining"
        assert result.passed is True
        assert result.required_cultivation_value == 50
        assert result.required_comprehension_value == 10
        assert result.qualification_obtained is True
        assert result.gaps == ()


def test_execute_breakthrough_updates_realm_and_consumes_resources(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """正式突破应切换大境界、重置修为、扣减感悟与材料，并消费资格。"""
    database_url = _build_sqlite_url(tmp_path / "character_progression_execute.db")
    _upgrade_database(database_url, monkeypatch)
    session_factory = create_session_factory(database_url)
    static_config = load_static_config()
    progression = CharacterGrowthProgression(static_config)
    target_rule = progression.get_realm_rule("qi_refining")

    with session_scope(session_factory) as session:
        growth_service, progression_service, character_repository, inventory_repository = _build_services(session, static_config)
        created = growth_service.create_character(
            discord_user_id="32004",
            player_display_name="玄陵",
            character_name="明川",
        )
        growth_service.add_cultivation(character_id=created.character_id, amount=50)
        growth_service.add_comprehension(character_id=created.character_id, amount=14)

        aggregate = character_repository.get_aggregate(created.character_id)
        assert aggregate is not None
        assert aggregate.progress is not None
        aggregate.progress.breakthrough_qualification_obtained = True
        character_repository.save_progress(aggregate.progress)

        inventory_repository.upsert_item(
            InventoryItem(
                character_id=created.character_id,
                item_type="material",
                item_id="qi_condensation_grass",
                quantity=3,
                item_payload_json={"bound": True},
            )
        )

        result = progression_service.execute_breakthrough(character_id=created.character_id)
        aggregate = character_repository.get_aggregate(created.character_id)
        material_item = inventory_repository.get_item(created.character_id, "material", "qi_condensation_grass")

        assert aggregate is not None
        assert aggregate.progress is not None
        assert material_item is not None
        assert result.character_id == created.character_id
        assert result.mapping_id == "mortal_to_qi_refining"
        assert result.from_realm_id == "mortal"
        assert result.to_realm_id == "qi_refining"
        assert result.new_stage_id == target_rule.stage_thresholds[0].stage_id
        assert result.new_stage_name == target_rule.stage_thresholds[0].stage_name
        assert result.previous_cultivation_value == 50
        assert result.new_cultivation_value == 0
        assert result.previous_comprehension_value == 14
        assert result.consumed_comprehension_value == 10
        assert result.remaining_comprehension_value == 4
        assert result.qualification_consumed is True
        assert result.consumed_items == (
            result.consumed_items[0],
        )
        assert result.consumed_items[0].item_type == "material"
        assert result.consumed_items[0].item_id == "qi_condensation_grass"
        assert result.consumed_items[0].quantity == 2
        assert result.consumed_items[0].before_quantity == 3
        assert result.consumed_items[0].after_quantity == 1
        assert aggregate.progress.realm_id == "qi_refining"
        assert aggregate.progress.stage_id == target_rule.stage_thresholds[0].stage_id
        assert aggregate.progress.cultivation_value == 0
        assert aggregate.progress.comprehension_value == 4
        assert aggregate.progress.breakthrough_qualification_obtained is False
        assert material_item.quantity == 1


def test_execute_breakthrough_raises_when_precheck_is_not_passed(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """正式突破前置不足时应拒绝执行，且不应写回任何状态。"""
    database_url = _build_sqlite_url(tmp_path / "character_progression_execute_blocked.db")
    _upgrade_database(database_url, monkeypatch)
    session_factory = create_session_factory(database_url)
    static_config = load_static_config()

    with session_scope(session_factory) as session:
        growth_service, progression_service, character_repository, inventory_repository = _build_services(session, static_config)
        created = growth_service.create_character(
            discord_user_id="32005",
            player_display_name="迟水",
            character_name="照寒",
        )
        growth_service.add_cultivation(character_id=created.character_id, amount=50)
        growth_service.add_comprehension(character_id=created.character_id, amount=9)

        aggregate = character_repository.get_aggregate(created.character_id)
        assert aggregate is not None
        assert aggregate.progress is not None
        aggregate.progress.breakthrough_qualification_obtained = True
        character_repository.save_progress(aggregate.progress)

        inventory_repository.upsert_item(
            InventoryItem(
                character_id=created.character_id,
                item_type="material",
                item_id="qi_condensation_grass",
                quantity=2,
                item_payload_json={"bound": True},
            )
        )

        with pytest.raises(BreakthroughExecutionBlockedError):
            progression_service.execute_breakthrough(character_id=created.character_id)

        aggregate = character_repository.get_aggregate(created.character_id)
        material_item = inventory_repository.get_item(created.character_id, "material", "qi_condensation_grass")

        assert aggregate is not None
        assert aggregate.progress is not None
        assert aggregate.progress.realm_id == "mortal"
        assert aggregate.progress.stage_id == "perfect"
        assert aggregate.progress.cultivation_value == 50
        assert aggregate.progress.comprehension_value == 9
        assert aggregate.progress.breakthrough_qualification_obtained is True
        assert material_item is not None
        assert material_item.quantity == 2


def test_breakthrough_precheck_reports_open_limit_at_launch_cap(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """达到首发开放上限时应返回开放上限缺口。"""
    database_url = _build_sqlite_url(tmp_path / "character_progression_open_limit.db")
    _upgrade_database(database_url, monkeypatch)
    session_factory = create_session_factory(database_url)
    static_config = load_static_config()
    progression = CharacterGrowthProgression(static_config)
    tribulation_rule = progression.get_realm_rule("tribulation")

    with session_scope(session_factory) as session:
        growth_service, progression_service, character_repository, _ = _build_services(session, static_config)
        created = growth_service.create_character(
            discord_user_id="32003",
            player_display_name="长风",
            character_name="明渊",
        )

        aggregate = character_repository.get_aggregate(created.character_id)
        assert aggregate is not None
        assert aggregate.progress is not None
        aggregate.progress.realm_id = "tribulation"
        aggregate.progress.stage_id = "perfect"
        aggregate.progress.cultivation_value = tribulation_rule.total_cultivation
        aggregate.progress.comprehension_value = 2400
        aggregate.progress.breakthrough_qualification_obtained = True
        character_repository.save_progress(aggregate.progress)

        result = progression_service.get_breakthrough_precheck(character_id=created.character_id)

        assert result.current_realm_id == "tribulation"
        assert result.target_realm_id is None
        assert result.target_realm_name is None
        assert result.mapping_id is None
        assert result.passed is False
        assert result.required_cultivation_value is None
        assert result.required_comprehension_value is None
        assert result.qualification_obtained is True
        assert result.gaps == (result.gaps[0],)
        assert result.gaps[0].gap_type == "open_limit"
