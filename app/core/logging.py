"""Structured JSON logging.

One JSON object per line, ISO timestamps, with per-request correlation:
`request_id_var`/`trace_id_var` are contextvars set by
app/core/middleware.py (request id) and app/api/v1/parse.py (Langfuse
trace id) for the duration of a request/parse call, and every log record
emitted in that window automatically carries them via `_ContextFilter` —
no need to pass request_id through every function call by hand.

PII discipline: this module never decides what gets logged — callers do,
via `extra={...}`. The rule callers must follow (documented, not
enforced by code, since Python logging can't intercept string content):
never log raw audio (it never reaches disk or memory-as-a-variable past
app/api/v1/parse.py anyway), never log JWTs/webhook secrets/API keys, and
only log `raw_transcript`/transcript text when settings.LOG_CAPTURE_
TRANSCRIPT is true (same gate philosophy as LANGFUSE_CAPTURE_TRANSCRIPT).
`redact_headers` below exists for the one place headers might get logged.
"""

import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime

from app.core.config import get_settings

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
trace_id_var: ContextVar[str | None] = ContextVar("trace_id", default=None)

_SENSITIVE_HEADERS = {"authorization", "cookie", "x-api-key"}
_REDACTED = "***REDACTED***"

# Attributes every stdlib LogRecord already has — anything else on a
# record came from an explicit `extra={...}` (or our own context filter)
# and belongs in the JSON payload.
_STANDARD_LOG_RECORD_ATTRS = frozenset(
    {
        "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
        "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
        "created", "msecs", "relativeCreated", "thread", "threadName",
        "processName", "process", "taskName", "message",
    }
)


def redact_headers(headers: dict[str, str]) -> dict[str, str]:
    """Use this instead of logging a headers mapping directly."""
    return {
        key: (_REDACTED if key.lower() in _SENSITIVE_HEADERS else value)
        for key, value in headers.items()
    }


class _ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        record.trace_id = trace_id_var.get()
        return True


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in _STANDARD_LOG_RECORD_ATTRS or key.startswith("_") or value is None:
                continue
            payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging() -> None:
    settings = get_settings()

    handler = logging.StreamHandler(sys.stdout)
    if settings.LOG_JSON:
        handler.setFormatter(JSONFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        )
    handler.addFilter(_ContextFilter())

    root = logging.getLogger()
    root.setLevel(settings.LOG_LEVEL.upper())
    root.handlers = [handler]

    # Quiet down noisy third-party loggers by default.
    logging.getLogger("uvicorn.access").setLevel(settings.LOG_LEVEL.upper())
