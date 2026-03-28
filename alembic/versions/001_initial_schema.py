"""initial schema

Revision ID: 001
Revises:
Create Date: 2026-03-27

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "portfolio",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("ticker", sa.String(10), nullable=False),
        sa.Column("shares", sa.Numeric(15, 4), nullable=False),
        sa.Column("cost_basis", sa.Numeric(15, 4), nullable=False),
        sa.Column("purchase_date", sa.Date(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "articles",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("ticker", sa.String(10), nullable=False),
        sa.Column("headline", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), unique=True, nullable=False),
        sa.Column("source", sa.String(100)),
        sa.Column("body", sa.Text()),
        sa.Column("snippet", sa.Text()),
        sa.Column("published_at", sa.DateTime()),
        sa.Column("scraped_at", sa.DateTime(), server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_articles_ticker", "articles", ["ticker"])
    op.create_index("ix_articles_published_at", "articles", ["published_at"])

    op.create_table(
        "api_signals",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("ticker", sa.String(10), nullable=False),
        sa.Column("signal_date", sa.Date(), nullable=False),
        sa.Column("current_price", sa.Numeric(15, 4)),
        sa.Column("day_change_pct", sa.Numeric(8, 4)),
        sa.Column("rsi", sa.Numeric(8, 4)),
        sa.Column("macd", sa.Numeric(12, 6)),
        sa.Column("analyst_rating", sa.String(20)),
        sa.Column("price_target", sa.Numeric(15, 4)),
        sa.Column("benzinga_sentiment", sa.Numeric(4, 3)),
        sa.Column("raw_data", JSONB()),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ticker", "signal_date", name="uq_signal_ticker_date"),
    )
    op.create_index("ix_api_signals_ticker", "api_signals", ["ticker"])

    op.create_table(
        "summaries",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("ticker", sa.String(10), nullable=False),
        sa.Column("report_date", sa.Date(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("sentiment", sa.Numeric(4, 3)),
        sa.Column("recommendation", sa.String(10)),
        sa.Column("confidence", sa.Numeric(4, 3)),
        sa.Column("key_catalysts", JSONB()),
        sa.Column("key_risks", JSONB()),
        sa.Column("analyst_consensus", sa.String(20)),
        sa.Column("price_target", sa.Numeric(15, 4)),
        sa.Column("technical_signal", sa.String(200)),
        sa.Column("article_count", sa.Integer()),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ticker", "report_date", name="uq_summary_ticker_date"),
    )

    op.create_table(
        "recommendations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("report_date", sa.Date(), unique=True, nullable=False),
        sa.Column("market_theme", sa.String(300)),
        sa.Column("five_min_summary", sa.Text()),
        sa.Column("portfolio_summary", sa.Text()),
        sa.Column("global_market_context", sa.Text()),
        sa.Column("action_items", JSONB()),
        sa.Column("top_opportunities", JSONB()),
        sa.Column("top_risks", JSONB()),
        sa.Column("overall_sentiment", sa.Numeric(4, 3)),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "newsletters",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("report_date", sa.Date(), unique=True, nullable=False),
        sa.Column("html_content", sa.Text()),
        sa.Column("markdown_content", sa.Text()),
        sa.Column("file_path", sa.String(500)),
        sa.Column("email_sent", sa.Boolean(), default=False),
        sa.Column("email_sent_at", sa.DateTime()),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "scrape_logs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("ticker", sa.String(10)),
        sa.Column("source", sa.String(100)),
        sa.Column("articles_found", sa.Integer()),
        sa.Column("success", sa.Boolean()),
        sa.Column("error_message", sa.Text()),
        sa.Column("duration_ms", sa.Integer()),
        sa.Column("scraped_at", sa.DateTime(), server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("scrape_logs")
    op.drop_table("newsletters")
    op.drop_table("recommendations")
    op.drop_table("summaries")
    op.drop_table("api_signals")
    op.drop_table("articles")
    op.drop_table("portfolio")
