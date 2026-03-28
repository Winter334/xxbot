"""调整阶段 5 无尽副本运行态结构"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0003_stage5_endless_adjustments"
down_revision = "0002_create_stage2_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """补齐无尽副本运行态所需的骨架字段。"""
    with op.batch_alter_table("endless_run_states") as batch_op:
        batch_op.add_column(
            sa.Column(
                "selected_start_floor",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("1"),
            )
        )
        batch_op.add_column(
            sa.Column(
                "current_node_type",
                sa.String(length=32),
                nullable=False,
                server_default=sa.text("'normal'"),
            )
        )
        batch_op.add_column(
            sa.Column(
                "run_seed",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )
        batch_op.create_index(
            "ix_endless_run_states_current_node_type",
            ["current_node_type"],
            unique=False,
        )

    with op.batch_alter_table("endless_run_states") as batch_op:
        batch_op.alter_column("selected_start_floor", server_default=None)
        batch_op.alter_column("current_node_type", server_default=None)
        batch_op.alter_column("run_seed", server_default=None)


def downgrade() -> None:
    """回滚无尽副本运行态骨架字段。"""
    with op.batch_alter_table("endless_run_states") as batch_op:
        batch_op.drop_index("ix_endless_run_states_current_node_type")
        batch_op.drop_column("run_seed")
        batch_op.drop_column("current_node_type")
        batch_op.drop_column("selected_start_floor")
