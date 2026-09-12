"""Langfuse tracing — a thin, no-op-safe wrapper around the v3 (OpenTelemetry
-based) Langfuse SDK.

When LANGFUSE_PUBLIC_KEY/LANGFUSE_SECRET_KEY aren't set (local dev, CI, or
any environment that hasn't opted in), every function here degrades to
doing nothing — tracing can never break a request or force tests to talk
to a real Langfuse project. Callers (app/api/v1/parse.py) depend on this
module's small interface, not on the Langfuse SDK directly, for the same
reason the LLM/STT providers sit behind their own ABCs: the tracing vendor
should be swappable too.
"""

from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Literal

from app.core.config import get_settings

try:
    from langfuse import Langfuse
except ImportError:  # pragma: no cover - optional dependency import guard
    Langfuse = None  # type: ignore[assignment, misc]


@lru_cache
def get_langfuse_client() -> "Langfuse | None":
    settings = get_settings()
    if Langfuse is None or not settings.LANGFUSE_PUBLIC_KEY or not settings.LANGFUSE_SECRET_KEY:
        return None
    return Langfuse(
        public_key=settings.LANGFUSE_PUBLIC_KEY,
        secret_key=settings.LANGFUSE_SECRET_KEY,
        host=settings.LANGFUSE_HOST,
    )


@dataclass
class Observation:
    """One Langfuse span/generation — or nothing, when tracing is off."""

    _obs: Any | None

    def update(self, **kwargs: Any) -> None:
        if self._obs is not None:
            self._obs.update(**kwargs)


@contextmanager
def traced_operation(
    name: str,
    *,
    as_type: Literal["span", "generation"] = "span",
    input: Any = None,
    model: str | None = None,
) -> Generator[Observation, None, None]:
    """Start a Langfuse observation as a context manager; no-ops entirely
    when Langfuse isn't configured.
    """
    client = get_langfuse_client()
    if client is None:
        yield Observation(_obs=None)
        return

    # mypy can't resolve the SDK's per-literal @overloads when `as_type` is
    # a variable rather than a literal at the call site — safe either way,
    # since both "span" and "generation" are valid overloads.
    obs_cm = client.start_as_current_observation(
        name=name, as_type=as_type, input=input, model=model  # type: ignore[arg-type]
    )
    with obs_cm as obs:
        yield Observation(_obs=obs)


def current_trace_id() -> str | None:
    client = get_langfuse_client()
    if client is None:
        return None
    return client.get_current_trace_id()
