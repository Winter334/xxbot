"""创建阶段 2 业务表结构"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0002_create_stage2_schema"
down_revision = "0001_create_system_markers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """创建阶段 2 所需的最小完整表结构。"""
    op.create_table(
        "players",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("discord_user_id", sa.String(length=32), nullable=False),
        sa.Column("display_name", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("discord_user_id", name="uq_players_discord_user_id"),
    )

    op.create_table(
        "characters",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("player_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=64), nullable=True),
        sa.Column("total_power_score", sa.Integer(), nullable=False),
        sa.Column("public_power_score", sa.Integer(), nullable=False),
        sa.Column("hidden_pvp_score", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["player_id"], ["players.id"]),
        sa.UniqueConstraint("player_id", name="uq_characters_player_id"),
    )
    op.create_index("ix_characters_total_power_score", "characters", ["total_power_score"], unique=False)
    op.create_index("ix_characters_hidden_pvp_score", "characters", ["hidden_pvp_score"], unique=False)

    op.create_table(
        "character_progress",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("character_id", sa.Integer(), nullable=False),
        sa.Column("realm_id", sa.String(length=64), nullable=False),
        sa.Column("stage_id", sa.String(length=64), nullable=False),
        sa.Column("cultivation_value", sa.Integer(), nullable=False),
        sa.Column("comprehension_value", sa.Integer(), nullable=False),
        sa.Column("breakthrough_qualification_obtained", sa.Boolean(), nullable=False),
        sa.Column("highest_endless_floor", sa.Integer(), nullable=False),
        sa.Column("current_hp_ratio", sa.Numeric(6, 4), nullable=False),
        sa.Column("current_mp_ratio", sa.Numeric(6, 4), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["character_id"], ["characters.id"]),
        sa.UniqueConstraint("character_id", name="uq_character_progress_character_id"),
    )
    op.create_index(
        "ix_character_progress_highest_endless_floor",
        "character_progress",
        ["highest_endless_floor"],
        unique=False,
    )

    op.create_table(
        "character_skill_loadouts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("character_id", sa.Integer(), nullable=False),
        sa.Column("main_axis_id", sa.String(length=64), nullable=False),
        sa.Column("main_path_id", sa.String(length=64), nullable=False),
        sa.Column("behavior_template_id", sa.String(length=64), nullable=False),
        sa.Column("body_method_id", sa.String(length=64), nullable=True),
        sa.Column("movement_skill_id", sa.String(length=64), nullable=True),
        sa.Column("spirit_skill_id", sa.String(length=64), nullable=True),
        sa.Column("config_version", sa.String(length=32), nullable=True),
        sa.Column("loadout_notes_json", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["character_id"], ["characters.id"]),
        sa.UniqueConstraint("character_id", name="uq_character_skill_loadouts_character_id"),
    )
    op.create_index(
        "ix_character_skill_loadouts_main_path_id",
        "character_skill_loadouts",
        ["main_path_id"],
        unique=False,
    )

    op.create_table(
        "currency_balances",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("character_id", sa.Integer(), nullable=False),
        sa.Column("spirit_stone", sa.Integer(), nullable=False),
        sa.Column("honor_coin", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["character_id"], ["characters.id"]),
        sa.UniqueConstraint("character_id", name="uq_currency_balances_character_id"),
    )

    op.create_table(
        "equipment_items",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("character_id", sa.Integer(), nullable=False),
        sa.Column("slot_id", sa.String(length=64), nullable=False),
        sa.Column("equipped_slot_id", sa.String(length=64), nullable=True),
        sa.Column("quality_id", sa.String(length=64), nullable=False),
        sa.Column("item_name", sa.String(length=128), nullable=False),
        sa.Column("base_snapshot_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["character_id"], ["characters.id"]),
        sa.UniqueConstraint(
            "character_id",
            "equipped_slot_id",
            name="uq_equipment_items_character_equipped_slot_id",
        ),
    )
    op.create_index("ix_equipment_items_character_id", "equipment_items", ["character_id"], unique=False)
    op.create_index("ix_equipment_items_quality_id", "equipment_items", ["quality_id"], unique=False)

    op.create_table(
        "equipment_enhancements",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("equipment_item_id", sa.Integer(), nullable=False),
        sa.Column("enhancement_level", sa.Integer(), nullable=False),
        sa.Column("success_count", sa.Integer(), nullable=False),
        sa.Column("failure_count", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["equipment_item_id"], ["equipment_items.id"]),
        sa.UniqueConstraint("equipment_item_id", name="uq_equipment_enhancements_equipment_item_id"),
    )

    op.create_table(
        "equipment_affixes",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("equipment_item_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("affix_id", sa.String(length=64), nullable=False),
        sa.Column("tier_id", sa.String(length=64), nullable=False),
        sa.Column("roll_value", sa.Numeric(10, 4), nullable=False),
        sa.ForeignKeyConstraint(["equipment_item_id"], ["equipment_items.id"]),
        sa.UniqueConstraint("equipment_item_id", "position", name="uq_equipment_affixes_equipment_item_position"),
    )
    op.create_index("ix_equipment_affixes_equipment_item_id", "equipment_affixes", ["equipment_item_id"], unique=False)
    op.create_index("ix_equipment_affixes_affix_id", "equipment_affixes", ["affix_id"], unique=False)

    op.create_table(
        "artifact_profiles",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("equipment_item_id", sa.Integer(), nullable=False),
        sa.Column("artifact_template_id", sa.String(length=64), nullable=False),
        sa.Column("refinement_level", sa.Integer(), nullable=False),
        sa.Column("core_effect_snapshot_json", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["equipment_item_id"], ["equipment_items.id"]),
        sa.UniqueConstraint("equipment_item_id", name="uq_artifact_profiles_equipment_item_id"),
    )

    op.create_table(
        "inventory_items",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("character_id", sa.Integer(), nullable=False),
        sa.Column("item_type", sa.String(length=64), nullable=False),
        sa.Column("item_id", sa.String(length=64), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("item_payload_json", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["character_id"], ["characters.id"]),
        sa.UniqueConstraint(
            "character_id",
            "item_type",
            "item_id",
            name="uq_inventory_items_character_type_item_id",
        ),
    )

    op.create_table(
        "retreat_states",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("character_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("scheduled_end_at", sa.DateTime(), nullable=True),
        sa.Column("settled_at", sa.DateTime(), nullable=True),
        sa.Column("context_json", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["character_id"], ["characters.id"]),
        sa.UniqueConstraint("character_id", name="uq_retreat_states_character_id"),
    )
    op.create_index("ix_retreat_states_scheduled_end_at", "retreat_states", ["scheduled_end_at"], unique=False)

    op.create_table(
        "healing_states",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("character_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("injury_level", sa.String(length=32), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("scheduled_end_at", sa.DateTime(), nullable=True),
        sa.Column("settled_at", sa.DateTime(), nullable=True),
        sa.Column("context_json", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["character_id"], ["characters.id"]),
        sa.UniqueConstraint("character_id", name="uq_healing_states_character_id"),
    )
    op.create_index("ix_healing_states_scheduled_end_at", "healing_states", ["scheduled_end_at"], unique=False)

    op.create_table(
        "endless_run_states",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("character_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("current_floor", sa.Integer(), nullable=False),
        sa.Column("highest_floor_reached", sa.Integer(), nullable=False),
        sa.Column("last_region_bias_id", sa.String(length=64), nullable=True),
        sa.Column("last_enemy_template_id", sa.String(length=64), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("pending_rewards_json", sa.JSON(), nullable=False),
        sa.Column("run_snapshot_json", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["character_id"], ["characters.id"]),
        sa.UniqueConstraint("character_id", name="uq_endless_run_states_character_id"),
    )
    op.create_index(
        "ix_endless_run_states_highest_floor_reached",
        "endless_run_states",
        ["highest_floor_reached"],
        unique=False,
    )
    op.create_index("ix_endless_run_states_status", "endless_run_states", ["status"], unique=False)

    op.create_table(
        "breakthrough_trial_progress",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("character_id", sa.Integer(), nullable=False),
        sa.Column("mapping_id", sa.String(length=64), nullable=False),
        sa.Column("group_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("best_clear_at", sa.DateTime(), nullable=True),
        sa.Column("last_result_json", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["character_id"], ["characters.id"]),
        sa.UniqueConstraint(
            "character_id",
            "mapping_id",
            name="uq_breakthrough_trial_progress_character_mapping_id",
        ),
    )
    op.create_index(
        "ix_breakthrough_trial_progress_group_id",
        "breakthrough_trial_progress",
        ["group_id"],
        unique=False,
    )

    op.create_table(
        "pvp_defense_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("character_id", sa.Integer(), nullable=False),
        sa.Column("snapshot_version", sa.Integer(), nullable=False),
        sa.Column("power_score", sa.Integer(), nullable=False),
        sa.Column("rank_position", sa.Integer(), nullable=True),
        sa.Column("formation_json", sa.JSON(), nullable=False),
        sa.Column("stats_json", sa.JSON(), nullable=False),
        sa.Column("source_updated_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["character_id"], ["characters.id"]),
        sa.UniqueConstraint(
            "character_id",
            "snapshot_version",
            name="uq_pvp_defense_snapshots_character_version",
        ),
    )
    op.create_index("ix_pvp_defense_snapshots_power_score", "pvp_defense_snapshots", ["power_score"], unique=False)
    op.create_index("ix_pvp_defense_snapshots_character_id", "pvp_defense_snapshots", ["character_id"], unique=False)

    op.create_table(
        "leaderboard_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("board_type", sa.String(length=32), nullable=False),
        sa.Column("generated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("scope_json", sa.JSON(), nullable=False),
    )
    op.create_index(
        "ix_leaderboard_snapshots_board_type_generated_at",
        "leaderboard_snapshots",
        ["board_type", "generated_at"],
        unique=False,
    )

    op.create_table(
        "battle_reports",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("character_id", sa.Integer(), nullable=False),
        sa.Column("battle_type", sa.String(length=32), nullable=False),
        sa.Column("result", sa.String(length=32), nullable=False),
        sa.Column("opponent_ref", sa.String(length=128), nullable=True),
        sa.Column("summary_json", sa.JSON(), nullable=False),
        sa.Column("detail_log_json", sa.JSON(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["character_id"], ["characters.id"]),
    )
    op.create_index(
        "ix_battle_reports_character_id_occurred_at",
        "battle_reports",
        ["character_id", "occurred_at"],
        unique=False,
    )
    op.create_index("ix_battle_reports_battle_type", "battle_reports", ["battle_type"], unique=False)

    op.create_table(
        "leaderboard_entry_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("leaderboard_snapshot_id", sa.Integer(), nullable=False),
        sa.Column("character_id", sa.Integer(), nullable=False),
        sa.Column("rank_position", sa.Integer(), nullable=False),
        sa.Column("score", sa.Integer(), nullable=False),
        sa.Column("summary_json", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["leaderboard_snapshot_id"], ["leaderboard_snapshots.id"]),
        sa.ForeignKeyConstraint(["character_id"], ["characters.id"]),
        sa.UniqueConstraint(
            "leaderboard_snapshot_id",
            "rank_position",
            name="uq_leaderboard_entry_snapshots_snapshot_rank_position",
        ),
        sa.UniqueConstraint(
            "leaderboard_snapshot_id",
            "character_id",
            name="uq_leaderboard_entry_snapshots_snapshot_character_id",
        ),
    )
    op.create_index(
        "ix_leaderboard_entry_snapshots_score",
        "leaderboard_entry_snapshots",
        ["score"],
        unique=False,
    )
    op.create_index(
        "ix_leaderboard_entry_snapshots_character_id",
        "leaderboard_entry_snapshots",
        ["character_id"],
        unique=False,
    )

    op.create_table(
        "drop_records",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("character_id", sa.Integer(), nullable=False),
        sa.Column("battle_report_id", sa.Integer(), nullable=True),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("source_ref", sa.String(length=128), nullable=True),
        sa.Column("items_json", sa.JSON(), nullable=False),
        sa.Column("currencies_json", sa.JSON(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["character_id"], ["characters.id"]),
        sa.ForeignKeyConstraint(["battle_report_id"], ["battle_reports.id"]),
    )
    op.create_index(
        "ix_drop_records_character_id_occurred_at",
        "drop_records",
        ["character_id", "occurred_at"],
        unique=False,
    )
    op.create_index("ix_drop_records_source_type", "drop_records", ["source_type"], unique=False)


def downgrade() -> None:
    """回滚阶段 2 业务表结构。"""
    op.drop_index("ix_drop_records_source_type", table_name="drop_records")
    op.drop_index("ix_drop_records_character_id_occurred_at", table_name="drop_records")
    op.drop_table("drop_records")

    op.drop_index("ix_leaderboard_entry_snapshots_character_id", table_name="leaderboard_entry_snapshots")
    op.drop_index("ix_leaderboard_entry_snapshots_score", table_name="leaderboard_entry_snapshots")
    op.drop_table("leaderboard_entry_snapshots")

    op.drop_index("ix_battle_reports_battle_type", table_name="battle_reports")
    op.drop_index("ix_battle_reports_character_id_occurred_at", table_name="battle_reports")
    op.drop_table("battle_reports")

    op.drop_index("ix_leaderboard_snapshots_board_type_generated_at", table_name="leaderboard_snapshots")
    op.drop_table("leaderboard_snapshots")

    op.drop_index("ix_pvp_defense_snapshots_character_id", table_name="pvp_defense_snapshots")
    op.drop_index("ix_pvp_defense_snapshots_power_score", table_name="pvp_defense_snapshots")
    op.drop_table("pvp_defense_snapshots")

    op.drop_index("ix_breakthrough_trial_progress_group_id", table_name="breakthrough_trial_progress")
    op.drop_table("breakthrough_trial_progress")

    op.drop_index("ix_endless_run_states_status", table_name="endless_run_states")
    op.drop_index("ix_endless_run_states_highest_floor_reached", table_name="endless_run_states")
    op.drop_table("endless_run_states")

    op.drop_index("ix_healing_states_scheduled_end_at", table_name="healing_states")
    op.drop_table("healing_states")

    op.drop_index("ix_retreat_states_scheduled_end_at", table_name="retreat_states")
    op.drop_table("retreat_states")

    op.drop_table("inventory_items")
    op.drop_table("artifact_profiles")

    op.drop_index("ix_equipment_affixes_affix_id", table_name="equipment_affixes")
    op.drop_index("ix_equipment_affixes_equipment_item_id", table_name="equipment_affixes")
    op.drop_table("equipment_affixes")

    op.drop_table("equipment_enhancements")

    op.drop_index("ix_equipment_items_quality_id", table_name="equipment_items")
    op.drop_index("ix_equipment_items_character_id", table_name="equipment_items")
    op.drop_table("equipment_items")

    op.drop_table("currency_balances")

    op.drop_index("ix_character_skill_loadouts_main_path_id", table_name="character_skill_loadouts")
    op.drop_table("character_skill_loadouts")

    op.drop_index("ix_character_progress_highest_endless_floor", table_name="character_progress")
    op.drop_table("character_progress")

    op.drop_index("ix_characters_hidden_pvp_score", table_name="characters")
    op.drop_index("ix_characters_total_power_score", table_name="characters")
    op.drop_table("characters")

    op.drop_table("players")
