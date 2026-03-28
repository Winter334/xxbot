"""扩展阶段 9 PVP 核心持久化结构"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0007_stage9_pvp_core"
down_revision = "0006_stage8_character_score_snapshots"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """扩展 PVP 防守快照并新增挑战记录、日账本与荣誉币流水。"""
    with op.batch_alter_table("pvp_defense_snapshots") as batch_op:
        batch_op.add_column(sa.Column("public_power_score", sa.Integer(), nullable=False, server_default=sa.text("0")))
        batch_op.add_column(sa.Column("hidden_pvp_score", sa.Integer(), nullable=False, server_default=sa.text("0")))
        batch_op.add_column(sa.Column("score_version", sa.String(length=32), nullable=False, server_default=sa.text("'stage8.v1'")))
        batch_op.add_column(
            sa.Column("snapshot_reason", sa.String(length=64), nullable=False, server_default=sa.text("'legacy_import'"))
        )
        batch_op.add_column(
            sa.Column("build_fingerprint", sa.String(length=128), nullable=False, server_default=sa.text("'legacy_snapshot'"))
        )
        batch_op.add_column(sa.Column("summary_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")))
        batch_op.add_column(sa.Column("lock_started_at", sa.DateTime(), nullable=False, server_default=sa.func.now()))
        batch_op.add_column(sa.Column("lock_expires_at", sa.DateTime(), nullable=False, server_default=sa.func.now()))
        batch_op.create_index(
            "ix_pvp_defense_snapshots_character_lock_expires_at",
            ["character_id", "lock_expires_at"],
            unique=False,
        )
        batch_op.create_index("ix_pvp_defense_snapshots_build_fingerprint", ["build_fingerprint"], unique=False)

    op.execute(
        """
        UPDATE pvp_defense_snapshots
        SET
            public_power_score = power_score,
            hidden_pvp_score = power_score,
            score_version = 'stage8.v1',
            snapshot_reason = 'legacy_import',
            build_fingerprint = 'legacy_' || character_id || '_' || snapshot_version,
            summary_json = '{}',
            lock_started_at = COALESCE(source_updated_at, created_at),
            lock_expires_at = datetime(COALESCE(source_updated_at, created_at), '+24 hours')
        """
    )

    with op.batch_alter_table("pvp_defense_snapshots") as batch_op:
        batch_op.alter_column("public_power_score", server_default=None)
        batch_op.alter_column("hidden_pvp_score", server_default=None)
        batch_op.alter_column("score_version", server_default=None)
        batch_op.alter_column("snapshot_reason", server_default=None)
        batch_op.alter_column("build_fingerprint", server_default=None)
        batch_op.alter_column("summary_json", server_default=None)
        batch_op.alter_column("lock_started_at", server_default=None)
        batch_op.alter_column("lock_expires_at", server_default=None)

    op.create_table(
        "pvp_daily_activity_ledgers",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("character_id", sa.Integer(), nullable=False),
        sa.Column("cycle_anchor_date", sa.Date(), nullable=False),
        sa.Column("effective_challenge_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("successful_challenge_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("defense_failure_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_challenge_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["character_id"], ["characters.id"]),
        sa.UniqueConstraint("character_id", "cycle_anchor_date", name="uq_pvp_daily_activity_ledgers_character_cycle"),
    )
    op.create_index(
        "ix_pvp_daily_activity_ledgers_cycle_anchor_date",
        "pvp_daily_activity_ledgers",
        ["cycle_anchor_date"],
        unique=False,
    )

    op.create_table(
        "honor_coin_ledgers",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("character_id", sa.Integer(), nullable=False),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("source_ref", sa.String(length=128), nullable=True),
        sa.Column("delta", sa.Integer(), nullable=False),
        sa.Column("balance_after", sa.Integer(), nullable=False),
        sa.Column("detail_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["character_id"], ["characters.id"]),
    )
    op.create_index(
        "ix_honor_coin_ledgers_character_id_created_at",
        "honor_coin_ledgers",
        ["character_id", "created_at"],
        unique=False,
    )
    op.create_index("ix_honor_coin_ledgers_source_type", "honor_coin_ledgers", ["source_type"], unique=False)

    op.create_table(
        "pvp_challenge_records",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("attacker_character_id", sa.Integer(), nullable=False),
        sa.Column("defender_character_id", sa.Integer(), nullable=False),
        sa.Column("defender_snapshot_id", sa.Integer(), nullable=False),
        sa.Column("leaderboard_snapshot_id", sa.Integer(), nullable=False),
        sa.Column("battle_report_id", sa.Integer(), nullable=False),
        sa.Column("cycle_anchor_date", sa.Date(), nullable=False),
        sa.Column("battle_outcome", sa.String(length=32), nullable=False),
        sa.Column("rank_before_attacker", sa.Integer(), nullable=False),
        sa.Column("rank_before_defender", sa.Integer(), nullable=False),
        sa.Column("rank_after_attacker", sa.Integer(), nullable=False),
        sa.Column("rank_after_defender", sa.Integer(), nullable=False),
        sa.Column("honor_coin_delta", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("rank_effect_applied", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("settlement_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["attacker_character_id"], ["characters.id"]),
        sa.ForeignKeyConstraint(["defender_character_id"], ["characters.id"]),
        sa.ForeignKeyConstraint(["defender_snapshot_id"], ["pvp_defense_snapshots.id"]),
        sa.ForeignKeyConstraint(["leaderboard_snapshot_id"], ["leaderboard_snapshots.id"]),
        sa.ForeignKeyConstraint(["battle_report_id"], ["battle_reports.id"]),
        sa.UniqueConstraint("battle_report_id", name="uq_pvp_challenge_records_battle_report_id"),
    )
    op.create_index(
        "ix_pvp_challenge_records_attacker_cycle_created_at",
        "pvp_challenge_records",
        ["attacker_character_id", "cycle_anchor_date", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_pvp_challenge_records_defender_cycle_created_at",
        "pvp_challenge_records",
        ["defender_character_id", "cycle_anchor_date", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_pvp_challenge_records_attacker_defender_cycle",
        "pvp_challenge_records",
        ["attacker_character_id", "defender_character_id", "cycle_anchor_date"],
        unique=False,
    )
    op.create_index(
        "ix_pvp_challenge_records_leaderboard_snapshot_id",
        "pvp_challenge_records",
        ["leaderboard_snapshot_id"],
        unique=False,
    )
    op.create_index(
        "ix_pvp_challenge_records_defender_snapshot_id",
        "pvp_challenge_records",
        ["defender_snapshot_id"],
        unique=False,
    )

    with op.batch_alter_table("honor_coin_ledgers") as batch_op:
        batch_op.alter_column("detail_json", server_default=None)

    with op.batch_alter_table("pvp_challenge_records") as batch_op:
        batch_op.alter_column("honor_coin_delta", server_default=None)
        batch_op.alter_column("rank_effect_applied", server_default=None)
        batch_op.alter_column("settlement_json", server_default=None)


def downgrade() -> None:
    """回滚阶段 9 PVP 核心持久化结构。"""
    op.drop_index("ix_pvp_challenge_records_defender_snapshot_id", table_name="pvp_challenge_records")
    op.drop_index("ix_pvp_challenge_records_leaderboard_snapshot_id", table_name="pvp_challenge_records")
    op.drop_index("ix_pvp_challenge_records_attacker_defender_cycle", table_name="pvp_challenge_records")
    op.drop_index("ix_pvp_challenge_records_defender_cycle_created_at", table_name="pvp_challenge_records")
    op.drop_index("ix_pvp_challenge_records_attacker_cycle_created_at", table_name="pvp_challenge_records")
    op.drop_table("pvp_challenge_records")

    op.drop_index("ix_honor_coin_ledgers_source_type", table_name="honor_coin_ledgers")
    op.drop_index("ix_honor_coin_ledgers_character_id_created_at", table_name="honor_coin_ledgers")
    op.drop_table("honor_coin_ledgers")

    op.drop_index("ix_pvp_daily_activity_ledgers_cycle_anchor_date", table_name="pvp_daily_activity_ledgers")
    op.drop_table("pvp_daily_activity_ledgers")

    with op.batch_alter_table("pvp_defense_snapshots") as batch_op:
        batch_op.drop_index("ix_pvp_defense_snapshots_build_fingerprint")
        batch_op.drop_index("ix_pvp_defense_snapshots_character_lock_expires_at")
        batch_op.drop_column("lock_expires_at")
        batch_op.drop_column("lock_started_at")
        batch_op.drop_column("summary_json")
        batch_op.drop_column("build_fingerprint")
        batch_op.drop_column("snapshot_reason")
        batch_op.drop_column("score_version")
        batch_op.drop_column("hidden_pvp_score")
        batch_op.drop_column("public_power_score")
