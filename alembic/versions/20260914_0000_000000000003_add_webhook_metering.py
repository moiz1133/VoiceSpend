"""add processed_webhook_events + entitlements.last_event_ts_ms

Revision ID: 000000000003
Revises: 000000000002
Create Date: 2026-09-14 00:00:00

Phase 5 (metering & entitlements) infrastructure:

- `processed_webhook_events` is the exactly-once + audit log for inbound
  billing webhooks (RevenueCat now). The primary key is the provider's
  event id, not a generated UUID — that's what lets the handler tell a
  redelivery apart from a new event with a single INSERT ... ON CONFLICT
  DO NOTHING (see app/api/v1/webhooks.py).
- `entitlements.last_event_ts_ms` is the ordering guard: webhooks can be
  redelivered out of order, so we only ever apply a state change from an
  event newer than the last one that actually changed the row.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "000000000003"
down_revision: str | None = "000000000002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "processed_webhook_events",
        sa.Column("event_id", sa.String(), nullable=False),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("raw_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.PrimaryKeyConstraint("event_id", name=op.f("pk_processed_webhook_events")),
    )

    op.add_column("entitlements", sa.Column("last_event_ts_ms", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column("entitlements", "last_event_ts_ms")
    op.drop_table("processed_webhook_events")
