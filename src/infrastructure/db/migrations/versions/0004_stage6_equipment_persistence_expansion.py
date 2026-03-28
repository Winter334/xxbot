"""扩展阶段 6 装备持久化结构"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0004_stage6_equipment_persistence_expansion"
down_revision = "0003_stage5_endless_adjustments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """补齐阶段 6 装备成长、命名与分解审计所需结构。"""
    with op.batch_alter_table("equipment_items") as batch_op:
        batch_op.add_column(sa.Column("slot_name", sa.String(length=64), nullable=False, server_default=sa.text("''")))
        batch_op.add_column(sa.Column("quality_name", sa.String(length=64), nullable=False, server_default=sa.text("''")))
        batch_op.add_column(
            sa.Column("template_id", sa.String(length=64), nullable=False, server_default=sa.text("'legacy_template'"))
        )
        batch_op.add_column(sa.Column("template_name", sa.String(length=128), nullable=False, server_default=sa.text("''")))
        batch_op.add_column(sa.Column("is_artifact", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch_op.add_column(sa.Column("resonance_name", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("item_state", sa.String(length=32), nullable=False, server_default=sa.text("'active'")))
        batch_op.add_column(sa.Column("dismantled_at", sa.DateTime(), nullable=True))

    op.execute("UPDATE equipment_items SET slot_name = slot_id WHERE slot_name = ''")
    op.execute("UPDATE equipment_items SET quality_name = quality_id WHERE quality_name = ''")
    op.execute("UPDATE equipment_items SET template_name = item_name WHERE template_name = ''")
    op.execute("UPDATE equipment_items SET is_artifact = true WHERE slot_id = 'artifact'")

    with op.batch_alter_table("equipment_items") as batch_op:
        batch_op.create_index("ix_equipment_items_character_id_item_state", ["character_id", "item_state"], unique=False)
        batch_op.create_index("ix_equipment_items_template_id", ["template_id"], unique=False)
        batch_op.alter_column("slot_name", server_default=None)
        batch_op.alter_column("quality_name", server_default=None)
        batch_op.alter_column("template_id", server_default=None)
        batch_op.alter_column("template_name", server_default=None)
        batch_op.alter_column("is_artifact", server_default=None)
        batch_op.alter_column("item_state", server_default=None)

    with op.batch_alter_table("equipment_enhancements") as batch_op:
        batch_op.add_column(
            sa.Column("base_stat_bonus_ratio", sa.Numeric(10, 4), nullable=False, server_default=sa.text("0"))
        )
        batch_op.add_column(sa.Column("affix_bonus_ratio", sa.Numeric(10, 4), nullable=False, server_default=sa.text("0")))
        batch_op.alter_column("base_stat_bonus_ratio", server_default=None)
        batch_op.alter_column("affix_bonus_ratio", server_default=None)

    with op.batch_alter_table("equipment_affixes") as batch_op:
        batch_op.add_column(sa.Column("affix_name", sa.String(length=64), nullable=False, server_default=sa.text("''")))
        batch_op.add_column(sa.Column("stat_id", sa.String(length=64), nullable=False, server_default=sa.text("''")))
        batch_op.add_column(sa.Column("category", sa.String(length=64), nullable=False, server_default=sa.text("'unknown'")))
        batch_op.add_column(sa.Column("tier_name", sa.String(length=64), nullable=False, server_default=sa.text("''")))
        batch_op.add_column(sa.Column("value", sa.Integer(), nullable=False, server_default=sa.text("0")))
        batch_op.add_column(sa.Column("is_pve_specialized", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch_op.add_column(sa.Column("is_pvp_specialized", sa.Boolean(), nullable=False, server_default=sa.false()))

    op.execute("UPDATE equipment_affixes SET affix_name = affix_id WHERE affix_name = ''")
    op.execute("UPDATE equipment_affixes SET tier_name = tier_id WHERE tier_name = ''")

    with op.batch_alter_table("equipment_affixes") as batch_op:
        batch_op.create_index("ix_equipment_affixes_tier_id", ["tier_id"], unique=False)
        batch_op.alter_column("affix_name", server_default=None)
        batch_op.alter_column("stat_id", server_default=None)
        batch_op.alter_column("category", server_default=None)
        batch_op.alter_column("tier_name", server_default=None)
        batch_op.alter_column("value", server_default=None)
        batch_op.alter_column("is_pve_specialized", server_default=None)
        batch_op.alter_column("is_pvp_specialized", server_default=None)

    op.create_table(
        "artifact_nurture_states",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("equipment_item_id", sa.Integer(), nullable=False),
        sa.Column("nurture_level", sa.Integer(), nullable=False),
        sa.Column("base_stat_bonus_ratio", sa.Numeric(10, 4), nullable=False),
        sa.Column("affix_bonus_ratio", sa.Numeric(10, 4), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["equipment_item_id"], ["equipment_items.id"]),
        sa.UniqueConstraint("equipment_item_id", name="uq_artifact_nurture_states_equipment_item_id"),
    )

    op.create_table(
        "equipment_naming_states",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("equipment_item_id", sa.Integer(), nullable=False),
        sa.Column("resolved_name", sa.String(length=128), nullable=False),
        sa.Column("naming_template_id", sa.String(length=64), nullable=False),
        sa.Column("naming_source", sa.String(length=64), nullable=False),
        sa.Column("naming_metadata_json", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["equipment_item_id"], ["equipment_items.id"]),
        sa.UniqueConstraint("equipment_item_id", name="uq_equipment_naming_states_equipment_item_id"),
    )
    op.create_index(
        "ix_equipment_naming_states_naming_source",
        "equipment_naming_states",
        ["naming_source"],
        unique=False,
    )

    op.create_table(
        "equipment_dismantle_records",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("equipment_item_id", sa.Integer(), nullable=False),
        sa.Column("character_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("returns_json", sa.JSON(), nullable=False),
        sa.Column("audit_metadata_json", sa.JSON(), nullable=False),
        sa.Column("settled_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["character_id"], ["characters.id"]),
        sa.ForeignKeyConstraint(["equipment_item_id"], ["equipment_items.id"]),
        sa.UniqueConstraint("equipment_item_id", name="uq_equipment_dismantle_records_equipment_item_id"),
    )
    op.create_index(
        "ix_equipment_dismantle_records_character_id_status",
        "equipment_dismantle_records",
        ["character_id", "status"],
        unique=False,
    )

    with op.batch_alter_table("inventory_items") as batch_op:
        batch_op.create_index("ix_inventory_items_character_id_item_type", ["character_id", "item_type"], unique=False)


def downgrade() -> None:
    """回滚阶段 6 装备持久化结构。"""
    with op.batch_alter_table("inventory_items") as batch_op:
        batch_op.drop_index("ix_inventory_items_character_id_item_type")

    op.drop_index("ix_equipment_dismantle_records_character_id_status", table_name="equipment_dismantle_records")
    op.drop_table("equipment_dismantle_records")

    op.drop_index("ix_equipment_naming_states_naming_source", table_name="equipment_naming_states")
    op.drop_table("equipment_naming_states")

    op.drop_table("artifact_nurture_states")

    with op.batch_alter_table("equipment_affixes") as batch_op:
        batch_op.drop_index("ix_equipment_affixes_tier_id")
        batch_op.drop_column("is_pvp_specialized")
        batch_op.drop_column("is_pve_specialized")
        batch_op.drop_column("value")
        batch_op.drop_column("tier_name")
        batch_op.drop_column("category")
        batch_op.drop_column("stat_id")
        batch_op.drop_column("affix_name")

    with op.batch_alter_table("equipment_enhancements") as batch_op:
        batch_op.drop_column("affix_bonus_ratio")
        batch_op.drop_column("base_stat_bonus_ratio")

    with op.batch_alter_table("equipment_items") as batch_op:
        batch_op.drop_index("ix_equipment_items_template_id")
        batch_op.drop_index("ix_equipment_items_character_id_item_state")
        batch_op.drop_column("dismantled_at")
        batch_op.drop_column("item_state")
        batch_op.drop_column("resonance_name")
        batch_op.drop_column("is_artifact")
        batch_op.drop_column("template_name")
        batch_op.drop_column("template_id")
        batch_op.drop_column("quality_name")
        batch_op.drop_column("slot_name")
