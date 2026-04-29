"""Add portfolio_performance table for nightly P&L time-series snapshots.

Revision ID: 005
Revises: 004
Create Date: 2026-04-29
"""
from alembic import op
import sqlalchemy as sa

revision = "005"
down_revision = "004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "portfolio_performance",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("portfolio_name", sa.String(100), nullable=False, index=True),
        sa.Column("snapshot_date", sa.Date, nullable=False, index=True),
        sa.Column("total_cost", sa.Numeric(18, 4), nullable=False),
        sa.Column("total_value", sa.Numeric(18, 4), nullable=False),
        sa.Column("total_pnl", sa.Numeric(18, 4), nullable=False),
        sa.Column("total_pnl_pct", sa.Numeric(10, 4), nullable=False),
        sa.Column("spy_price", sa.Numeric(15, 4)),
        sa.Column("spy_pnl_pct", sa.Numeric(10, 4)),
        sa.Column("position_count", sa.Integer),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
        sa.UniqueConstraint("portfolio_name", "snapshot_date", name="uq_perf_portfolio_date"),
    )


def downgrade() -> None:
    op.drop_table("portfolio_performance")
