"""Shared StrEnums for fixed-domain columns.

Used both as the Python-side validation type in Pydantic schemas and as the
source of truth for the CHECK constraint values on the corresponding
SQLAlchemy VARCHAR columns (see app/models). Kept out of Postgres native
ENUM types deliberately — altering those in Alembic is painful.
"""

from enum import StrEnum


class Platform(StrEnum):
    IOS = "ios"
    ANDROID = "android"


class ParseStatus(StrEnum):
    LOCAL = "local"
    ENRICHED = "enriched"
    FAILED = "failed"


class Tier(StrEnum):
    FREE = "free"
    PRO = "pro"


class EntitlementStatus(StrEnum):
    ACTIVE = "active"
    EXPIRED = "expired"
    IN_GRACE = "in_grace"
    CANCELLED = "cancelled"


class Store(StrEnum):
    APP_STORE = "app_store"
    PLAY_STORE = "play_store"
    STRIPE = "stripe"
    LEMONSQUEEZY = "lemonsqueezy"
