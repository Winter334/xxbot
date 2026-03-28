"""阶段 6 装备迁移与仓储集成测试。"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

from infrastructure.db.models import (
    ArtifactNurtureState,
    ArtifactProfile,
    Character,
    CurrencyBalance,
    EquipmentAffix,
    EquipmentDismantleRecord,
    EquipmentEnhancement,
    EquipmentItem,
    EquipmentNamingState,
    InventoryItem,
    Player,
)
from infrastructure.db.repositories import (
    SqlAlchemyCharacterRepository,
    SqlAlchemyEquipmentRepository,
    SqlAlchemyInventoryRepository,
    SqlAlchemyPlayerRepository,
)
from infrastructure.db.session import create_engine_from_url, create_session_factory, session_scope

PROJECT_ROOT = Path(__file__).resolve().parents[3]
EXPECTED_STAGE6_TABLES = {
    "artifact_nurture_states",
    "equipment_naming_states",
    "equipment_dismantle_records",
}


def _build_sqlite_url(database_path: Path) -> str:
    """构造测试用 SQLite 地址。"""
    return f"sqlite+pysqlite:///{database_path.as_posix()}"


def _upgrade_database(database_url: str, monkeypatch) -> None:
    """将测试数据库升级到最新迁移版本。"""
    monkeypatch.chdir(PROJECT_ROOT)
    monkeypatch.setenv("DATABASE_URL", database_url)
    command.upgrade(Config((PROJECT_ROOT / "alembic.ini").as_posix()), "head")


def test_stage6_migration_adds_equipment_tables_columns_and_indexes(tmp_path, monkeypatch) -> None:
    """阶段 6 迁移应补齐装备成长、命名与分解审计结构。"""
    database_url = _build_sqlite_url(tmp_path / "stage6_migration.db")
    _upgrade_database(database_url, monkeypatch)

    engine = create_engine_from_url(database_url)
    try:
        inspector = inspect(engine)
        table_names = set(inspector.get_table_names())
        assert EXPECTED_STAGE6_TABLES.issubset(table_names)

        equipment_item_columns = {column["name"] for column in inspector.get_columns("equipment_items")}
        assert {
            "slot_name",
            "quality_name",
            "template_id",
            "template_name",
            "rank_id",
            "rank_name",
            "rank_order",
            "mapped_realm_id",
            "is_artifact",
            "resonance_name",
            "item_state",
            "dismantled_at",
        }.issubset(equipment_item_columns)

        equipment_item_indexes = {index["name"] for index in inspector.get_indexes("equipment_items")}
        assert "ix_equipment_items_character_id_item_state" in equipment_item_indexes
        assert "ix_equipment_items_template_id" in equipment_item_indexes
        assert "ix_equipment_items_rank_id" in equipment_item_indexes

        enhancement_columns = {column["name"] for column in inspector.get_columns("equipment_enhancements")}
        assert {"base_stat_bonus_ratio", "affix_bonus_ratio"}.issubset(enhancement_columns)

        affix_columns = {column["name"] for column in inspector.get_columns("equipment_affixes")}
        assert {
            "affix_name",
            "stat_id",
            "category",
            "tier_name",
            "value",
            "is_pve_specialized",
            "is_pvp_specialized",
        }.issubset(affix_columns)

        affix_indexes = {index["name"] for index in inspector.get_indexes("equipment_affixes")}
        assert "ix_equipment_affixes_tier_id" in affix_indexes

        naming_indexes = {index["name"] for index in inspector.get_indexes("equipment_naming_states")}
        assert "ix_equipment_naming_states_naming_source" in naming_indexes

        dismantle_indexes = {index["name"] for index in inspector.get_indexes("equipment_dismantle_records")}
        assert "ix_equipment_dismantle_records_character_id_status" in dismantle_indexes

        inventory_indexes = {index["name"] for index in inspector.get_indexes("inventory_items")}
        assert "ix_inventory_items_character_id_item_type" in inventory_indexes
    finally:
        engine.dispose()


def test_stage6_equipment_repositories_round_trip_growth_naming_and_dismantle_state(tmp_path, monkeypatch) -> None:
    """阶段 6 仓储应能完整读写装备成长、法宝培养、命名、阶数与分解审计状态。"""
    database_url = _build_sqlite_url(tmp_path / "stage6_repositories.db")
    _upgrade_database(database_url, monkeypatch)

    session_factory = create_session_factory(database_url)
    dismantled_at = datetime(2026, 3, 26, 12, 30, 0)

    with session_scope(session_factory) as session:
        player_repo = SqlAlchemyPlayerRepository(session)
        character_repo = SqlAlchemyCharacterRepository(session)
        equipment_repo = SqlAlchemyEquipmentRepository(session)
        inventory_repo = SqlAlchemyInventoryRepository(session)

        player = player_repo.add(Player(discord_user_id="30001", display_name="离火"))
        character = character_repo.add(
            Character(
                player_id=player.id,
                name="玄霄",
                title="炼器师",
                total_power_score=1580,
                public_power_score=1510,
                hidden_pvp_score=1470,
            )
        )
        character.title = "神兵使"
        character_repo.save(character)
        character_id = character.id

        character_repo.save_currency_balance(
            CurrencyBalance(character_id=character_id, spirit_stone=9800, honor_coin=150)
        )

        inventory_repo.upsert_item(
            InventoryItem(
                character_id=character_id,
                item_type="material",
                item_id="enhancement_stone",
                quantity=32,
                item_payload_json={"bound": True},
            )
        )
        inventory_repo.upsert_item(
            InventoryItem(
                character_id=character_id,
                item_type="material",
                item_id="artifact_essence",
                quantity=9,
                item_payload_json={"source": "dismantle"},
            )
        )
        inventory_repo.upsert_item(
            InventoryItem(
                character_id=character_id,
                item_type="token",
                item_id="naming_scroll",
                quantity=2,
                item_payload_json={"rarity": "epic"},
            )
        )

        weapon = EquipmentItem(
            character_id=character_id,
            slot_id="weapon",
            slot_name="武器",
            equipped_slot_id="weapon",
            quality_id="legendary",
            quality_name="传说",
            template_id="iron_sword",
            template_name="玄铁剑",
            rank_id="foundation",
            rank_name="三阶",
            rank_order=3,
            mapped_realm_id="foundation",
            is_artifact=False,
            resonance_name=None,
            item_state="active",
            item_name="破军玄铁剑",
            base_snapshot_json={
                "base_attributes": [
                    {"stat_id": "attack_power", "value": 291},
                    {"stat_id": "speed", "value": 21},
                ]
            },
        )
        weapon.enhancement = EquipmentEnhancement(
            enhancement_level=6,
            success_count=6,
            failure_count=2,
            base_stat_bonus_ratio=Decimal("0.2400"),
            affix_bonus_ratio=Decimal("0.1200"),
        )
        weapon.affixes.extend(
            [
                EquipmentAffix(
                    position=1,
                    affix_id="attack_power",
                    affix_name="破军",
                    stat_id="attack_power",
                    category="base_stat",
                    tier_id="heaven",
                    tier_name="天",
                    roll_value=Decimal("1.5800"),
                    value=86,
                    is_pve_specialized=False,
                    is_pvp_specialized=False,
                ),
                EquipmentAffix(
                    position=2,
                    affix_id="pve_damage",
                    affix_name="斩妖",
                    stat_id="pve_damage_permille",
                    category="combat_bonus",
                    tier_id="earth",
                    tier_name="地",
                    roll_value=Decimal("1.2600"),
                    value=143,
                    is_pve_specialized=True,
                    is_pvp_specialized=False,
                ),
            ]
        )
        weapon.naming_state = EquipmentNamingState(
            resolved_name="破军玄铁剑",
            naming_template_id="legendary_masterwork",
            naming_source="template_rule",
            naming_metadata_json={"primary_affix_name": "破军", "template_name": "玄铁剑"},
        )
        equipment_repo.add(weapon)
        weapon_id = weapon.id

        artifact = EquipmentItem(
            character_id=character_id,
            slot_id="artifact",
            slot_name="法宝",
            equipped_slot_id=None,
            quality_id="epic",
            quality_name="史诗",
            template_id="flame_seal",
            template_name="玄火印",
            rank_id="tribulation",
            rank_name="十阶",
            rank_order=10,
            mapped_realm_id="tribulation",
            is_artifact=True,
            resonance_name="火契",
            item_state="dismantled",
            item_name="火契玄火印",
            base_snapshot_json={
                "base_attributes": [
                    {"stat_id": "shield_power", "value": 4149},
                    {"stat_id": "attack_power", "value": 2508},
                ]
            },
            dismantled_at=dismantled_at,
        )
        artifact.enhancement = EquipmentEnhancement(
            enhancement_level=4,
            success_count=4,
            failure_count=1,
            base_stat_bonus_ratio=Decimal("0.1600"),
            affix_bonus_ratio=Decimal("0.0800"),
        )
        artifact.affixes.append(
            EquipmentAffix(
                position=1,
                affix_id="shield_power",
                affix_name="镇岳",
                stat_id="shield_power",
                category="base_stat",
                tier_id="earth",
                tier_name="地",
                roll_value=Decimal("1.3100"),
                value=1775,
                is_pve_specialized=False,
                is_pvp_specialized=False,
            )
        )
        artifact.artifact_profile = ArtifactProfile(
            artifact_template_id="flame_seal",
            refinement_level=2,
            core_effect_snapshot_json={"shield_bonus": 25},
        )
        artifact.artifact_nurture_state = ArtifactNurtureState(
            nurture_level=3,
            base_stat_bonus_ratio=Decimal("0.1500"),
            affix_bonus_ratio=Decimal("0.0900"),
        )
        artifact.naming_state = EquipmentNamingState(
            resolved_name="火契玄火印",
            naming_template_id="artifact_resonant",
            naming_source="template_rule",
            naming_metadata_json={"resonance_name": "火契", "template_name": "玄火印"},
        )
        artifact.dismantle_record = EquipmentDismantleRecord(
            character_id=character_id,
            status="completed",
            returns_json=[
                {"resource_id": "artifact_essence", "quantity": 102},
                {"resource_id": "spirit_sand", "quantity": 143},
            ],
            audit_metadata_json={
                "source": "equipment_dismantle",
                "reason": "test_cleanup",
                "operator": "pytest",
            },
            settled_at=dismantled_at,
        )
        equipment_repo.add(artifact)
        artifact_id = artifact.id

    with session_scope(session_factory) as session:
        character_repo = SqlAlchemyCharacterRepository(session)
        equipment_repo = SqlAlchemyEquipmentRepository(session)
        inventory_repo = SqlAlchemyInventoryRepository(session)

        aggregate = character_repo.get_aggregate(character_id)
        assert aggregate is not None
        assert aggregate.character.title == "神兵使"
        assert aggregate.currency_balance is not None
        assert aggregate.currency_balance.spirit_stone == 9800
        assert len(aggregate.equipment_items) == 2
        assert len(aggregate.inventory_items) == 3

        loaded_weapon = equipment_repo.get_by_character_and_id(character_id, weapon_id)
        assert loaded_weapon is not None
        assert loaded_weapon.rank_id == "foundation"
        assert loaded_weapon.rank_name == "三阶"
        assert loaded_weapon.enhancement is not None
        assert loaded_weapon.enhancement.base_stat_bonus_ratio == Decimal("0.2400")
        assert loaded_weapon.naming_state is not None
        assert loaded_weapon.naming_state.naming_source == "template_rule"
        assert loaded_weapon.naming_state.naming_metadata_json["primary_affix_name"] == "破军"

        equipped_weapon = equipment_repo.get_equipped_in_slot(character_id, "weapon")
        assert equipped_weapon is not None
        assert equipped_weapon.id == weapon_id

        active_items = equipment_repo.list_active_by_character_id(character_id)
        assert [item.id for item in active_items] == [weapon_id]

        dismantled_items = equipment_repo.list_dismantled_by_character_id(character_id)
        assert [item.id for item in dismantled_items] == [artifact_id]
        assert dismantled_items[0].rank_id == "tribulation"
        assert dismantled_items[0].artifact_nurture_state is not None
        assert dismantled_items[0].artifact_nurture_state.nurture_level == 3
        assert dismantled_items[0].dismantle_record is not None
        assert dismantled_items[0].dismantle_record.audit_metadata_json["reason"] == "test_cleanup"

        equipped_items = equipment_repo.list_equipped_by_character_id(character_id)
        assert [item.id for item in equipped_items] == [weapon_id]

        material_items = inventory_repo.list_by_character_id_and_type(character_id, "material")
        assert {item.item_id for item in material_items} == {"artifact_essence", "enhancement_stone"}

        loaded_weapon.item_name = "斩星玄铁剑"
        assert loaded_weapon.naming_state is not None
        loaded_weapon.naming_state.resolved_name = "斩星玄铁剑"
        equipment_repo.save(loaded_weapon)

    with session_scope(session_factory) as session:
        equipment_repo = SqlAlchemyEquipmentRepository(session)

        updated_weapon = equipment_repo.get(weapon_id)
        assert updated_weapon is not None
        assert updated_weapon.item_name == "斩星玄铁剑"
        assert updated_weapon.rank_name == "三阶"
        assert updated_weapon.naming_state is not None
        assert updated_weapon.naming_state.resolved_name == "斩星玄铁剑"
        assert len(equipment_repo.list_by_character_id(character_id)) == 2
