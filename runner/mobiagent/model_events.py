"""Context-bound structured events for original MobiAgent model calls.

The original runner remains usable on its own: when no sink is installed this
module is a no-op. App-test execution installs a sink for one business attempt
so Decider and Grounder calls can be correlated with the step and persisted
without copying prompts, screenshots, or credentials into the event stream.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import logging
import time
from typing import Any, Callable, Iterator, Mapping


MODEL_EVENT_SCHEMA_VERSION = "mobiagent-model-event-v1"
ModelEventSink = Callable[[Mapping[str, Any]], None]

_SINK: ContextVar[ModelEventSink | None] = ContextVar(
    "mobiagent_model_event_sink",
    default=None,
)
_CONTEXT: ContextVar[Mapping[str, Any]] = ContextVar(
    "mobiagent_model_event_context",
    default={},
)
LOGGER = logging.getLogger(__name__)


@contextmanager
def model_event_scope(
    sink: ModelEventSink | None,
    **context: Any,
) -> Iterator[None]:
    """Bind a sink and audit context to nested Decider/Grounder calls."""

    parent = dict(_CONTEXT.get())
    parent.update({key: value for key, value in context.items() if value is not None})
    sink_token = _SINK.set(sink)
    context_token = _CONTEXT.set(parent)
    try:
        yield
    finally:
        _CONTEXT.reset(context_token)
        _SINK.reset(sink_token)


def emit_model_event(event_type: str, **fields: Any) -> None:
    """Emit one best-effort event without changing model-call semantics."""

    sink = _SINK.get()
    if sink is None:
        return
    payload = {
        "schema_version": MODEL_EVENT_SCHEMA_VERSION,
        "timestamp_ms": int(time.time() * 1000),
        **dict(_CONTEXT.get()),
        "event_type": str(event_type),
        **fields,
    }
    try:
        sink(payload)
    except Exception as exc:  # noqa: BLE001 - debug output must not trigger model retries.
        LOGGER.warning("MobiAgent model event sink failed: %s", exc)


__all__ = [
    "MODEL_EVENT_SCHEMA_VERSION",
    "ModelEventSink",
    "emit_model_event",
    "model_event_scope",
]
