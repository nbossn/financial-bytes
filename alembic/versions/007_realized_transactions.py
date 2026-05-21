"""Add realized_transactions table for tracking executed portfolio sales.

Revision ID: 007
Revises: 006
Create Date: 2026-04-30
"""
from alembic import op
import sqlalchemy as sa

revision = "007"
down_revision = "006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "realized_transactions",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("portfolio_name", sa.String(100), nullable=False, index=True),
        sa.Column("ticker", sa.String(10), nullable=False, index=True),
        sa.Column("transaction_date", sa.Date, nullable=False),
        sa.Column("shares", sa.Numeric(15, 4), nullable=False),
        sa.Column("sale_price", sa.Numeric(15, 4), nullable=False),
        sa.Column("gross_proceeds", sa.Numeric(18, 4), nullable=False),
        sa.Column("cost_basis_total", sa.Numeric(18, 4), nullable=False),
        sa.Column("realized_gain", sa.Numeric(18, 4), nullable=False),
        sa.Column("gain_pct", sa.Numeric(8, 4)),
        sa.Column("gain_pct_of_proceeds", sa.Numeric(8, 4)),
        sa.Column("holding_period", sa.String(20)),
        sa.Column("lot_acquired", sa.Date),
        sa.Column("lot_source", sa.String(50)),
        sa.Column("fed_tax_rate", sa.Numeric(6, 4)),
        sa.Column("fed_tax_estimated", sa.Numeric(18, 4)),
        sa.Column("state_tax_rate", sa.Numeric(6, 4)),
        sa.Column("state_tax_estimated", sa.Numeric(18, 4)),
        sa.Column("total_tax_estimated", sa.Numeric(18, 4)),
        sa.Column("net_proceeds_after_tax", sa.Numeric(18, 4)),
        sa.Column("notes", sa.Text),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("realized_transactions")
