"""Allow portfolio.purchase_date to be NULL — unknown acquisition date.

Fidelity's positions export carries no lot dates. With purchase_date NOT NULL the
loader had to invent one to persist at all, and it stamped date.today(). That
fabricated date reached the analyst agent, which read it as the acquisition date
and generated claims like "the CEO sold shares on your day-of-entry" (2026-08-18
newsletter, MRVL).

Unknown is a legitimate state and the schema should be able to say so. Real lot
dates still arrive via the per-portfolio purchase_history override.

Revision ID: 008
Revises: 007
Create Date: 2026-08-18
"""
from alembic import op
import sqlalchemy as sa

revision = "008"
down_revision = "007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # SQLite cannot ALTER a column in place; batch_alter_table rebuilds the table.
    with op.batch_alter_table("portfolio") as batch_op:
        batch_op.alter_column(
            "purchase_date",
            existing_type=sa.Date(),
            nullable=True,
        )


def downgrade() -> None:
    # Rows with NULL would violate NOT NULL. Park them on the old sentinel so the
    # constraint can be restored; this is lossy but reversible in shape.
    op.execute("UPDATE portfolio SET purchase_date = '2000-01-01' WHERE purchase_date IS NULL")
    with op.batch_alter_table("portfolio") as batch_op:
        batch_op.alter_column(
            "purchase_date",
            existing_type=sa.Date(),
            nullable=False,
        )
