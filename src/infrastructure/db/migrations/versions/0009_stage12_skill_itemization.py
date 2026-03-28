"""阶段 12 功法物品化与装配持久化基础设施"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0009_stage12_skill_itemization"
down_revision = "0008_stage11_equipment_rank_system"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """创建功法实例表，并重建角色装配结构为实例引用。"""
    op.create_table(
        "character_skill_items",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("character_id", sa.Integer(), nullable=False),
        sa.Column("lineage_id", sa.String(length=64), nullable=False),
        sa.Column("path_id", sa.String(length=64), nullable=False),
        sa.Column("axis_id", sa.String(length=64), nullable=False),
        sa.Column("skill_type", sa.String(length=32), nullable=False),
        sa.Column("auxiliary_slot_id", sa.String(length=32), nullable=True),
        sa.Column("skill_name", sa.String(length=128), nullable=False),
        sa.Column("rank_id", sa.String(length=64), nullable=False),
        sa.Column("rank_name", sa.String(length=64), nullable=False),
        sa.Column("rank_order", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("quality_id", sa.String(length=64), nullable=False),
        sa.Column("quality_name", sa.String(length=64), nullable=False),
        sa.Column("total_budget", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("budget_distribution_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("resolved_attributes_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("resolved_patches_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("source_type", sa.String(length=64), nullable=False),
        sa.Column("source_record_id", sa.String(length=128), nullable=True),
        sa.Column("is_locked", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("item_state", sa.String(length=32), nullable=False, server_default=sa.text("'inventory'")),
        sa.Column("equipped_at", sa.DateTime(), nullable=True),
        sa.Column("unequipped_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["character_id"], ["characters.id"]),
    )
    op.create_index("ix_character_skill_items_character_id", "character_skill_items", ["character_id"], unique=False)
    op.create_index(
        "ix_character_skill_items_character_id_skill_type",
        "character_skill_items",
        ["character_id", "skill_type"],
        unique=False,
    )
    op.create_index(
        "ix_character_skill_items_character_id_auxiliary_slot_id",
        "character_skill_items",
        ["character_id", "auxiliary_slot_id"],
        unique=False,
    )
    op.create_index("ix_character_skill_items_lineage_id", "character_skill_items", ["lineage_id"], unique=False)
    op.create_index("ix_character_skill_items_item_state", "character_skill_items", ["item_state"], unique=False)

    op.drop_index("ix_character_skill_loadouts_main_path_id", table_name="character_skill_loadouts")
    op.drop_table("character_skill_loadouts")

    op.create_table(
        "character_skill_loadouts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("character_id", sa.Integer(), nullable=False),
        sa.Column("main_skill_id", sa.Integer(), nullable=True),
        sa.Column("guard_skill_id", sa.Integer(), nullable=True),
        sa.Column("movement_skill_id", sa.Integer(), nullable=True),
        sa.Column("spirit_skill_id", sa.Integer(), nullable=True),
        sa.Column("main_axis_id", sa.String(length=64), nullable=True),
        sa.Column("main_path_id", sa.String(length=64), nullable=True),
        sa.Column("behavior_template_id", sa.String(length=64), nullable=True),
        sa.Column("config_version", sa.String(length=32), nullable=True),
        sa.Column("loadout_notes_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["character_id"], ["characters.id"]),
        sa.ForeignKeyConstraint(["main_skill_id"], ["character_skill_items.id"]),
        sa.ForeignKeyConstraint(["guard_skill_id"], ["character_skill_items.id"]),
        sa.ForeignKeyConstraint(["movement_skill_id"], ["character_skill_items.id"]),
        sa.ForeignKeyConstraint(["spirit_skill_id"], ["character_skill_items.id"]),
        sa.UniqueConstraint("character_id", name="uq_character_skill_loadouts_character_id"),
    )
    op.create_index(
        "ix_character_skill_loadouts_main_skill_id",
        "character_skill_loadouts",
        ["main_skill_id"],
        unique=False,
    )
    op.create_index(
        "ix_character_skill_loadouts_main_path_id",
        "character_skill_loadouts",
        ["main_path_id"],
        unique=False,
    )


def downgrade() -> None:
    """回滚功法实例表与新装配字段。"""
    op.drop_index("ix_character_skill_loadouts_main_path_id", table_name="character_skill_loadouts")
    op.drop_index("ix_character_skill_loadouts_main_skill_id", table_name="character_skill_loadouts")
    op.drop_table("character_skill_loadouts")

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
        sa.Column("loadout_notes_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
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

    op.drop_index("ix_character_skill_items_item_state", table_name="character_skill_items")
    op.drop_index("ix_character_skill_items_lineage_id", table_name="character_skill_items")
    op.drop_index("ix_character_skill_items_character_id_auxiliary_slot_id", table_name="character_skill_items")
    op.drop_index("ix_character_skill_items_character_id_skill_type", table_name="character_skill_items")
    op.drop_index("ix_character_skill_items_character_id", table_name="character_skill_items")
    op.drop_table("character_skill_items")
