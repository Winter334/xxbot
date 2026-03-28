"""阶段 2 数据模型、迁移与仓储集成测试。"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError

from infrastructure.db.models import (
    ArtifactProfile,
    BattleReport,
    BreakthroughTrialProgress,
    Character,
    CharacterProgress,
    CharacterSkillItem,
    CharacterSkillLoadout,
    CurrencyBalance,
    DropRecord,
    EndlessRunState,
    EquipmentAffix,
    EquipmentEnhancement,
    EquipmentItem,
    HealingState,
    InventoryItem,
    LeaderboardEntrySnapshot,
    LeaderboardSnapshot,
    Player,
    PvpDefenseSnapshot,
    RetreatState,
)
from infrastructure.db.repositories import (
    SqlAlchemyBattleRecordRepository,
    SqlAlchemyBreakthroughRepository,
    SqlAlchemyCharacterRepository,
    SqlAlchemyEquipmentRepository,
    SqlAlchemyInventoryRepository,
    SqlAlchemyPlayerRepository,
    SqlAlchemySkillRepository,
    SqlAlchemySnapshotRepository,
    SqlAlchemyStateRepository,
)
from infrastructure.db.session import create_engine_from_url, create_session_factory, session_scope

PROJECT_ROOT = Path(__file__).resolve().parents[3]
EXPECTED_STAGE2_TABLES = {
    "alembic_version",
    "system_markers",
    "players",
    "characters",
    "character_progress",
    "character_skill_items",
    "character_skill_loadouts",
    "currency_balances",
    "equipment_items",
    "equipment_enhancements",
    "equipment_affixes",
    "artifact_profiles",
    "inventory_items",
    "retreat_states",
    "healing_states",
    "endless_run_states",
    "breakthrough_trial_progress",
    "pvp_defense_snapshots",
    "leaderboard_snapshots",
    "leaderboard_entry_snapshots",
    "battle_reports",
    "drop_records",
}


def _build_sqlite_url(database_path: Path) -> str:
    """构造测试用 SQLite 地址。"""
    return f"sqlite+pysqlite:///{database_path.as_posix()}"


def _upgrade_database(database_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """将测试数据库升级到最新迁移版本。"""
    monkeypatch.chdir(PROJECT_ROOT)
    monkeypatch.setenv("DATABASE_URL", database_url)
    command.upgrade(Config((PROJECT_ROOT / "alembic.ini").as_posix()), "head")


def test_stage2_migration_creates_expected_tables(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """阶段迁移应创建全部业务表。"""
    database_url = _build_sqlite_url(tmp_path / "stage2_migration.db")

    _upgrade_database(database_url, monkeypatch)

    engine = create_engine_from_url(database_url)
    try:
        table_names = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()

    assert EXPECTED_STAGE2_TABLES.issubset(table_names)


def test_stage2_repositories_can_round_trip_core_entities(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """角色、功法、装备、进度、排行、快照与战报应能完整落库和读取。"""
    database_url = _build_sqlite_url(tmp_path / "stage2_round_trip.db")
    _upgrade_database(database_url, monkeypatch)

    session_factory = create_session_factory(database_url)
    now = datetime(2026, 3, 25, 22, 0, 0)

    with session_scope(session_factory) as session:
        player_repo = SqlAlchemyPlayerRepository(session)
        character_repo = SqlAlchemyCharacterRepository(session)
        skill_repo = SqlAlchemySkillRepository(session)
        equipment_repo = SqlAlchemyEquipmentRepository(session)
        inventory_repo = SqlAlchemyInventoryRepository(session)
        state_repo = SqlAlchemyStateRepository(session)
        breakthrough_repo = SqlAlchemyBreakthroughRepository(session)
        snapshot_repo = SqlAlchemySnapshotRepository(session)
        battle_record_repo = SqlAlchemyBattleRecordRepository(session)

        player = player_repo.add(Player(discord_user_id="10001", display_name="流云"))
        character = character_repo.add(
            Character(
                player_id=player.id,
                name="青玄",
                title="问道者",
                total_power_score=1200,
                public_power_score=1180,
                hidden_pvp_score=1110,
            )
        )
        character_id = character.id

        character_repo.save_progress(
            CharacterProgress(
                character_id=character_id,
                realm_id="foundation",
                stage_id="middle",
                cultivation_value=12345,
                comprehension_value=456,
                breakthrough_qualification_obtained=True,
                highest_endless_floor=27,
                current_hp_ratio=Decimal("0.8750"),
                current_mp_ratio=Decimal("0.6400"),
            )
        )

        main_skill = skill_repo.add_skill_item(
            CharacterSkillItem(
                character_id=character_id,
                lineage_id="seven_kill_sword",
                path_id="wenxin_sword",
                axis_id="sword",
                skill_type="main",
                auxiliary_slot_id=None,
                skill_name="七杀剑诀",
                rank_id="foundation",
                rank_name="三阶",
                rank_order=3,
                quality_id="superior",
                quality_name="上品",
                total_budget=18,
                budget_distribution_json={"attack_power": 8, "speed": 4, "max_hp": 6},
                resolved_attributes_json={"attack_power": 80, "speed": 12, "max_hp": 180},
                resolved_patches_json=[{"patch_id": "main_burst_damage_up", "value": 120}],
                source_type="drop",
                source_record_id="endless_floor_27",
                is_locked=True,
                item_state="equipped",
                equipped_at=now,
            )
        )
        guard_skill = skill_repo.add_skill_item(
            CharacterSkillItem(
                character_id=character_id,
                lineage_id="golden_bell_guard",
                path_id="wenxin_sword",
                axis_id="sword",
                skill_type="auxiliary",
                auxiliary_slot_id="guard",
                skill_name="金钟护元诀",
                rank_id="foundation",
                rank_name="三阶",
                rank_order=3,
                quality_id="good",
                quality_name="良品",
                total_budget=7,
                budget_distribution_json={"guard_power": 4, "max_hp": 3},
                resolved_attributes_json={"guard_power": 45, "max_hp": 90},
                resolved_patches_json=[{"patch_id": "guard_shield_bonus", "value": 45}],
                source_type="drop",
                source_record_id="endless_floor_27",
                is_locked=False,
                item_state="equipped",
                equipped_at=now,
            )
        )
        movement_skill = skill_repo.add_skill_item(
            CharacterSkillItem(
                character_id=character_id,
                lineage_id="wind_shadow_step",
                path_id="wenxin_sword",
                axis_id="sword",
                skill_type="auxiliary",
                auxiliary_slot_id="movement",
                skill_name="风影步",
                rank_id="foundation",
                rank_name="三阶",
                rank_order=3,
                quality_id="good",
                quality_name="良品",
                total_budget=6,
                budget_distribution_json={"speed": 4, "dodge_rate_permille": 2},
                resolved_attributes_json={"speed": 16, "dodge_rate_permille": 40},
                resolved_patches_json=[{"patch_id": "movement_speed_bonus", "value": 18}],
                source_type="drop",
                source_record_id="endless_floor_27",
                is_locked=False,
                item_state="equipped",
                equipped_at=now,
            )
        )
        spirit_skill = skill_repo.add_skill_item(
            CharacterSkillItem(
                character_id=character_id,
                lineage_id="sword_heart_lock",
                path_id="wenxin_sword",
                axis_id="sword",
                skill_type="auxiliary",
                auxiliary_slot_id="spirit",
                skill_name="锁念剑心诀",
                rank_id="foundation",
                rank_name="三阶",
                rank_order=3,
                quality_id="ordinary",
                quality_name="凡品",
                total_budget=5,
                budget_distribution_json={"crit_rate_permille": 3, "speed": 2},
                resolved_attributes_json={"crit_rate_permille": 35, "speed": 8},
                resolved_patches_json=[{"patch_id": "spirit_control_hit_bonus", "value": 40}],
                source_type="drop",
                source_record_id="endless_floor_27",
                is_locked=False,
                item_state="equipped",
                equipped_at=now,
            )
        )
        backpack_skill = skill_repo.add_skill_item(
            CharacterSkillItem(
                character_id=character_id,
                lineage_id="formless_heart_sword",
                path_id="wenxin_sword",
                axis_id="sword",
                skill_type="main",
                auxiliary_slot_id=None,
                skill_name="无相心剑篇",
                rank_id="core",
                rank_name="四阶",
                rank_order=4,
                quality_id="rare",
                quality_name="珍品",
                total_budget=27,
                budget_distribution_json={"attack_power": 10, "crit_rate_permille": 8, "speed": 9},
                resolved_attributes_json={"attack_power": 110, "crit_rate_permille": 75, "speed": 20},
                resolved_patches_json=[{"patch_id": "main_precision_bonus", "value": 40}],
                source_type="drop",
                source_record_id="boss_trial_01",
                is_locked=False,
                item_state="inventory",
                equipped_at=None,
            )
        )

        skill_repo.save_skill_loadout(
            CharacterSkillLoadout(
                character_id=character_id,
                main_skill_id=main_skill.id,
                guard_skill_id=guard_skill.id,
                movement_skill_id=movement_skill.id,
                spirit_skill_id=spirit_skill.id,
                main_axis_id="sword",
                main_path_id="wenxin_sword",
                behavior_template_id="wenxin_sword",
                config_version="1.0.0",
                loadout_notes_json={"combat_style": "burst"},
            )
        )
        character_repo.save_currency_balance(
            CurrencyBalance(character_id=character_id, spirit_stone=5000, honor_coin=80)
        )

        weapon = EquipmentItem(
            character_id=character_id,
            slot_id="weapon",
            equipped_slot_id="weapon",
            quality_id="epic",
            item_name="青霜剑",
            base_snapshot_json={"attack": 120},
        )
        weapon.enhancement = EquipmentEnhancement(enhancement_level=7, success_count=7, failure_count=2)
        weapon.affixes.extend(
            [
                EquipmentAffix(
                    position=1,
                    affix_id="attack_power",
                    tier_id="earth",
                    roll_value=Decimal("1.4200"),
                ),
                EquipmentAffix(
                    position=2,
                    affix_id="penetration",
                    tier_id="mystic",
                    roll_value=Decimal("1.1800"),
                ),
            ]
        )
        equipment_repo.add(weapon)

        artifact = EquipmentItem(
            character_id=character_id,
            slot_id="artifact",
            equipped_slot_id="artifact",
            quality_id="legendary",
            item_name="玄火印",
            base_snapshot_json={"shield": 80},
        )
        artifact.enhancement = EquipmentEnhancement(enhancement_level=4, success_count=4, failure_count=1)
        artifact.affixes.append(
            EquipmentAffix(
                position=1,
                affix_id="shield_power",
                tier_id="heaven",
                roll_value=Decimal("1.6300"),
            )
        )
        artifact.artifact_profile = ArtifactProfile(
            artifact_template_id="flame_seal",
            refinement_level=3,
            core_effect_snapshot_json={"shield_bonus": 25},
        )
        equipment_repo.add(artifact)

        inventory_repo.upsert_item(
            InventoryItem(
                character_id=character_id,
                item_type="material",
                item_id="enhancement_stone",
                quantity=24,
                item_payload_json={"bound": True},
            )
        )

        state_repo.save_retreat_state(
            RetreatState(
                character_id=character_id,
                status="running",
                started_at=now,
                scheduled_end_at=now + timedelta(hours=6),
                settled_at=None,
                context_json={"realm_id": "foundation"},
            )
        )
        state_repo.save_healing_state(
            HealingState(
                character_id=character_id,
                status="running",
                injury_level="moderate",
                started_at=now,
                scheduled_end_at=now + timedelta(hours=2),
                settled_at=None,
                context_json={"injury_ratio": "0.35"},
            )
        )
        state_repo.save_endless_run_state(
            EndlessRunState(
                character_id=character_id,
                status="running",
                selected_start_floor=20,
                current_floor=27,
                highest_floor_reached=27,
                current_node_type="normal",
                last_region_bias_id="flame",
                last_enemy_template_id="guardian",
                run_seed=42,
                started_at=now,
                pending_rewards_json={
                    "version": 1,
                    "stable_totals": {"cultivation": 120, "insight": 12, "refining_essence": 3},
                    "pending_totals": {"equipment_score": 36, "artifact_score": 6, "dao_pattern_score": 4},
                    "last_reward_floor": 26,
                },
                run_snapshot_json={
                    "has_active_run": True,
                    "selected_start_floor": 20,
                    "current_floor": 27,
                    "current_node_type": "normal",
                    "run_seed": 42,
                    "current_region": {"region_id": "flame", "region_bias_id": "flame"},
                    "anchor_status": {
                        "highest_unlocked_anchor_floor": 20,
                        "available_start_floors": [1, 10, 20],
                        "selected_start_floor": 20,
                        "selected_start_floor_unlocked": True,
                        "current_anchor_floor": 20,
                        "next_anchor_floor": 30,
                    },
                    "encounter_history": [{"floor": 27, "node_type": "normal"}],
                },
            )
        )

        breakthrough_repo.save_progress(
            BreakthroughTrialProgress(
                character_id=character_id,
                mapping_id="foundation_to_core",
                group_id="entry_trials",
                status="cleared",
                attempt_count=3,
                best_clear_at=now,
                last_result_json={"boss_template_id": "caster"},
            )
        )

        snapshot_repo.add_pvp_defense_snapshot(
            PvpDefenseSnapshot(
                character_id=character_id,
                snapshot_version=1,
                power_score=1180,
                public_power_score=1180,
                hidden_pvp_score=1110,
                score_version="stage12.v1",
                snapshot_reason="test_seed",
                build_fingerprint="stage2-round-trip-v1",
                rank_position=8,
                formation_json={"main_path_id": "wenxin_sword"},
                stats_json={"hp": 820, "speed": 145},
                summary_json={"character_name": "青玄", "main_path_id": "wenxin_sword"},
                source_updated_at=now,
                lock_started_at=now,
                lock_expires_at=now + timedelta(hours=24),
            )
        )
        snapshot_repo.add_pvp_defense_snapshot(
            PvpDefenseSnapshot(
                character_id=character_id,
                snapshot_version=2,
                power_score=1200,
                public_power_score=1180,
                hidden_pvp_score=1110,
                score_version="stage12.v1",
                snapshot_reason="test_refresh",
                build_fingerprint="stage2-round-trip-v2",
                rank_position=6,
                formation_json={"main_path_id": "wenxin_sword"},
                stats_json={"hp": 850, "speed": 150},
                summary_json={"character_name": "青玄", "main_path_id": "wenxin_sword"},
                source_updated_at=now + timedelta(minutes=10),
                lock_started_at=now + timedelta(minutes=10),
                lock_expires_at=now + timedelta(hours=24, minutes=10),
            )
        )

        leaderboard_snapshot = LeaderboardSnapshot(board_type="power", scope_json={"season": "launch"})
        leaderboard_snapshot.entries.append(
            LeaderboardEntrySnapshot(
                character_id=character_id,
                rank_position=1,
                score=1200,
                summary_json={"realm_id": "foundation", "stage_id": "middle"},
            )
        )
        snapshot_repo.add_leaderboard_snapshot(leaderboard_snapshot)

        battle_report = battle_record_repo.add_battle_report(
            BattleReport(
                character_id=character_id,
                battle_type="endless",
                result="victory",
                opponent_ref="guardian",
                summary_json={"floor": 27},
                detail_log_json={"rounds": [{"round": 1, "action": "slash"}]},
            )
        )
        battle_record_repo.add_drop_record(
            DropRecord(
                character_id=character_id,
                battle_report_id=battle_report.id,
                source_type="endless",
                source_ref="floor_27",
                items_json=[{"item_id": "enhancement_stone", "quantity": 2}],
                currencies_json={"spirit_stone": 120},
            )
        )

    with session_scope(session_factory) as session:
        character_repo = SqlAlchemyCharacterRepository(session)
        skill_repo = SqlAlchemySkillRepository(session)
        equipment_repo = SqlAlchemyEquipmentRepository(session)
        inventory_repo = SqlAlchemyInventoryRepository(session)
        state_repo = SqlAlchemyStateRepository(session)
        breakthrough_repo = SqlAlchemyBreakthroughRepository(session)
        snapshot_repo = SqlAlchemySnapshotRepository(session)
        battle_record_repo = SqlAlchemyBattleRecordRepository(session)

        aggregate = character_repo.get_aggregate(character_id)
        assert aggregate is not None
        assert aggregate.player.discord_user_id == "10001"
        assert aggregate.character.name == "青玄"
        assert aggregate.progress is not None
        assert aggregate.progress.highest_endless_floor == 27
        assert aggregate.progress.breakthrough_qualification_obtained is True
        assert aggregate.skill_loadout is not None
        assert aggregate.skill_loadout.main_path_id == "wenxin_sword"
        assert aggregate.skill_loadout.main_skill_id == main_skill.id
        assert aggregate.skill_loadout.guard_skill_id == guard_skill.id
        assert aggregate.skill_loadout.body_method_id == guard_skill.id
        assert aggregate.currency_balance is not None
        assert aggregate.currency_balance.spirit_stone == 5000

        skill_items = skill_repo.list_skill_items_by_character_id(character_id)
        assert len(skill_items) == 5
        assert skill_repo.get_skill_item(main_skill.id) is not None
        assert skill_repo.get_skill_item(main_skill.id).skill_name == "七杀剑诀"
        assert skill_repo.get_skill_item_by_character_and_id(character_id, backpack_skill.id) is not None
        assert skill_repo.get_skill_item_by_character_and_id(character_id, backpack_skill.id).item_state == "inventory"
        assert skill_repo.get_skill_loadout(character_id) is not None
        assert skill_repo.get_skill_loadout(character_id).movement_skill_id == movement_skill.id

        equipment_items = equipment_repo.list_by_character_id(character_id)
        assert len(equipment_items) == 2
        equipped_items = equipment_repo.list_equipped_by_character_id(character_id)
        assert {item.equipped_slot_id for item in equipped_items} == {"weapon", "artifact"}

        loaded_weapon = next(item for item in equipment_items if item.slot_id == "weapon")
        assert loaded_weapon.enhancement is not None
        assert loaded_weapon.enhancement.enhancement_level == 7
        assert [affix.affix_id for affix in loaded_weapon.affixes] == ["attack_power", "penetration"]

        loaded_artifact = next(item for item in equipment_items if item.slot_id == "artifact")
        assert loaded_artifact.artifact_profile is not None
        assert loaded_artifact.artifact_profile.refinement_level == 3

        inventory_items = inventory_repo.list_by_character_id(character_id)
        assert len(inventory_items) == 1
        assert inventory_items[0].quantity == 24

        retreat_state = state_repo.get_retreat_state(character_id)
        assert retreat_state is not None
        assert retreat_state.status == "running"

        healing_state = state_repo.get_healing_state(character_id)
        assert healing_state is not None
        assert healing_state.injury_level == "moderate"

        endless_run_state = state_repo.get_endless_run_state(character_id)
        assert endless_run_state is not None
        assert endless_run_state.selected_start_floor == 20
        assert endless_run_state.current_floor == 27
        assert endless_run_state.current_node_type == "normal"
        assert endless_run_state.run_seed == 42
        assert endless_run_state.pending_rewards_json["stable_totals"]["cultivation"] == 120
        assert endless_run_state.run_snapshot_json["anchor_status"]["current_anchor_floor"] == 20
        assert endless_run_state.run_snapshot_json["encounter_history"][0]["floor"] == 27
        assert state_repo.has_running_endless_run(character_id) is True

        breakthrough_progress = breakthrough_repo.get_progress(character_id, "foundation_to_core")
        assert breakthrough_progress is not None
        assert breakthrough_progress.status == "cleared"

        latest_pvp_snapshot = snapshot_repo.get_latest_pvp_defense_snapshot(character_id)
        assert latest_pvp_snapshot is not None
        assert latest_pvp_snapshot.snapshot_version == 2
        assert latest_pvp_snapshot.rank_position == 6

        latest_leaderboard = snapshot_repo.get_latest_leaderboard("power")
        assert latest_leaderboard is not None
        assert len(latest_leaderboard.entries) == 1
        assert latest_leaderboard.entries[0].rank_position == 1
        assert latest_leaderboard.entries[0].character_id == character_id
        assert latest_leaderboard.entries[0].summary_json["realm_id"] == "foundation"

        battle_reports = battle_record_repo.list_battle_reports(character_id)
        assert len(battle_reports) == 1
        assert battle_reports[0].result == "victory"

        drop_records = battle_record_repo.list_drop_records(character_id)
        assert len(drop_records) == 1
        assert drop_records[0].currencies_json["spirit_stone"] == 120
        assert drop_records[0].items_json[0]["item_id"] == "enhancement_stone"


def test_stage2_unique_constraints_keep_core_relations_unambiguous(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """玩家角色一对一与装备位唯一约束应生效。"""
    database_url = _build_sqlite_url(tmp_path / "stage2_constraints.db")
    _upgrade_database(database_url, monkeypatch)

    session_factory = create_session_factory(database_url)

    with session_scope(session_factory) as session:
        player = Player(discord_user_id="20001", display_name="寒山")
        session.add(player)
        session.flush()
        session.add(
            Character(
                player_id=player.id,
                name="玄岳",
                total_power_score=900,
                public_power_score=900,
                hidden_pvp_score=880,
            )
        )

    session = session_factory()
    try:
        player = session.scalar(select(Player).where(Player.discord_user_id == "20001"))
        assert player is not None
        session.add(
            Character(
                player_id=player.id,
                name="重影",
                total_power_score=910,
                public_power_score=910,
                hidden_pvp_score=890,
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()
    finally:
        session.close()

    session = session_factory()
    try:
        character = session.scalar(select(Character).where(Character.name == "玄岳"))
        assert character is not None
        session.add_all(
            [
                EquipmentItem(
                    character_id=character.id,
                    slot_id="weapon",
                    equipped_slot_id="weapon",
                    quality_id="rare",
                    item_name="寒刃",
                    base_snapshot_json={"attack": 55},
                ),
                EquipmentItem(
                    character_id=character.id,
                    slot_id="weapon",
                    equipped_slot_id="weapon",
                    quality_id="epic",
                    item_name="惊雷",
                    base_snapshot_json={"attack": 88},
                ),
            ]
        )
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()
    finally:
        session.close()
