"""Contract/snapshot tests for client-facing response shapes.

These don't test business logic — they lock down the SHAPE of the
responses a mobile client parses. A refactor that silently renames,
removes, or adds a field to any of these would otherwise only surface as
a confusing client-side bug much later; this makes it an immediate, loud
test failure at the point of the change instead. No DB needed — these
just introspect the Pydantic models.
"""

from app.schemas.entitlement import EntitlementSignal
from app.schemas.parse import ExtractionOut, ParseResponse, TranscribeResponse
from app.schemas.sync import ExpenseSyncOut, SyncPullResponse, SyncPushResponse, SyncPushResult


def _field_names(model: type) -> set[str]:
    return set(model.model_fields.keys())


def test_parse_response_contract() -> None:
    assert _field_names(ParseResponse) == {"status", "extraction", "trace_id", "entitlement"}


def test_transcribe_response_contract() -> None:
    assert _field_names(TranscribeResponse) == {
        "status",
        "extraction",
        "trace_id",
        "entitlement",
        "transcript",
    }


def test_extraction_out_contract() -> None:
    assert _field_names(ExtractionOut) == {
        "amount_original",
        "currency_original",
        "amount_base",
        "currency_base",
        "category",
        "category_is_known",
        "payment_method",
        "merchant",
        "spent_at",
        "confidence",
    }


def test_entitlement_signal_contract() -> None:
    assert _field_names(EntitlementSignal) == {
        "tier",
        "status",
        "period",
        "used",
        "quota",
        "over_quota",
    }


def test_sync_push_response_contract() -> None:
    assert _field_names(SyncPushResponse) == {"results", "server_high_water", "entitlement"}


def test_sync_push_result_contract() -> None:
    assert _field_names(SyncPushResult) == {"id", "status", "reason", "server_seq"}


def test_sync_pull_response_contract() -> None:
    assert _field_names(SyncPullResponse) == {"records", "next_cursor", "has_more"}


def test_expense_sync_out_contract() -> None:
    assert _field_names(ExpenseSyncOut) == {
        "id",
        "user_id",
        "device_id",
        "amount_original",
        "currency_original",
        "amount_base",
        "currency_base",
        "category",
        "payment_method",
        "merchant",
        "note",
        "spent_at",
        "created_at",
        "updated_at",
        "parse_status",
        "raw_transcript",
        "client_rev",
        "deleted_at",
        "server_seq",
    }
