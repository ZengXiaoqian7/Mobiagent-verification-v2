"""Durable model-event journal for App-test execution."""

from __future__ import annotations

import json
import logging
from pathlib import Path
import threading
from typing import Any, Callable, Mapping


LOGGER = logging.getLogger(__name__)
MODEL_EVENT_JOURNAL_FILE = "model_events.jsonl"


class ModelEventJournal:
    """Write ordered JSONL events and optionally mirror them to a live UI."""

    def __init__(
        self,
        path: Path,
        *,
        listener: Callable[[Mapping[str, Any]], None] | None = None,
    ) -> None:
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("", encoding="utf-8")
        self.listener = listener
        self._lock = threading.Lock()
        self._event_count = 0

    @property
    def event_count(self) -> int:
        return self._event_count

    def __call__(self, event: Mapping[str, Any]) -> None:
        with self._lock:
            self._event_count += 1
            payload = {
                **dict(event),
                "event_index": self._event_count,
            }
            with self.path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(
                    json.dumps(
                        payload,
                        ensure_ascii=False,
                        sort_keys=True,
                        default=str,
                    )
                    + "\n"
                )
        if self.listener is not None:
            try:
                self.listener(payload)
            except Exception as exc:  # noqa: BLE001 - the durable event is already saved.
                LOGGER.warning("Live model event listener failed: %s", exc)


__all__ = ["MODEL_EVENT_JOURNAL_FILE", "ModelEventJournal"]
