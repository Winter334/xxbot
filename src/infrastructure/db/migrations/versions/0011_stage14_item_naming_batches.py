"""阶段 14 结算后实例命名批次最小持久化扩展"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0011_stage14_item_naming_batches"
down_revision = "0010_stage13_special_affix_skeleton"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """为功法补充命名元数据，并新增按单次结算聚合的命名批次表。"""
    with op.batch_alter_table("character_skill_items") as batch_op:
        batch_op.add_column(
            sa.Column(
                "naming_source",
                sa.String(length=64),
                nullable=False,
                server_default=sa.text("'lineage_static'"),
            )
        )
        batch_op.add_column(
            sa.Column(
                "naming_metadata_json",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'{}'"),
            )
        )
        batch_op.create_index("ix_character_skill_items_naming_source", ["naming_source"], unique=False)
        batch_op.alter_column("naming_source", server_default=None)
        batch_op.alter_column("naming_metadata_json", server_default=None)

    op.create_table(
        "item_naming_batches",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("character_id", sa.Integer(), nullable=False),
        sa.Column("source_type", sa.String(length=64), nullable=False),
        sa.Column("source_ref", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("provider_name", sa.String(length=64), nullable=True),
        sa.Column("request_payload_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("result_payload_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("error_message", sa.String(length=255), nullable=True),
        sa.Column("processed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["character_id"], ["characters.id"]),
        sa.UniqueConstraint(
            "character_id",
            "source_type",
            "source_ref",
            name="uq_item_naming_batches_character_source",
        ),
    )
    op.create_index(
        "ix_item_naming_batches_character_id_status",
        "item_naming_batches",
        ["character_id", "status"],
        unique=False,
    )


def downgrade() -> None:
    """回滚命名批次表与功法命名元数据字段。"""
    op.drop_index("ix_item_naming_batches_character_id_status", table_name="item_naming_batches")
    op.drop_table("item_naming_batches")

    with op.batch_alter_table("character_skill_items") as batch_op:
        batch_op.drop_index("ix_character_skill_items_naming_source")
        batch_op.drop_column("naming_metadata_json")
        batch_op.drop_column("naming_source")
