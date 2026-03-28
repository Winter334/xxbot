"""扩展阶段 7 突破秘境持久化结构"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0005_stage7_breakthrough_persistence"
down_revision = "0004_stage6_equipment_persistence_expansion"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """补齐阶段 7 突破秘境进度与方向级软限制账本。"""
    with op.batch_alter_table("breakthrough_trial_progress") as batch_op:
        batch_op.add_column(sa.Column("cleared_count", sa.Integer(), nullable=False, server_default=sa.text("0")))
        batch_op.add_column(sa.Column("first_cleared_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("last_cleared_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("qualification_granted_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("last_reward_direction", sa.String(length=64), nullable=True))

    op.execute(
        """
        UPDATE breakthrough_trial_progress
        SET
            cleared_count = CASE WHEN status = 'cleared' THEN 1 ELSE 0 END,
            first_cleared_at = best_clear_at,
            last_cleared_at = best_clear_at
        """
    )

    with op.batch_alter_table("breakthrough_trial_progress") as batch_op:
        batch_op.create_index(
            "ix_breakthrough_trial_progress_character_id_status",
            ["character_id", "status"],
            unique=False,
        )
        batch_op.alter_column("cleared_count", server_default=None)

    op.create_table(
        "breakthrough_reward_ledgers",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("character_id", sa.Integer(), nullable=False),
        sa.Column("reward_direction", sa.String(length=64), nullable=False),
        sa.Column("cycle_type", sa.String(length=32), nullable=False),
        sa.Column("cycle_anchor_date", sa.Date(), nullable=False),
        sa.Column("high_yield_settlement_count", sa.Integer(), nullable=False),
        sa.Column("last_settled_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["character_id"], ["characters.id"]),
        sa.UniqueConstraint(
            "character_id",
            "reward_direction",
            "cycle_type",
            "cycle_anchor_date",
            name="uq_breakthrough_reward_ledgers_character_direction_cycle",
        ),
    )


def downgrade() -> None:
    """回滚阶段 7 突破秘境持久化结构。"""
    op.drop_table("breakthrough_reward_ledgers")

    with op.batch_alter_table("breakthrough_trial_progress") as batch_op:
        batch_op.drop_index("ix_breakthrough_trial_progress_character_id_status")
        batch_op.drop_column("last_reward_direction")
        batch_op.drop_column("qualification_granted_at")
        batch_op.drop_column("last_cleared_at")
        batch_op.drop_column("first_cleared_at")
        batch_op.drop_column("cleared_count")
