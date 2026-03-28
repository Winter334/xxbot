"""新增阶段 11 装备阶数持久化字段"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0008_stage11_equipment_rank_system"
down_revision = "0007_stage9_pvp_core"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """为装备实例新增阶数标识、名称、顺序与映射境界。"""
    with op.batch_alter_table("equipment_items") as batch_op:
        batch_op.add_column(sa.Column("rank_id", sa.String(length=64), nullable=False, server_default=sa.text("'mortal'")))
        batch_op.add_column(sa.Column("rank_name", sa.String(length=64), nullable=False, server_default=sa.text("'一阶'")))
        batch_op.add_column(sa.Column("rank_order", sa.Integer(), nullable=False, server_default=sa.text("1")))
        batch_op.add_column(sa.Column("mapped_realm_id", sa.String(length=64), nullable=False, server_default=sa.text("'mortal'")))
        batch_op.create_index("ix_equipment_items_rank_id", ["rank_id"], unique=False)
        batch_op.alter_column("rank_id", server_default=None)
        batch_op.alter_column("rank_name", server_default=None)
        batch_op.alter_column("rank_order", server_default=None)
        batch_op.alter_column("mapped_realm_id", server_default=None)


def downgrade() -> None:
    """回滚装备阶数持久化字段。"""
    with op.batch_alter_table("equipment_items") as batch_op:
        batch_op.drop_index("ix_equipment_items_rank_id")
        batch_op.drop_column("mapped_realm_id")
        batch_op.drop_column("rank_order")
        batch_op.drop_column("rank_name")
        batch_op.drop_column("rank_id")
