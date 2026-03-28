"""阶段 6 装备应用服务集成测试。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from alembic import command
from alembic.config import Config
import pytest

from application.character.current_attribute_service import CurrentAttributeService
from application.equipment import EquipmentResourceInsufficientError, EquipmentService
from infrastructure.config.static import load_static_config
from infrastructure.db.models import Character, CharacterProgress, CurrencyBalance, InventoryItem, Player
from infrastructure.db.repositories import (
    SqlAlchemyCharacterRepository,
    SqlAlchemyEquipmentRepository,
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


def _create_character_context(session, *, spirit_stone: int = 0, materials: dict[str, int] | None = None) -> int:
    """创建装备应用服务测试所需的角色基础上下文。"""
    player_repo = SqlAlchemyPlayerRepository(session)
    character_repo = SqlAlchemyCharacterRepository(session)
    inventory_repo = SqlAlchemyInventoryRepository(session)

    player = player_repo.add(Player(discord_user_id="50001", display_name="器修"))
    character = character_repo.add(
        Character(
            player_id=player.id,
            name="寒冶",
            title="炼器者",
            total_power_score=0,
            public_power_score=0,
            hidden_pvp_score=0,
        )
    )
    character_repo.save_currency_balance(CurrencyBalance(character_id=character.id, spirit_stone=spirit_stone, honor_coin=0))
    character_repo.save_progress(
        CharacterProgress(
            character_id=character.id,
            realm_id="foundation",
            stage_id="middle",
            cultivation_value=0,
            comprehension_value=0,
            breakthrough_qualification_obtained=False,
            highest_endless_floor=0,
            current_hp_ratio=1,
            current_mp_ratio=1,
        )
    )

    for item_id, quantity in (materials or {}).items():
        inventory_repo.upsert_item(
            InventoryItem(
                character_id=character.id,
                item_type="material",
                item_id=item_id,
                quantity=quantity,
                item_payload_json={},
            )
        )
    return character.id


def test_generate_equipment_enhance_wash_and_reforge_persist_changes(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """应用服务应接通装备生成、强化、洗炼、重铸与命名落库。"""
    database_url = _build_sqlite_url(tmp_path / "equipment_service_flow.db")
    _upgrade_database(database_url, monkeypatch)
    session_factory = create_session_factory(database_url)
    static_config = load_static_config()

    with session_scope(session_factory) as session:
        character_id = _create_character_context(
            session,
            spirit_stone=50000,
            materials={
                "enhancement_stone": 20,
                "wash_jade": 3,
                "seal_talisman": 2,
                "reforge_crystal": 2,
            },
        )
        service = EquipmentService(
            character_repository=SqlAlchemyCharacterRepository(session),
            equipment_repository=SqlAlchemyEquipmentRepository(session),
            inventory_repository=SqlAlchemyInventoryRepository(session),
            static_config=static_config,
        )

        generated = service.generate_equipment(
            character_id=character_id,
            slot_id="weapon",
            quality_id="legendary",
            rank_id="foundation",
            template_id="iron_sword",
            affix_count=2,
            seed=11,
        )
        assert generated.item.template_id == "iron_sword"
        assert generated.item.rank_id == "foundation"
        assert generated.item.rank_name == "三阶"
        assert generated.item.naming is not None
        assert generated.item.display_name == generated.item.naming.resolved_name
        assert generated.item.naming.naming_template_id in {
            "default_quality_template",
            "epic_masterwork",
            "legendary_masterwork",
        }
        assert len(generated.item.affixes) == 2
        assert generated.item.item_state == "active"

        enhanced = service.enhance_equipment(
            character_id=character_id,
            equipment_item_id=generated.item.item_id,
            seed=1,
        )
        assert enhanced.success is True
        assert enhanced.previous_level == 0
        assert enhanced.target_level == 1
        assert enhanced.item.enhancement_level == 1
        assert enhanced.item.rank_id == "foundation"
        assert len(enhanced.resource_changes) == 2
        assert {entry.resource_id for entry in enhanced.resource_changes} == {"spirit_stone", "enhancement_stone"}

        washed = service.wash_equipment(
            character_id=character_id,
            equipment_item_id=generated.item.item_id,
            locked_affix_indices=(0,),
            seed=77,
        )
        assert washed.locked_affix_indices == (0,)
        assert len(washed.rerolled_affixes) == 1
        assert washed.item.rank_id == enhanced.item.rank_id
        assert washed.item.affixes[0].affix_id == enhanced.item.affixes[0].affix_id
        assert washed.item.affixes[1].value != enhanced.item.affixes[1].value or washed.item.affixes[1].tier_id != enhanced.item.affixes[1].tier_id
        assert {entry.resource_id for entry in washed.resource_changes} == {"seal_talisman", "spirit_stone", "wash_jade"}

        reforged = service.reforge_equipment(
            character_id=character_id,
            equipment_item_id=generated.item.item_id,
            seed=9,
        )
        assert reforged.item.template_id != ""
        assert reforged.item.rank_id == washed.item.rank_id
        assert reforged.item.enhancement_level == washed.item.enhancement_level
        assert reforged.previous_template_id == washed.item.template_id
        assert len(reforged.previous_affixes) == len(washed.item.affixes)
        assert reforged.item.naming is not None
        assert reforged.item.display_name == reforged.item.naming.resolved_name
        assert {entry.resource_id for entry in reforged.resource_changes} == {"reforge_crystal", "spirit_stone"}

        detail = service.get_equipment_detail(character_id=character_id, equipment_item_id=generated.item.item_id)
        assert detail.item_id == generated.item.item_id
        assert detail.rank_id == "foundation"
        assert detail.display_name == reforged.item.display_name
        assert detail.enhancement_level == 1
        assert len(detail.resolved_stats) >= 1

        collection = service.list_equipment(character_id=character_id)
        assert [item.item_id for item in collection.active_items] == [generated.item.item_id]
        assert collection.equipped_items == ()
        assert collection.dismantled_items == ()


def test_artifact_nurture_and_dismantle_update_resources_and_state(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """应用服务应接通法宝培养、分解回收与分解审计。"""
    database_url = _build_sqlite_url(tmp_path / "equipment_service_artifact.db")
    _upgrade_database(database_url, monkeypatch)
    session_factory = create_session_factory(database_url)
    static_config = load_static_config()

    with session_scope(session_factory) as session:
        character_id = _create_character_context(
            session,
            spirit_stone=9000,
            materials={
                "artifact_essence": 12,
            },
        )
        service = EquipmentService(
            character_repository=SqlAlchemyCharacterRepository(session),
            equipment_repository=SqlAlchemyEquipmentRepository(session),
            inventory_repository=SqlAlchemyInventoryRepository(session),
            static_config=static_config,
        )

        generated = service.generate_equipment(
            character_id=character_id,
            slot_id="artifact",
            quality_id="epic",
            rank_id="tribulation",
            template_id="skyfire_mirror",
            affix_count=1,
            seed=5,
        )
        assert generated.item.is_artifact is True
        assert generated.item.rank_id == "tribulation"
        assert generated.item.naming is not None
        assert generated.item.display_name == generated.item.naming.resolved_name

        nurtured = service.nurture_artifact(
            character_id=character_id,
            equipment_item_id=generated.item.item_id,
        )
        assert nurtured.previous_level == 0
        assert nurtured.target_level == 1
        assert nurtured.item.artifact_nurture_level == 1
        assert nurtured.item.rank_id == "tribulation"
        assert {entry.resource_id for entry in nurtured.resource_changes} == {"artifact_essence", "spirit_stone"}

        dismantled = service.dismantle_equipment(
            character_id=character_id,
            equipment_item_id=generated.item.item_id,
            occurred_at=datetime(2026, 3, 26, 20, 0, 0),
            reason="integration_test",
            operator="pytest",
        )
        change_map = {entry.resource_id: entry.quantity for entry in dismantled.resource_changes}
        assert dismantled.item.item_state == "dismantled"
        assert dismantled.item.rank_id == "tribulation"
        assert dismantled.item.dismantled_at == datetime(2026, 3, 26, 20, 0, 0)
        assert any(entry.change_type == "grant" for entry in dismantled.resource_changes)
        assert change_map["artifact_essence"] >= 102
        assert change_map["spirit_sand"] == 143

        collection = service.list_equipment(character_id=character_id)
        assert collection.active_items == ()
        assert [item.item_id for item in collection.dismantled_items] == [generated.item.item_id]

        equipment_repo = SqlAlchemyEquipmentRepository(session)
        persisted_item = equipment_repo.get(generated.item.item_id)
        assert persisted_item is not None
        assert persisted_item.rank_id == "tribulation"
        assert persisted_item.dismantle_record is not None
        assert persisted_item.dismantle_record.audit_metadata_json["reason"] == "integration_test"
        assert persisted_item.dismantle_record.audit_metadata_json["operator"] == "pytest"


def test_enhance_requires_explicit_resource_availability(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """资源不足时应用服务应阻止强化并保持装备状态不变。"""
    database_url = _build_sqlite_url(tmp_path / "equipment_service_resource_guard.db")
    _upgrade_database(database_url, monkeypatch)
    session_factory = create_session_factory(database_url)
    static_config = load_static_config()

    with session_scope(session_factory) as session:
        character_id = _create_character_context(
            session,
            spirit_stone=100,
            materials={
                "enhancement_stone": 0,
            },
        )
        service = EquipmentService(
            character_repository=SqlAlchemyCharacterRepository(session),
            equipment_repository=SqlAlchemyEquipmentRepository(session),
            inventory_repository=SqlAlchemyInventoryRepository(session),
            static_config=static_config,
        )

        generated = service.generate_equipment(
            character_id=character_id,
            slot_id="weapon",
            quality_id="common",
            rank_id="mortal",
            template_id="iron_sword",
            affix_count=1,
            seed=3,
        )

        with pytest.raises(EquipmentResourceInsufficientError):
            service.enhance_equipment(
                character_id=character_id,
                equipment_item_id=generated.item.item_id,
                seed=1,
            )

        detail = service.get_equipment_detail(character_id=character_id, equipment_item_id=generated.item.item_id)
        assert detail.enhancement_level == 0
        assert detail.rank_id == "mortal"


def test_current_attribute_service_applies_equipment_and_artifact_stats_by_scene(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """当前属性服务应把装备与法宝属性接入中性、PVE 与 PVP 场景。"""
    database_url = _build_sqlite_url(tmp_path / "equipment_current_attributes.db")
    _upgrade_database(database_url, monkeypatch)
    session_factory = create_session_factory(database_url)
    static_config = load_static_config()

    with session_scope(session_factory) as session:
        character_id = _create_character_context(
            session,
            spirit_stone=50000,
            materials={"artifact_essence": 16},
        )
        character_repository = SqlAlchemyCharacterRepository(session)
        equipment_repository = SqlAlchemyEquipmentRepository(session)
        inventory_repository = SqlAlchemyInventoryRepository(session)
        equipment_service = EquipmentService(
            character_repository=character_repository,
            equipment_repository=equipment_repository,
            inventory_repository=inventory_repository,
            static_config=static_config,
        )
        current_attribute_service = CurrentAttributeService(
            character_repository=character_repository,
            static_config=static_config,
        )
        base_neutral = current_attribute_service.get_neutral_view(character_id=character_id)

        weapon = equipment_service.generate_equipment(
            character_id=character_id,
            slot_id="weapon",
            quality_id="rare",
            rank_id="foundation",
            template_id="spirit_blade",
            affix_count=1,
            seed=7,
        )
        armor = equipment_service.generate_equipment(
            character_id=character_id,
            slot_id="armor",
            quality_id="rare",
            rank_id="foundation",
            template_id="cloud_robe",
            affix_count=1,
            seed=11,
        )
        accessory = equipment_service.generate_equipment(
            character_id=character_id,
            slot_id="accessory",
            quality_id="rare",
            rank_id="foundation",
            template_id="jade_ring",
            affix_count=1,
            seed=6,
        )
        artifact = equipment_service.generate_equipment(
            character_id=character_id,
            slot_id="artifact",
            quality_id="rare",
            rank_id="foundation",
            template_id="skyfire_mirror",
            affix_count=1,
            seed=0,
        )

        equipment_service.equip_item(character_id=character_id, equipment_item_id=weapon.item.item_id)
        equipment_service.equip_item(character_id=character_id, equipment_item_id=armor.item.item_id)
        equipment_service.equip_item(character_id=character_id, equipment_item_id=accessory.item.item_id)
        equipment_service.equip_item(character_id=character_id, equipment_item_id=artifact.item.item_id)
        equipment_service.nurture_artifact(character_id=character_id, equipment_item_id=artifact.item.item_id)

        accessory_model = equipment_repository.get(accessory.item.item_id)
        assert accessory_model is not None
        accessory_model.affixes[0].stat_id = "damage_bonus_permille"
        accessory_model.affixes[0].value = 40
        accessory_model.affixes[0].is_pve_specialized = True
        accessory_model.affixes[0].is_pvp_specialized = False
        equipment_repository.save(accessory_model)

        armor_model = equipment_repository.get(armor.item.item_id)
        assert armor_model is not None
        armor_model.affixes[0].stat_id = "control_resist_permille"
        armor_model.affixes[0].value = 42
        armor_model.affixes[0].is_pve_specialized = False
        armor_model.affixes[0].is_pvp_specialized = True
        equipment_repository.save(armor_model)

        session.expire_all()
        neutral = current_attribute_service.get_neutral_view(character_id=character_id)
        pve = current_attribute_service.get_pve_view(character_id=character_id)
        pvp = current_attribute_service.get_pvp_view(character_id=character_id)

        assert neutral.attack_power > base_neutral.attack_power
        assert neutral.speed > base_neutral.speed
        assert neutral.max_hp > base_neutral.max_hp
        assert pve.damage_bonus_permille > neutral.damage_bonus_permille
        assert pvp.damage_bonus_permille == neutral.damage_bonus_permille
        assert pvp.control_resist_permille > neutral.control_resist_permille
        assert pve.control_resist_permille == neutral.control_resist_permille
