"""阶段 13 特殊词条骨架持久化扩展"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0010_stage13_special_affix_skeleton"
down_revision = "0009_stage12_skill_itemization"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """为装备词条表补充特殊效果持久化字段。"""
    with op.batch_alter_table("equipment_affixes") as batch_op:
        batch_op.add_column(sa.Column("affix_kind", sa.String(length=32), nullable=False, server_default=sa.text("'numeric'")))
        batch_op.add_column(sa.Column("special_effect_id", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("special_effect_name", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("special_effect_type", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("trigger_event", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("special_effect_payload_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")))
        batch_op.add_column(sa.Column("public_score_key", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("hidden_pvp_score_key", sa.String(length=64), nullable=True))
        batch_op.create_index("ix_equipment_affixes_affix_kind", ["affix_kind"], unique=False)
        batch_op.create_index("ix_equipment_affixes_special_effect_id", ["special_effect_id"], unique=False)
        batch_op.alter_column("affix_kind", server_default=None)
        batch_op.alter_column("special_effect_payload_json", server_default=None)


def downgrade() -> None:
    """回滚特殊效果持久化字段。"""
    with op.batch_alter_table("equipment_affixes") as batch_op:
        batch_op.drop_index("ix_equipment_affixes_special_effect_id")
        batch_op.drop_index("ix_equipment_affixes_affix_kind")
        batch_op.drop_column("hidden_pvp_score_key")
        batch_op.drop_column("public_score_key")
        batch_op.drop_column("special_effect_payload_json")
        batch_op.drop_column("trigger_event")
        batch_op.drop_column("special_effect_type")
        batch_op.drop_column("special_effect_name")
        batch_op.drop_column("special_effect_id")
        batch_op.drop_column("affix_kind")
