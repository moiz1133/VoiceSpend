"""add expenses.server_seq sync cursor (sequence + trigger)

Revision ID: 000000000002
Revises: 000000000001
Create Date: 2026-09-13 00:00:00

Adds the monotonic server-assigned sequence number that backs the sync pull
cursor. Deliberately NOT app-code-maintained: a BEFORE INSERT OR UPDATE
trigger stamps it on every write to `expenses`, from any code path (the
sync upsert now, LLM/FX enrichment in later phases), so the cursor can never
be forgotten by some future write path. Alembic doesn't have first-class
support for sequences/functions/triggers, so those three steps are raw SQL;
the column and index use normal Alembic ops.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "000000000002"
down_revision: str | None = "000000000001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE SEQUENCE expenses_sync_seq")

    op.add_column("expenses", sa.Column("server_seq", sa.BigInteger(), nullable=True))
    op.execute(
        "UPDATE expenses SET server_seq = nextval('expenses_sync_seq') "
        "WHERE server_seq IS NULL"
    )
    op.alter_column("expenses", "server_seq", nullable=False)

    op.execute(
        """
        CREATE FUNCTION set_expense_server_seq() RETURNS trigger AS $$
        BEGIN
            NEW.server_seq := nextval('expenses_sync_seq');
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_expenses_server_seq
        BEFORE INSERT OR UPDATE ON expenses
        FOR EACH ROW EXECUTE FUNCTION set_expense_server_seq()
        """
    )

    op.create_index("ix_expenses_user_server_seq", "expenses", ["user_id", "server_seq"])


def downgrade() -> None:
    op.drop_index("ix_expenses_user_server_seq", table_name="expenses")
    op.execute("DROP TRIGGER trg_expenses_server_seq ON expenses")
    op.execute("DROP FUNCTION set_expense_server_seq()")
    op.drop_column("expenses", "server_seq")
    op.execute("DROP SEQUENCE expenses_sync_seq")
