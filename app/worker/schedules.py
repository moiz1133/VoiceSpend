"""Celery beat schedule — kept separate from celery_app.py so the schedule
itself is easy to find and diff independently of broker/serialization config.
"""

from typing import Any

from celery.schedules import crontab

from app.core.config import get_settings


def build_beat_schedule() -> dict[str, dict[str, Any]]:
    settings = get_settings()
    return {
        "ingest-daily-fx": {
            "task": "app.worker.tasks.fx.ingest_daily_fx",
            "schedule": crontab(hour=settings.FX_INGEST_HOUR_UTC, minute=0),
        },
    }
