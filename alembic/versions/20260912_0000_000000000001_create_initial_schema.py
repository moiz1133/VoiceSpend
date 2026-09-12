"""create initial schema

Revision ID: 000000000001
Revises:
Create Date: 2026-09-12 00:00:00

Creates all Phase 2 tables (users, devices, expenses, categories, fx_rates,
entitlements, usage_counters) plus their indexes and constraints, and seeds
the default system categories. This is the one real initial migration —
there is no separate empty/no-op revision to carry forward.
"""

import uuid
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "000000000001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SYSTEM_CATEGORIES = [
    ("Food", "food"),
    ("Transport", "transport"),
    ("Groceries", "groceries"),
    ("Bills", "bills"),
    ("Shopping", "shopping"),
    ("Health", "health"),
    ("Entertainment", "entertainment"),
    ("Other", "other"),
]


def upgrade() -> None:
    op.create_table(
        "fx_rates",
        sa.Column("currency_code", sa.CHAR(length=3), nullable=False),
        sa.Column("rate_date", sa.Date(), nullable=False),
        sa.Column("rate_per_usd", sa.Numeric(precision=18, scale=8), nullable=False),
        sa.Column("source", sa.String(), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fx_rates")),
        sa.UniqueConstraint(
            "currency_code", "rate_date", name="uq_fx_rates_currency_code_rate_date"
        ),
    )
    op.create_index(
        "ix_fx_rates_currency_code_rate_date",
        "fx_rates",
        ["currency_code", sa.literal_column("rate_date DESC")],
        unique=False,
    )

    op.create_table(
        "users",
        sa.Column("auth_provider_id", sa.String(), nullable=True),
        sa.Column("email", sa.String(), nullable=True),
        sa.Column("is_anonymous", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("base_currency", sa.CHAR(length=3), server_default="USD", nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("auth_provider_id", name=op.f("uq_users_auth_provider_id")),
        sa.UniqueConstraint("email", name=op.f("uq_users_email")),
    )

    op.create_table(
        "categories",
        sa.Column("user_id", sa.UUID(), nullable=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("icon", sa.String(), nullable=True),
        sa.Column("is_system", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_categories_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_categories")),
    )
    # A plain UNIQUE(user_id, name) would not dedupe system rows, since
    # Postgres treats NULL user_id as distinct on every row — two partial
    # unique indexes cover the user-owned and system-global cases separately.
    op.create_index(
        "uq_categories_name_system",
        "categories",
        ["name"],
        unique=True,
        postgresql_where=sa.text("user_id IS NULL"),
    )
    op.create_index(
        "uq_categories_user_id_name",
        "categories",
        ["user_id", "name"],
        unique=True,
        postgresql_where=sa.text("user_id IS NOT NULL"),
    )

    op.create_table(
        "devices",
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("platform", sa.String(), nullable=False),
        sa.Column("push_token", sa.String(), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("platform IN ('ios', 'android')", name=op.f("ck_devices_platform")),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_devices_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_devices")),
    )
    op.create_index("ix_devices_user_id", "devices", ["user_id"], unique=False)

    op.create_table(
        "entitlements",
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("tier", sa.String(), server_default="free", nullable=False),
        sa.Column("status", sa.String(), server_default="active", nullable=False),
        sa.Column("product_id", sa.String(), nullable=True),
        sa.Column("store", sa.String(), nullable=True),
        sa.Column("revenuecat_app_user_id", sa.String(), nullable=True),
        sa.Column("current_period_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('active', 'expired', 'in_grace', 'cancelled')",
            name=op.f("ck_entitlements_status"),
        ),
        sa.CheckConstraint(
            "store IN ('app_store', 'play_store', 'stripe', 'lemonsqueezy')",
            name=op.f("ck_entitlements_store"),
        ),
        sa.CheckConstraint("tier IN ('free', 'pro')", name=op.f("ck_entitlements_tier")),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_entitlements_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_entitlements")),
        sa.UniqueConstraint("user_id", name=op.f("uq_entitlements_user_id")),
    )

    op.create_table(
        "usage_counters",
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("period", sa.String(), nullable=False),
        sa.Column("log_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_usage_counters_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_usage_counters")),
        sa.UniqueConstraint("user_id", "period", name="uq_usage_counters_user_id_period"),
    )

    op.create_table(
        "expenses",
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("device_id", sa.UUID(), nullable=False),
        sa.Column("amount_original", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("currency_original", sa.CHAR(length=3), nullable=False),
        sa.Column("amount_base", sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column("currency_base", sa.CHAR(length=3), nullable=True),
        sa.Column("category", sa.String(), nullable=True),
        sa.Column("payment_method", sa.String(), nullable=True),
        sa.Column("merchant", sa.String(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("spent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("parse_status", sa.String(), server_default="local", nullable=False),
        sa.Column("raw_transcript", sa.Text(), nullable=True),
        sa.Column("client_rev", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "parse_status IN ('local', 'enriched', 'failed')", name=op.f("ck_expenses_parse_status")
        ),
        sa.ForeignKeyConstraint(
            ["device_id"],
            ["devices.id"],
            name=op.f("fk_expenses_device_id_devices"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_expenses_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_expenses")),
    )
    op.create_index(
        "ix_expenses_user_id_spent_at",
        "expenses",
        ["user_id", sa.literal_column("spent_at DESC")],
        unique=False,
    )
    op.create_index(
        "ix_expenses_user_id_updated_at", "expenses", ["user_id", "updated_at"], unique=False
    )

    # Seed default system categories (user_id IS NULL == global default).
    categories_table = sa.table(
        "categories",
        sa.column("id", sa.UUID()),
        sa.column("user_id", sa.UUID()),
        sa.column("name", sa.String()),
        sa.column("icon", sa.String()),
        sa.column("is_system", sa.Boolean()),
    )
    op.bulk_insert(
        categories_table,
        [
            {"id": uuid.uuid4(), "user_id": None, "name": name, "icon": icon, "is_system": True}
            for name, icon in SYSTEM_CATEGORIES
        ],
    )


def downgrade() -> None:
    op.drop_index("ix_expenses_user_id_updated_at", table_name="expenses")
    op.drop_index("ix_expenses_user_id_spent_at", table_name="expenses")
    op.drop_table("expenses")
    op.drop_table("usage_counters")
    op.drop_table("entitlements")
    op.drop_index("ix_devices_user_id", table_name="devices")
    op.drop_table("devices")
    op.drop_index(
        "uq_categories_user_id_name",
        table_name="categories",
        postgresql_where=sa.text("user_id IS NOT NULL"),
    )
    op.drop_index(
        "uq_categories_name_system",
        table_name="categories",
        postgresql_where=sa.text("user_id IS NULL"),
    )
    op.drop_table("categories")
    op.drop_table("users")
    op.drop_index("ix_fx_rates_currency_code_rate_date", table_name="fx_rates")
    op.drop_table("fx_rates")
