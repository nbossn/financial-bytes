"""add portfolio_name to per-portfolio tables

Revision ID: 002
Revises: 001
Create Date: 2026-03-28
"""
from alembic import op
import sqlalchemy as sa

revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    is_sqlite = bind.dialect.name == "sqlite"

    # ── portfolio ─────────────────────────────────────────────────
    op.add_column("portfolio", sa.Column("portfolio_name", sa.String(100), nullable=True))
    op.execute("UPDATE portfolio SET portfolio_name = 'default'")
    if not is_sqlite:
        op.alter_column("portfolio", "portfolio_name", nullable=False)
    op.create_index("ix_portfolio_portfolio_name", "portfolio", ["portfolio_name"])

    # ── summaries ─────────────────────────────────────────────────
    if is_sqlite:
        with op.batch_alter_table("summaries") as batch_op:
            batch_op.drop_constraint("uq_summary_ticker_date", type_="unique")
            batch_op.add_column(sa.Column("portfolio_name", sa.String(100), nullable=True))
        op.execute("UPDATE summaries SET portfolio_name = 'default'")
        with op.batch_alter_table("summaries") as batch_op:
            batch_op.create_unique_constraint(
                "uq_summary_ticker_date_portfolio",
                ["ticker", "report_date", "portfolio_name"],
            )
    else:
        op.drop_constraint("uq_summary_ticker_date", "summaries", type_="unique")
        op.add_column("summaries", sa.Column("portfolio_name", sa.String(100), nullable=True))
        op.execute("UPDATE summaries SET portfolio_name = 'default'")
        op.alter_column("summaries", "portfolio_name", nullable=False)
        op.create_index("ix_summaries_portfolio_name", "summaries", ["portfolio_name"])
        op.create_unique_constraint(
            "uq_summary_ticker_date_portfolio",
            "summaries",
            ["ticker", "report_date", "portfolio_name"],
        )

    # ── recommendations ───────────────────────────────────────────
    if is_sqlite:
        with op.batch_alter_table("recommendations") as batch_op:
            batch_op.add_column(sa.Column("portfolio_name", sa.String(100), nullable=True))
        op.execute("UPDATE recommendations SET portfolio_name = 'default'")
        with op.batch_alter_table("recommendations") as batch_op:
            # Drop the column-level unique on report_date (SQLite batch recreates table)
            batch_op.alter_column("report_date", existing_type=sa.Date(), unique=False, nullable=False)
            batch_op.create_unique_constraint(
                "uq_recommendation_date_portfolio",
                ["report_date", "portfolio_name"],
            )
    else:
        op.add_column("recommendations", sa.Column("portfolio_name", sa.String(100), nullable=True))
        op.execute("UPDATE recommendations SET portfolio_name = 'default'")
        op.alter_column("recommendations", "portfolio_name", nullable=False)
        op.create_index("ix_recommendations_portfolio_name", "recommendations", ["portfolio_name"])
        # PostgreSQL auto-names single-column unique as {table}_{column}_key
        op.drop_constraint("recommendations_report_date_key", "recommendations", type_="unique")
        op.create_unique_constraint(
            "uq_recommendation_date_portfolio",
            "recommendations",
            ["report_date", "portfolio_name"],
        )

    # ── newsletters ───────────────────────────────────────────────
    if is_sqlite:
        with op.batch_alter_table("newsletters") as batch_op:
            batch_op.add_column(sa.Column("portfolio_name", sa.String(100), nullable=True))
        op.execute("UPDATE newsletters SET portfolio_name = 'default'")
        with op.batch_alter_table("newsletters") as batch_op:
            batch_op.alter_column("report_date", existing_type=sa.Date(), unique=False, nullable=False)
            batch_op.create_unique_constraint(
                "uq_newsletter_date_portfolio",
                ["report_date", "portfolio_name"],
            )
    else:
        op.add_column("newsletters", sa.Column("portfolio_name", sa.String(100), nullable=True))
        op.execute("UPDATE newsletters SET portfolio_name = 'default'")
        op.alter_column("newsletters", "portfolio_name", nullable=False)
        op.create_index("ix_newsletters_portfolio_name", "newsletters", ["portfolio_name"])
        op.drop_constraint("newsletters_report_date_key", "newsletters", type_="unique")
        op.create_unique_constraint(
            "uq_newsletter_date_portfolio",
            "newsletters",
            ["report_date", "portfolio_name"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    is_sqlite = bind.dialect.name == "sqlite"

    # newsletters
    if is_sqlite:
        with op.batch_alter_table("newsletters") as batch_op:
            batch_op.drop_constraint("uq_newsletter_date_portfolio", type_="unique")
            batch_op.drop_column("portfolio_name")
            batch_op.alter_column("report_date", existing_type=sa.Date(), unique=True, nullable=False)
    else:
        op.drop_constraint("uq_newsletter_date_portfolio", "newsletters", type_="unique")
        op.drop_index("ix_newsletters_portfolio_name", "newsletters")
        op.drop_column("newsletters", "portfolio_name")
        op.create_unique_constraint("newsletters_report_date_key", "newsletters", ["report_date"])

    # recommendations
    if is_sqlite:
        with op.batch_alter_table("recommendations") as batch_op:
            batch_op.drop_constraint("uq_recommendation_date_portfolio", type_="unique")
            batch_op.drop_column("portfolio_name")
            batch_op.alter_column("report_date", existing_type=sa.Date(), unique=True, nullable=False)
    else:
        op.drop_constraint("uq_recommendation_date_portfolio", "recommendations", type_="unique")
        op.drop_index("ix_recommendations_portfolio_name", "recommendations")
        op.drop_column("recommendations", "portfolio_name")
        op.create_unique_constraint("recommendations_report_date_key", "recommendations", ["report_date"])

    # summaries
    if is_sqlite:
        with op.batch_alter_table("summaries") as batch_op:
            batch_op.drop_constraint("uq_summary_ticker_date_portfolio", type_="unique")
            batch_op.drop_column("portfolio_name")
            batch_op.create_unique_constraint("uq_summary_ticker_date", ["ticker", "report_date"])
    else:
        op.drop_constraint("uq_summary_ticker_date_portfolio", "summaries", type_="unique")
        op.drop_index("ix_summaries_portfolio_name", "summaries")
        op.drop_column("summaries", "portfolio_name")
        op.create_unique_constraint("uq_summary_ticker_date", "summaries", ["ticker", "report_date"])

    # portfolio
    op.drop_index("ix_portfolio_portfolio_name", "portfolio")
    op.drop_column("portfolio", "portfolio_name")
