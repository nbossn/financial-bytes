"""Add pipeline_runs table for resumable pipeline progress tracking.

Revision ID: 006
Revises: 005
Create Date: 2026-04-30
"""
from alembic import op
import sqlalchemy as sa

revision = "006"
down_revision = "005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pipeline_runs",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("run_id", sa.String(36), nullable=False, unique=True),
        sa.Column("portfolio_name", sa.String(100), nullable=False, index=True),
        sa.Column("report_date", sa.Date, nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="running"),
        sa.Column("phase", sa.String(50)),
        sa.Column("total_tickers", sa.Integer),
        sa.Column("tickers_complete", sa.Integer, server_default="0"),
        sa.Column("started_at", sa.DateTime, server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime),
        sa.Column("error_message", sa.Text),
        sa.UniqueConstraint("portfolio_name", "report_date", name="uq_run_portfolio_date"),
    )


def downgrade() -> None:
    op.drop_table("pipeline_runs")
