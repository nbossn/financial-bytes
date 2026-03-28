"""Drop legacy single-column UNIQUE (report_date) from recommendations and newsletters.

SQLite batch_alter_table cannot remove unnamed inline UNIQUE constraints.
This migration uses raw SQL to rebuild both tables without the old constraint.

Revision ID: 004
Revises: 003
Create Date: 2026-03-28
"""
from alembic import op

revision = "004"
down_revision = "003"
branch_labels = None
depends_on = None


_RECOMMENDATIONS_DDL = """
CREATE TABLE recommendations_new (
    id INTEGER NOT NULL PRIMARY KEY,
    report_date DATE NOT NULL,
    portfolio_name VARCHAR(100),
    market_theme VARCHAR(300),
    five_min_summary TEXT,
    portfolio_summary TEXT,
    global_market_context TEXT,
    action_items JSON,
    top_opportunities JSON,
    top_risks JSON,
    overall_sentiment NUMERIC(4, 3),
    created_at DATETIME DEFAULT (CURRENT_TIMESTAMP),
    CONSTRAINT uq_recommendation_date_portfolio UNIQUE (report_date, portfolio_name)
)
"""

_NEWSLETTERS_DDL = """
CREATE TABLE newsletters_new (
    id INTEGER NOT NULL PRIMARY KEY,
    report_date DATE NOT NULL,
    portfolio_name VARCHAR(100),
    html_content TEXT,
    markdown_content TEXT,
    file_path VARCHAR(500),
    email_sent BOOLEAN,
    email_sent_at DATETIME,
    created_at DATETIME DEFAULT (CURRENT_TIMESTAMP),
    CONSTRAINT uq_newsletter_date_portfolio UNIQUE (report_date, portfolio_name)
)
"""


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        return  # PostgreSQL constraints were fixed correctly in 002

    conn = bind

    # Rebuild recommendations
    conn.exec_driver_sql(_RECOMMENDATIONS_DDL.strip())
    conn.exec_driver_sql(
        "INSERT INTO recommendations_new "
        "SELECT id, report_date, portfolio_name, market_theme, five_min_summary, "
        "portfolio_summary, global_market_context, action_items, top_opportunities, "
        "top_risks, overall_sentiment, created_at FROM recommendations"
    )
    conn.exec_driver_sql("DROP TABLE recommendations")
    conn.exec_driver_sql("ALTER TABLE recommendations_new RENAME TO recommendations")

    # Rebuild newsletters
    conn.exec_driver_sql(_NEWSLETTERS_DDL.strip())
    conn.exec_driver_sql(
        "INSERT INTO newsletters_new "
        "SELECT id, report_date, portfolio_name, html_content, markdown_content, "
        "file_path, email_sent, email_sent_at, created_at FROM newsletters"
    )
    conn.exec_driver_sql("DROP TABLE newsletters")
    conn.exec_driver_sql("ALTER TABLE newsletters_new RENAME TO newsletters")


def downgrade() -> None:
    pass
