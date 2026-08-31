"""Console compatibility helpers for command-line model diagnostics."""

from __future__ import annotations

import sys
from typing import Any, Iterable


def configure_utf8_console(streams: Iterable[Any] | None = None) -> None:
    """Prefer UTF-8 for terminal output while leaving unsupported streams alone."""

    selected = streams if streams is not None else (sys.stdout, sys.stderr)
    for stream in selected:
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            # Closed, detached, or host-owned streams cannot always be changed.
            continue
