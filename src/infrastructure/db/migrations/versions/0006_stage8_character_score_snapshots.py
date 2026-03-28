"""新增阶段 8 角色评分明细快照表"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0006_stage8_character_score_snapshots"
down_revision = "0005_stage7_breakthrough_persistence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """新增角色评分明细快照表。"""
    op.create_table(
        "character_score_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("character_id", sa.Integer(), nullable=False),
        sa.Column("score_version", sa.String(length=32), nullable=False),
        sa.Column("total_power_score", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("public_power_score", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("hidden_pvp_score", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("growth_score", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("equipment_score", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("skill_score", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("artifact_score", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("pvp_adjustment_score", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("breakdown_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("source_digest", sa.String(length=64), nullable=False),
        sa.Column("computed_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["character_id"], ["characters.id"]),
        sa.UniqueConstraint("character_id", name="uq_character_score_snapshots_character_id"),
    )
    op.create_index(
        "ix_character_score_snapshots_score_version",
        "character_score_snapshots",
        ["score_version"],
        unique=False,
    )

    with op.batch_alter_table("character_score_snapshots") as batch_op:
        batch_op.alter_column("total_power_score", server_default=None)
        batch_op.alter_column("public_power_score", server_default=None)
        batch_op.alter_column("hidden_pvp_score", server_default=None)
        batch_op.alter_column("growth_score", server_default=None)
        batch_op.alter_column("equipment_score", server_default=None)
        batch_op.alter_column("skill_score", server_default=None)
        batch_op.alter_column("artifact_score", server_default=None)
        batch_op.alter_column("pvp_adjustment_score", server_default=None)
        batch_op.alter_column("breakdown_json", server_default=None)


def downgrade() -> None:
    """回滚角色评分明细快照表。"""
    op.drop_index("ix_character_score_snapshots_score_version", table_name="character_score_snapshots")
    op.drop_table("character_score_snapshots")
