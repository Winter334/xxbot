"""创建阶段 0 链路验证表"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0001_create_system_markers"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """创建链路验证所需的最小表结构。"""
    op.create_table(
        "system_markers",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("name", name="uq_system_markers_name"),
    )


def downgrade() -> None:
    """回滚链路验证表。"""
    op.drop_table("system_markers")
