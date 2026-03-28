"""阶段 13 特殊词条首发表收口测试。"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from alembic import command
from alembic.config import Config
import pytest

from application.character.current_attribute_service import CurrentAttributeService
from application.equipment import EquipmentService
from application.pvp.defense_snapshot_service import PvpDefenseSnapshotService
from application.ranking import CharacterScoreService
from domain.battle import BattleSide, BattleSnapshot, BattleTemplateParser
from domain.battle.models import BattleEventPhase, BattleUnitSnapshot
from domain.battle.settlement import BattleRuntimeContext, SeededBattleRandomSource
from domain.battle.special_effects import (
    BattleSpecialEffectHook,
    BattleSpecialEffectRegistry,
)
from infrastructure.config.static import load_static_config
from infrastructure.db.models import Character, CharacterProgress, CurrencyBalance, EquipmentAffix, InventoryItem, Player
from infrastructure.db.repositories import (
    SqlAlchemyCharacterRepository,
    SqlAlchemyCharacterScoreSnapshotRepository,
    SqlAlchemyEquipmentRepository,
    SqlAlchemyInventoryRepository,
    SqlAlchemyPlayerRepository,
    SqlAlchemySnapshotRepository,
)
from infrastructure.db.session import create_session_factory, session_scope

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _build_sqlite_url(database_path: Path) -> str:
    return f"sqlite+pysqlite:///{database_path.as_posix()}"


def _upgrade_database(database_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(PROJECT_ROOT)
    monkeypatch.setenv("DATABASE_URL", database_url)
    command.upgrade(Config((PROJECT_ROOT / "alembic.ini").as_posix()), "head")


def _create_character_context(session, *, spirit_stone: int = 0, materials: dict[str, int] | None = None) -> int:
    player_repo = SqlAlchemyPlayerRepository(session)
    character_repo = SqlAlchemyCharacterRepository(session)
    inventory_repo = SqlAlchemyInventoryRepository(session)

    player = player_repo.add(Player(discord_user_id="50101", display_name="特效器修"))
    character = character_repo.add(
        Character(
            player_id=player.id,
            name="玄钧",
            title="百炼",
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


def _build_services(session, static_config):
    character_repository = SqlAlchemyCharacterRepository(session)
    equipment_repository = SqlAlchemyEquipmentRepository(session)
    inventory_repository = SqlAlchemyInventoryRepository(session)
    score_snapshot_repository = SqlAlchemyCharacterScoreSnapshotRepository(session)
    score_service = CharacterScoreService(
        character_repository=character_repository,
        score_snapshot_repository=score_snapshot_repository,
        static_config=static_config,
    )
    equipment_service = EquipmentService(
        character_repository=character_repository,
        equipment_repository=equipment_repository,
        inventory_repository=inventory_repository,
        static_config=static_config,
        score_service=score_service,
    )
    current_attribute_service = CurrentAttributeService(
        character_repository=character_repository,
        static_config=static_config,
    )
    defense_snapshot_service = PvpDefenseSnapshotService(
        character_repository=character_repository,
        snapshot_repository=SqlAlchemySnapshotRepository(session),
        current_attribute_service=current_attribute_service,
        static_config=static_config,
    )
    return (
        equipment_service,
        current_attribute_service,
        score_service,
        defense_snapshot_service,
        equipment_repository,
        character_repository,
    )


def _collect_special_effect_ids(affixes) -> set[str]:
    return {
        affix.special_effect.effect_id
        for affix in affixes
        if affix.special_effect is not None
    }


def _find_unit_state(context: BattleRuntimeContext, unit_id: str):
    return context.get_unit(unit_id)


def _find_effect_state(unit, effect_id: str):
    return next(effect for effect in unit.ordered_special_effects() if effect.effect_id == effect_id)


def _append_launch_special_effect_affix(
    equipment_model,
    *,
    static_config,
    effect_id: str,
    position: int,
    payload_overrides: dict[str, int | bool] | None = None,
) -> None:
    effect_definition = static_config.equipment.get_special_effect(effect_id)
    assert effect_definition is not None
    affix_definition = next(
        affix
        for affix in static_config.equipment.affixes
        if affix.special_effect_id == effect_id
    )
    tier_definition = static_config.equipment.get_affix_tier("earth")
    assert tier_definition is not None
    payload = dict(effect_definition.payload)
    if payload_overrides is not None:
        payload.update(payload_overrides)
    equipment_model.affixes.append(
        EquipmentAffix(
            position=position,
            affix_id=affix_definition.affix_id,
            affix_name=affix_definition.name,
            stat_id="" if affix_definition.stat_id is None else affix_definition.stat_id,
            category=affix_definition.category,
            tier_id=tier_definition.tier_id,
            tier_name=tier_definition.name,
            roll_value=Decimal("1.3000"),
            value=0,
            affix_kind="special_effect",
            special_effect_id=effect_definition.effect_id,
            special_effect_name=effect_definition.name,
            special_effect_type=effect_definition.effect_type,
            trigger_event=effect_definition.trigger_event,
            special_effect_payload_json=payload,
            public_score_key=effect_definition.public_score_key,
            hidden_pvp_score_key=effect_definition.hidden_pvp_score_key,
            is_pve_specialized=False,
            is_pvp_specialized=False,
        )
    )


def _build_battle_runtime_context(
    *,
    character_id: int,
    current_attribute_service: CurrentAttributeService,
    template_parser: BattleTemplateParser,
    seed: int,
    ally_hp_ratio: Decimal = Decimal("1.0"),
    enemy_template_id: str = "manhuang_body",
    enemy_shield: int = 0,
) -> tuple[BattleRuntimeContext, str, str]:
    ally_unit_id = f"character:{character_id}"
    enemy_unit_id = "enemy:test"
    ally_view = current_attribute_service.get_pve_view(character_id=character_id)
    enemy_view = current_attribute_service.get_pve_view(character_id=character_id)
    ally_snapshot = ally_view.build_battle_unit_snapshot(
        unit_id=ally_unit_id,
        unit_name="玄钧",
        side=BattleSide.ALLY,
        current_hp_ratio=ally_hp_ratio,
    )
    enemy_snapshot = enemy_view.build_battle_unit_snapshot(
        unit_id=enemy_unit_id,
        unit_name="木桩",
        side=BattleSide.ENEMY,
        runtime_template_id=enemy_template_id,
        current_shield=enemy_shield,
    )
    enemy_snapshot = replace(enemy_snapshot, special_effect_payloads=())
    snapshot = BattleSnapshot(
        seed=seed,
        allies=(ally_snapshot,),
        enemies=(enemy_snapshot,),
        round_limit=2,
        environment_tags=("unit_test",),
    )
    compiled_templates = {
        "wenxin_sword": template_parser.parse_template(path_id="wenxin_sword"),
        "manhuang_body": template_parser.parse_template(path_id="manhuang_body"),
    }
    registry = BattleSpecialEffectRegistry()
    context = BattleRuntimeContext.from_snapshot(
        snapshot=snapshot,
        behavior_templates=compiled_templates,
        random_source=SeededBattleRandomSource(seed=seed),
        special_effect_registry=registry,
    )
    return context, ally_unit_id, enemy_unit_id


def test_launch_special_affix_config_and_generation_ranges(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    database_url = _build_sqlite_url(tmp_path / "stage13_special_affix_generation.db")
    _upgrade_database(database_url, monkeypatch)
    session_factory = create_session_factory(database_url)
    static_config = load_static_config()

    expected_effects = {
        "sunder_on_hit": ("weapon", "se_sunder_on_hit", "pvp_se_sunder_on_hit"),
        "dot_on_hit": ("weapon", "se_dot_on_hit", "pvp_se_dot_on_hit"),
        "battle_start_barrier": ("armor", "se_battle_start_barrier", "pvp_se_battle_start_barrier"),
        "barrier_on_damage_taken": ("armor", "se_barrier_on_damage_taken", "pvp_se_barrier_on_damage_taken"),
        "low_hp_regen": ("armor", "se_low_hp_regen", "pvp_se_low_hp_regen"),
        "heal_after_attack": ("accessory", "se_heal_after_attack", "pvp_se_heal_after_attack"),
        "round_end_barrier_if_empty": (
            "accessory",
            "se_round_end_barrier_if_empty",
            "pvp_se_round_end_barrier_if_empty",
        ),
        "counter_sunder": ("artifact", "se_counter_sunder", "pvp_se_counter_sunder"),
        "damage_to_barrier": ("artifact", "se_damage_to_barrier", "pvp_se_damage_to_barrier"),
        "counter_dot": ("artifact", "se_counter_dot", "pvp_se_counter_dot"),
    }
    effect_by_id = {effect.effect_id: effect for effect in static_config.equipment.special_effects}
    assert set(effect_by_id) >= set(expected_effects)
    for effect_id, (slot_id, public_key, hidden_key) in expected_effects.items():
        effect = effect_by_id[effect_id]
        assert effect.public_score_key == public_key
        assert effect.hidden_pvp_score_key == hidden_key
        affix = next(
            item
            for item in static_config.equipment.affixes
            if item.special_effect_id == effect_id
        )
        assert affix.slot_ids == (slot_id,)
        assert affix.affix_kind == "special_effect"
        assert affix.tier_ids == ("earth", "heaven")

    pool_by_slot = {
        pool.slot_ids[0]: pool
        for pool in static_config.equipment.special_affix_generation.pools
    }
    assert set(pool_by_slot) == {"weapon", "armor", "accessory", "artifact"}
    for slot_id, pool in pool_by_slot.items():
        assert pool.quality_ids == ("epic", "legendary")
        assert pool.rank_ids == (
            "mortal",
            "qi_refining",
            "foundation",
            "core",
            "nascent_soul",
            "deity_transformation",
            "void_refinement",
            "body_integration",
            "great_vehicle",
            "tribulation",
        )
        assert all(
            static_config.equipment.get_affix(affix_id).special_effect_id in expected_effects
            for affix_id in pool.affix_ids
        )
        assert all(
            static_config.equipment.get_affix(affix_id).slot_ids == (slot_id,)
            for affix_id in pool.affix_ids
        )

    with session_scope(session_factory) as session:
        equipment_service, _, _, _, _, _ = _build_services(session, static_config)
        character_id = _create_character_context(session, spirit_stone=5000)

        weapon = equipment_service.generate_equipment(
            character_id=character_id,
            slot_id="weapon",
            quality_id="legendary",
            template_id="iron_sword",
            affix_count=1,
            seed=1,
        )
        armor = equipment_service.generate_equipment(
            character_id=character_id,
            slot_id="armor",
            quality_id="legendary",
            template_id="iron_armor",
            affix_count=1,
            seed=1,
        )
        accessory = equipment_service.generate_equipment(
            character_id=character_id,
            slot_id="accessory",
            quality_id="legendary",
            template_id="jade_ring",
            affix_count=1,
            seed=1,
        )
        artifact = equipment_service.generate_equipment(
            character_id=character_id,
            slot_id="artifact",
            quality_id="legendary",
            template_id="skyfire_mirror",
            affix_count=2,
            seed=1,
        )
        low_quality_weapon = equipment_service.generate_equipment(
            character_id=character_id,
            slot_id="weapon",
            quality_id="rare",
            template_id="iron_sword",
            affix_count=1,
            seed=1,
        )

        assert _collect_special_effect_ids(weapon.item.affixes) <= {"sunder_on_hit", "dot_on_hit"}
        assert _collect_special_effect_ids(armor.item.affixes) <= {
            "battle_start_barrier",
            "barrier_on_damage_taken",
            "low_hp_regen",
        }
        assert _collect_special_effect_ids(accessory.item.affixes) <= {
            "heal_after_attack",
            "round_end_barrier_if_empty",
        }
        artifact_special_effect_ids = _collect_special_effect_ids(artifact.item.affixes)
        assert artifact_special_effect_ids <= {"counter_sunder", "damage_to_barrier", "counter_dot"}
        assert len(artifact_special_effect_ids) == len([affix for affix in artifact.item.affixes if affix.special_effect is not None])
        assert _collect_special_effect_ids(low_quality_weapon.item.affixes) == set()


def test_special_effect_display_and_snapshot_chain(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    database_url = _build_sqlite_url(tmp_path / "stage13_special_affix_display.db")
    _upgrade_database(database_url, monkeypatch)
    session_factory = create_session_factory(database_url)
    static_config = load_static_config()

    with session_scope(session_factory) as session:
        (
            equipment_service,
            current_attribute_service,
            score_service,
            defense_snapshot_service,
            equipment_repository,
            _,
        ) = _build_services(session, static_config)
        character_id = _create_character_context(session, spirit_stone=5000)

        generated = equipment_service.generate_equipment(
            character_id=character_id,
            slot_id="artifact",
            quality_id="legendary",
            template_id="skyfire_mirror",
            affix_count=1,
            seed=3,
        )
        equipment_model = equipment_repository.get(generated.item.item_id)
        assert equipment_model is not None
        _append_launch_special_effect_affix(
            equipment_model,
            static_config=static_config,
            effect_id="damage_to_barrier",
            position=len(equipment_model.affixes) + 1,
        )
        equipment_repository.save(equipment_model)
        equipment_service.equip_item(character_id=character_id, equipment_item_id=generated.item.item_id)

        detail = equipment_service.get_equipment_detail(character_id=character_id, equipment_item_id=generated.item.item_id)
        effect_snapshot = next(
            affix.special_effect
            for affix in detail.affixes
            if affix.special_effect is not None and affix.special_effect.effect_id == "damage_to_barrier"
        )
        assert effect_snapshot is not None
        assert effect_snapshot.public_score_key == "se_damage_to_barrier"
        assert effect_snapshot.hidden_pvp_score_key == "pvp_se_damage_to_barrier"
        assert effect_snapshot.payload["damage_ratio_permille"] == 150

        current_view = current_attribute_service.get_pvp_view(character_id=character_id)
        payload_by_effect_id = {
            payload["effect_id"]: payload
            for payload in current_view.special_effect_payloads
        }
        assert payload_by_effect_id["damage_to_barrier"]["public_score_key"] == "se_damage_to_barrier"
        assert payload_by_effect_id["damage_to_barrier"]["hidden_pvp_score_key"] == "pvp_se_damage_to_barrier"

        score_service.refresh_character_score(character_id=character_id)
        bundle = defense_snapshot_service.ensure_snapshot(
            character_id=character_id,
            now=datetime(2026, 1, 1, 0, 0, 0),
            requested_reason="defense_on_demand",
        )
        defender_payload_by_effect_id = {
            payload["effect_id"]: payload
            for payload in bundle.battle_unit_snapshot.special_effect_payloads
        }
        assert defender_payload_by_effect_id["damage_to_barrier"]["payload"] == dict(effect_snapshot.payload)


@pytest.mark.parametrize(
    ("effect_id", "slot_id", "template_id"),
    [
        ("sunder_on_hit", "weapon", "spirit_blade"),
        ("dot_on_hit", "weapon", "spirit_blade"),
        ("battle_start_barrier", "armor", "iron_armor"),
        ("barrier_on_damage_taken", "armor", "iron_armor"),
        ("low_hp_regen", "armor", "iron_armor"),
        ("heal_after_attack", "accessory", "jade_ring"),
        ("round_end_barrier_if_empty", "accessory", "jade_ring"),
        ("counter_sunder", "artifact", "skyfire_mirror"),
        ("damage_to_barrier", "artifact", "skyfire_mirror"),
        ("counter_dot", "artifact", "skyfire_mirror"),
    ],
)
def test_battle_runtime_launch_special_effects(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    effect_id: str,
    slot_id: str,
    template_id: str,
) -> None:
    database_url = _build_sqlite_url(tmp_path / f"stage13_special_affix_battle_{effect_id}.db")
    _upgrade_database(database_url, monkeypatch)
    session_factory = create_session_factory(database_url)
    static_config = load_static_config()
    template_parser = BattleTemplateParser(
        template_config=static_config.battle_templates,
        skill_path_config=static_config.skill_paths,
    )

    with session_scope(session_factory) as session:
        equipment_service, current_attribute_service, _, _, equipment_repository, _ = _build_services(session, static_config)
        character_id = _create_character_context(session, spirit_stone=5000)

        generated = equipment_service.generate_equipment(
            character_id=character_id,
            slot_id=slot_id,
            quality_id="legendary",
            template_id=template_id,
            affix_count=0,
            seed=11,
        )
        equipment_model = equipment_repository.get(generated.item.item_id)
        assert equipment_model is not None
        _append_launch_special_effect_affix(
            equipment_model,
            static_config=static_config,
            effect_id=effect_id,
            position=len(equipment_model.affixes) + 1,
            payload_overrides={"trigger_rate_permille": 1000},
        )
        equipment_model.equipped_slot_id = slot_id
        equipment_repository.save(equipment_model)

        ally_hp_ratio = Decimal("0.40") if effect_id in {"low_hp_regen", "heal_after_attack"} else Decimal("1.0")
        enemy_template_id = "wenxin_sword" if effect_id in {"counter_sunder", "counter_dot", "barrier_on_damage_taken", "low_hp_regen"} else "manhuang_body"
        context, ally_unit_id, enemy_unit_id = _build_battle_runtime_context(
            character_id=character_id,
            current_attribute_service=current_attribute_service,
            template_parser=template_parser,
            seed=77,
            ally_hp_ratio=ally_hp_ratio,
            enemy_template_id=enemy_template_id,
        )
        registry = context.special_effect_registry

        if effect_id == "battle_start_barrier":
            registry.dispatch(
                hook=BattleSpecialEffectHook.BATTLE_START,
                runtime_context=context,
                owner_unit_id=ally_unit_id,
                actor_unit_id=ally_unit_id,
            )
        elif effect_id == "low_hp_regen":
            registry.dispatch(
                hook=BattleSpecialEffectHook.TURN_START,
                runtime_context=context,
                owner_unit_id=ally_unit_id,
                actor_unit_id=ally_unit_id,
            )
        elif effect_id == "heal_after_attack":
            context.emit_event(
                phase=BattleEventPhase.SETTLEMENT,
                event_type="damage_resolved",
                actor_unit_id=ally_unit_id,
                target_unit_id=enemy_unit_id,
                action_id="test_action",
                detail_items=(("final_damage", 120),),
            )
            registry.dispatch(
                hook=BattleSpecialEffectHook.AFTER_ACTION,
                runtime_context=context,
                owner_unit_id=ally_unit_id,
                actor_unit_id=ally_unit_id,
                target_unit_id=enemy_unit_id,
                action_id="test_action",
            )
        elif effect_id == "round_end_barrier_if_empty":
            registry.dispatch(
                hook=BattleSpecialEffectHook.ROUND_END,
                runtime_context=context,
                owner_unit_id=ally_unit_id,
            )
        elif effect_id in {"sunder_on_hit", "dot_on_hit", "damage_to_barrier"}:
            registry.dispatch(
                hook=BattleSpecialEffectHook.DAMAGE_RESOLVED,
                runtime_context=context,
                owner_unit_id=ally_unit_id,
                actor_unit_id=ally_unit_id,
                target_unit_id=enemy_unit_id,
                action_id="test_action",
                resolved_value=120,
            )
        else:
            damage_taken_value = 500 if effect_id == "barrier_on_damage_taken" else 120
            registry.dispatch(
                hook=BattleSpecialEffectHook.DAMAGE_TAKEN,
                runtime_context=context,
                owner_unit_id=ally_unit_id,
                actor_unit_id=enemy_unit_id,
                target_unit_id=ally_unit_id,
                action_id="enemy_action",
                resolved_value=damage_taken_value,
            )

        ally_state = _find_unit_state(context, ally_unit_id)
        enemy_state = _find_unit_state(context, enemy_unit_id)
        effect_state = _find_effect_state(ally_state, effect_id)
        assert effect_state.triggers_used_this_battle >= 1
        assert any(event.event_type == "special_effect_triggered" for event in context.events)

        if effect_id == "sunder_on_hit":
            assert any(status.category.value == "attribute_suppression" for status in enemy_state.statuses)
        elif effect_id == "dot_on_hit":
            assert any(status.category.value == "damage_over_time" for status in enemy_state.statuses)
        elif effect_id == "battle_start_barrier":
            assert ally_state.current_shield > 0
        elif effect_id == "barrier_on_damage_taken":
            assert ally_state.current_shield > 0
        elif effect_id == "low_hp_regen":
            assert ally_state.current_hp > context.snapshot.allies[0].current_hp
        elif effect_id == "heal_after_attack":
            assert any(event.event_type == "healing_applied" and event.actor_unit_id == ally_unit_id for event in context.events)
        elif effect_id == "round_end_barrier_if_empty":
            assert ally_state.current_shield > 0
        elif effect_id == "counter_sunder":
            assert any(status.category.value == "attribute_suppression" for status in enemy_state.statuses)
        elif effect_id == "damage_to_barrier":
            assert ally_state.current_shield > 0
        elif effect_id == "counter_dot":
            assert any(status.category.value == "damage_over_time" for status in enemy_state.statuses)


def test_score_refresh_covers_launch_special_effect_keys(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    database_url = _build_sqlite_url(tmp_path / "stage13_special_affix_score.db")
    _upgrade_database(database_url, monkeypatch)
    session_factory = create_session_factory(database_url)
    static_config = load_static_config()

    expected_public_keys = {
        "se_sunder_on_hit",
        "se_dot_on_hit",
        "se_battle_start_barrier",
        "se_barrier_on_damage_taken",
        "se_low_hp_regen",
        "se_heal_after_attack",
        "se_round_end_barrier_if_empty",
        "se_counter_sunder",
        "se_damage_to_barrier",
        "se_counter_dot",
    }
    expected_hidden_keys = {
        "pvp_se_sunder_on_hit",
        "pvp_se_dot_on_hit",
        "pvp_se_battle_start_barrier",
        "pvp_se_barrier_on_damage_taken",
        "pvp_se_low_hp_regen",
        "pvp_se_heal_after_attack",
        "pvp_se_round_end_barrier_if_empty",
        "pvp_se_counter_sunder",
        "pvp_se_damage_to_barrier",
        "pvp_se_counter_dot",
    }
    effects_by_slot = {
        "weapon": ("sunder_on_hit", "dot_on_hit"),
        "armor": ("battle_start_barrier", "barrier_on_damage_taken", "low_hp_regen"),
        "accessory": ("heal_after_attack", "round_end_barrier_if_empty"),
        "artifact": ("counter_sunder", "damage_to_barrier", "counter_dot"),
    }

    with session_scope(session_factory) as session:
        equipment_service, _, score_service, _, equipment_repository, _ = _build_services(session, static_config)
        character_id = _create_character_context(session, spirit_stone=5000)

        generation_specs = (
            ("weapon", "legendary", "iron_sword", 1),
            ("armor", "legendary", "iron_armor", 2),
            ("accessory", "legendary", "jade_ring", 3),
            ("artifact", "legendary", "skyfire_mirror", 4),
        )
        for slot_id, quality_id, template_id, seed in generation_specs:
            generated = equipment_service.generate_equipment(
                character_id=character_id,
                slot_id=slot_id,
                quality_id=quality_id,
                template_id=template_id,
                affix_count=0,
                seed=seed,
            )
            equipment_service.equip_item(character_id=character_id, equipment_item_id=generated.item.item_id)
            equipment_model = equipment_repository.get(generated.item.item_id)
            assert equipment_model is not None
            next_position = len(equipment_model.affixes) + 1
            for effect_id in effects_by_slot[slot_id]:
                _append_launch_special_effect_affix(
                    equipment_model,
                    static_config=static_config,
                    effect_id=effect_id,
                    position=next_position,
                )
                next_position += 1
            equipment_repository.save(equipment_model)

        result = score_service.refresh_character_score(character_id=character_id)
        assert result.character_id == character_id
        assert result.hidden_pvp_score >= result.public_power_score

        public_keys = {
            entry["public_score_key"]
            for section_name in ("equipment", "artifact")
            for slot_entry in (
                result.breakdown[section_name]["slot_scores"]
                if section_name == "equipment"
                else result.breakdown[section_name]["artifact_scores"]
            )
            for entry in slot_entry["special_effects"]
        }
        hidden_keys = {
            entry["hidden_pvp_score_key"]
            for entry in result.breakdown["pvp_adjustment"]["special_effect_adjustments"]
        }
        assert public_keys == expected_public_keys
        assert hidden_keys == expected_hidden_keys
