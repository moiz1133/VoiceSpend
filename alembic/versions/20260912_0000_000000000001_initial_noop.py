"""initial no-op migration

Revision ID: 000000000001
Revises:
Create Date: 2026-09-12 00:00:00

Proves the Alembic + async engine toolchain runs end to end. No tables exist
yet — real schema arrives in Phase 2.
"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "000000000001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
