"""Fix residual single-column UNIQUE on report_date in recommendations and newsletters.

SQLite batch_alter_table did not fully remove the inline UNIQUE from the original
column definition in migration 002. This migration rebuilds both tables cleanly.

Revision ID: 003
Revises: 002
Create Date: 2026-03-28
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import sqlite

revision = "003"
down_revision = "002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    is_sqlite = bind.dialect.name == "sqlite"

    if is_sqlite:
        # Recreate recommendations without the old column-level UNIQUE (report_date)
        with op.batch_alter_table("recommendations", recreate="always") as batch_op:
            batch_op.alter_column(
                "report_date",
                existing_type=sa.Date(),
                nullable=False,
                unique=False,
            )

        # Recreate newsletters without the old column-level UNIQUE (report_date)
        with op.batch_alter_table("newsletters", recreate="always") as batch_op:
            batch_op.alter_column(
                "report_date",
                existing_type=sa.Date(),
                nullable=False,
                unique=False,
            )


def downgrade() -> None:
    pass  # No rollback needed for constraint cleanup
