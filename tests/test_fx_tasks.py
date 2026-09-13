"""Tests for the Celery task wrappers in app/worker/tasks/fx.py.

conftest.py sets celery_app.conf.task_always_eager = True, so calling a
task directly here never needs a live broker. ingest_daily_fx's failure
path is safe to exercise for real (no DB write happens before the
provider raises); its success path is exercised indirectly via the
app/services/fx/ingest.py and app/services/fx/recompute.py unit tests
against the shared db_session fixture instead, to avoid this task's own
independent (non-test-transactional) DB session leaking durable rows.
"""

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Device, Expense, User
from app.worker.tasks import fx as fx_tasks

pytestmark = pytest.mark.pg

class _FailingProvider:
    provider_name = "failing"
    base_currency = "USD"

    async def fetch_latest(self) -> dict[str, Decimal]:
        raise ConnectionError("simulated fx provider outage")

    async def fetch_for_date(self, d: date) -> dict[str, Decimal]:
        raise ConnectionError("simulated fx provider outage")


def test_daily_ingest_provider_failure_logs_metric_and_retries(monkeypatch) -> None:
    monkeypatch.setattr(fx_tasks, "get_fx_provider", lambda: _FailingProvider())

    failure_calls: list[str] = []
    monkeypatch.setattr(
        fx_tasks, "record_fx_ingest_failure", lambda error: failure_calls.append(error)
    )
    # Assert directly on the module's logger call rather than through
    # pytest's caplog/root-handler plumbing — app/core/logging.py's
    # configure_logging() replaces root.handlers wholesale whenever any
    # other test's FastAPI app lifespan has run, which is order-dependent
    # and would make a caplog-based assertion here flaky.
    logged: list[str] = []
    monkeypatch.setattr(fx_tasks.logger, "exception", lambda msg, *a, **k: logged.append(msg))

    # called_directly=True (a bare call, not via the worker/broker) makes
    # Celery's retry() re-raise the original exception instead of trying to
    # actually reschedule via the broker — safe to assert on here.
    with pytest.raises(Exception, match="simulated fx provider outage"):
        fx_tasks.ingest_daily_fx()

    assert failure_calls == ["simulated fx provider outage"]
    assert logged == ["fx daily ingest failed"]


async def test_handle_base_currency_changed_nulls_and_enqueues(
    monkeypatch, db_session: AsyncSession, user: User, device: Device
) -> None:
    expense = Expense(
        user_id=user.id,
        device_id=device.id,
        amount_original=Decimal("10.00"),
        currency_original="EUR",
        amount_base=Decimal("11.00"),
        currency_base="USD",
        spent_at=datetime.now(UTC),
    )
    db_session.add(expense)
    await db_session.commit()

    enqueued: list[str] = []
    monkeypatch.setattr(
        fx_tasks,
        "enqueue_recompute_for_user",
        lambda uid: enqueued.append(str(uid)),
    )

    count = await fx_tasks.handle_base_currency_changed(db_session, user.id)

    assert count == 1
    assert enqueued == [str(user.id)]
    refreshed = (
        await db_session.execute(select(Expense).where(Expense.id == expense.id))
    ).scalar_one()
    assert refreshed.amount_base is None
    assert refreshed.currency_base is None


def test_enqueue_recompute_for_user_calls_task_delay(monkeypatch) -> None:
    calls: list[str | None] = []
    monkeypatch.setattr(
        fx_tasks.recompute_amount_base, "delay", lambda user_id=None: calls.append(user_id)
    )

    user_id = UUID("11111111-1111-1111-1111-111111111111")
    fx_tasks.enqueue_recompute_for_user(user_id)

    assert calls == [str(user_id)]
