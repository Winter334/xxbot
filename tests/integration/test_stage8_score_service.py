"""阶段 8 评分服务集成测试。"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
import pytest

from application.character import CharacterGrowthService
from application.equipment import EquipmentService
from application.ranking import CharacterScoreService
from infrastructure.config.static import load_static_config
from infrastructure.db.models import Character, CharacterScoreSnapshot, InventoryItem, Player
from infrastructure.db.repositories import (
    SqlAlchemyCharacterRepository,
    SqlAlchemyCharacterScoreSnapshotRepository,
    SqlAlchemyEquipmentRepository,
    SqlAlchemyInventoryRepository,
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


def test_stage8_migration_creates_character_score_snapshot_table(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """阶段 8 迁移应创建角色评分明细快照表。"""
    database_url = _build_sqlite_url(tmp_path / "stage8_score_migration.db")
    _upgrade_database(database_url, monkeypatch)

    engine = create_engine_from_url(database_url)
    try:
        table_names = set(engine.dialect.get_table_names(engine.connect()))
    finally:
        engine.dispose()

    assert "character_score_snapshots" in table_names


def test_refresh_character_score_updates_cache_and_snapshot(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """单角色评分刷新应写回角色缓存列与评分明细快照。"""
    database_url = _build_sqlite_url(tmp_path / "stage8_score_refresh.db")
    _upgrade_database(database_url, monkeypatch)
    session_factory = create_session_factory(database_url)
    static_config = load_static_config()

    with session_scope(session_factory) as session:
        player_repository = SqlAlchemyPlayerRepository(session)
        character_repository = SqlAlchemyCharacterRepository(session)
        score_snapshot_repository = SqlAlchemyCharacterScoreSnapshotRepository(session)
        inventory_repository = SqlAlchemyInventoryRepository(session)
        equipment_repository = SqlAlchemyEquipmentRepository(session)
        score_service = CharacterScoreService(
            character_repository=character_repository,
            score_snapshot_repository=score_snapshot_repository,
            static_config=static_config,
        )
        growth_service = CharacterGrowthService(
            player_repository=player_repository,
            character_repository=character_repository,
            static_config=static_config,
            score_service=score_service,
        )
        equipment_service = EquipmentService(
            character_repository=character_repository,
            equipment_repository=equipment_repository,
            inventory_repository=inventory_repository,
            static_config=static_config,
            score_service=score_service,
        )

        created = growth_service.create_character(
            discord_user_id="81001",
            player_display_name="评分修士",
            character_name="澄明",
        )
        character_id = created.character_id
        growth_service.add_cultivation(character_id=character_id, amount=40)
        growth_service.add_comprehension(character_id=character_id, amount=18)

        balance = character_repository.get_aggregate(character_id).currency_balance
        assert balance is not None
        balance.spirit_stone = 2000
        character_repository.save_currency_balance(balance)

        inventory_repository.upsert_item(
            InventoryItem(
                character_id=character_id,
                item_type="material",
                item_id="enhancement_stone",
                quantity=12,
                item_payload_json={},
            )
        )
        inventory_repository.upsert_item(
            InventoryItem(
                character_id=character_id,
                item_type="material",
                item_id="artifact_essence",
                quantity=6,
                item_payload_json={},
            )
        )

        weapon = equipment_service.generate_equipment(
            character_id=character_id,
            slot_id="weapon",
            quality_id="epic",
            template_id="iron_sword",
            affix_count=2,
            seed=7,
        )
        armor = equipment_service.generate_equipment(
            character_id=character_id,
            slot_id="armor",
            quality_id="rare",
            template_id="cloud_robe",
            affix_count=2,
            seed=9,
        )
        artifact = equipment_service.generate_equipment(
            character_id=character_id,
            slot_id="artifact",
            quality_id="epic",
            template_id="skyfire_mirror",
            affix_count=1,
            seed=5,
        )

        weapon_model = equipment_repository.get(weapon.item.item_id)
        armor_model = equipment_repository.get(armor.item.item_id)
        artifact_model = equipment_repository.get(artifact.item.item_id)
        assert weapon_model is not None
        assert armor_model is not None
        assert artifact_model is not None
        weapon_model.equipped_slot_id = "weapon"
        armor_model.equipped_slot_id = "armor"
        artifact_model.equipped_slot_id = "artifact"
        equipment_repository.save(weapon_model)
        equipment_repository.save(armor_model)
        equipment_repository.save(artifact_model)

        equipment_service.enhance_equipment(
            character_id=character_id,
            equipment_item_id=weapon.item.item_id,
            seed=1,
        )
        equipment_service.nurture_artifact(
            character_id=character_id,
            equipment_item_id=artifact.item.item_id,
        )

        result = score_service.refresh_character_score(character_id=character_id)

        assert result.character_id == character_id
        assert result.public_power_score == result.total_power_score
        assert result.public_power_score == (
            result.growth_score + result.equipment_score + result.skill_score + result.artifact_score
        )
        assert result.hidden_pvp_score == result.public_power_score + result.pvp_adjustment_score
        assert result.breakdown["source_summary"]["equipped_slot_ids"] == ["weapon", "armor", "artifact"]
        assert result.breakdown["skill"]["main_skill"]["skill_name"] == "七杀剑诀"
        assert result.breakdown["skill"]["main_skill"]["total_budget"] == 8
        assert result.breakdown["skill"]["guard_skill"]["total_budget"] == 3
        assert result.breakdown["skill"]["movement_skill"]["total_budget"] == 3
        assert result.breakdown["skill"]["spirit_skill"]["total_budget"] == 3
        assert result.breakdown["source_summary"]["main_skill_name"] == "七杀剑诀"
        assert result.breakdown["source_summary"]["equipped_skill_item_ids"]

        persisted_character = character_repository.get(character_id)
        persisted_snapshot = score_snapshot_repository.get_by_character_id(character_id)
        assert persisted_character is not None
        assert persisted_snapshot is not None
        assert persisted_character.total_power_score == result.total_power_score
        assert persisted_character.public_power_score == result.public_power_score
        assert persisted_character.hidden_pvp_score == result.hidden_pvp_score
        assert persisted_snapshot.score_version == result.score_version
        assert persisted_snapshot.equipment_score == result.equipment_score
        assert persisted_snapshot.artifact_score == result.artifact_score
        assert persisted_snapshot.hidden_pvp_score == result.hidden_pvp_score
        assert persisted_snapshot.source_digest == result.source_digest
        assert persisted_snapshot.breakdown_json["totals"]["public_power_score"] == result.public_power_score
        assert persisted_snapshot.breakdown_json["source_summary"]["character_name"] == "澄明"


def test_growth_and_equipment_services_trigger_single_character_score_refresh(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """成长与装备服务应在目标操作后触发当前角色评分刷新。"""
    database_url = _build_sqlite_url(tmp_path / "stage8_score_trigger.db")
    _upgrade_database(database_url, monkeypatch)
    session_factory = create_session_factory(database_url)
    static_config = load_static_config()

    with session_scope(session_factory) as session:
        player_repository = SqlAlchemyPlayerRepository(session)
        character_repository = SqlAlchemyCharacterRepository(session)
        score_snapshot_repository = SqlAlchemyCharacterScoreSnapshotRepository(session)
        inventory_repository = SqlAlchemyInventoryRepository(session)
        equipment_repository = SqlAlchemyEquipmentRepository(session)
        score_service = CharacterScoreService(
            character_repository=character_repository,
            score_snapshot_repository=score_snapshot_repository,
            static_config=static_config,
        )
        growth_service = CharacterGrowthService(
            player_repository=player_repository,
            character_repository=character_repository,
            static_config=static_config,
            score_service=score_service,
        )
        equipment_service = EquipmentService(
            character_repository=character_repository,
            equipment_repository=equipment_repository,
            inventory_repository=inventory_repository,
            static_config=static_config,
            score_service=score_service,
        )

        created = growth_service.create_character(
            discord_user_id="81002",
            player_display_name="联动修士",
            character_name="玄照",
        )
        character_id = created.character_id
        initial_snapshot = score_snapshot_repository.get_by_character_id(character_id)
        assert initial_snapshot is not None
        initial_public_score = initial_snapshot.public_power_score

        growth_service.add_cultivation(character_id=character_id, amount=20)
        growth_snapshot = score_snapshot_repository.get_by_character_id(character_id)
        assert growth_snapshot is not None
        assert growth_snapshot.public_power_score > initial_public_score

        balance = character_repository.get_aggregate(character_id).currency_balance
        assert balance is not None
        balance.spirit_stone = 1000
        character_repository.save_currency_balance(balance)

        inventory_repository.upsert_item(
            InventoryItem(
                character_id=character_id,
                item_type="material",
                item_id="enhancement_stone",
                quantity=8,
                item_payload_json={},
            )
        )
        weapon = equipment_service.generate_equipment(
            character_id=character_id,
            slot_id="weapon",
            quality_id="legendary",
            template_id="spirit_blade",
            affix_count=2,
            seed=13,
        )
        weapon_model = equipment_repository.get(weapon.item.item_id)
        assert weapon_model is not None
        weapon_model.equipped_slot_id = "weapon"
        equipment_repository.save(weapon_model)

        score_before_enhance = score_snapshot_repository.get_by_character_id(character_id)
        assert score_before_enhance is not None
        equipment_service.enhance_equipment(
            character_id=character_id,
            equipment_item_id=weapon.item.item_id,
            seed=2,
        )
        score_after_enhance = score_snapshot_repository.get_by_character_id(character_id)
        assert score_after_enhance is not None
        assert score_after_enhance.public_power_score >= score_before_enhance.public_power_score
        assert score_after_enhance.equipment_score >= score_before_enhance.equipment_score


def test_single_character_score_refresh_does_not_write_leaderboard_snapshot(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """单角色评分刷新不应顺带写入任何榜单快照。"""
    database_url = _build_sqlite_url(tmp_path / "stage8_score_no_leaderboard_write.db")
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
        created = growth_service.create_character(
            discord_user_id="81003",
            player_display_name="非阻塞评分修士",
            character_name="观澜",
        )
        character_id = created.character_id
        growth_service.add_cultivation(character_id=character_id, amount=36)
        assert snapshot_repository.get_latest_leaderboard("power") is None

        score_service = CharacterScoreService(
            character_repository=character_repository,
            score_snapshot_repository=score_snapshot_repository,
            static_config=static_config,
        )
        result = score_service.refresh_character_score(character_id=character_id)

        assert result.character_id == character_id
        persisted_snapshot = score_snapshot_repository.get_by_character_id(character_id)
        assert persisted_snapshot is not None
        assert persisted_snapshot.public_power_score == result.public_power_score
        assert snapshot_repository.get_latest_leaderboard("power") is None
        assert snapshot_repository.get_latest_leaderboard("pvp_challenge") is None
        assert snapshot_repository.get_latest_leaderboard("endless_depth") is None
