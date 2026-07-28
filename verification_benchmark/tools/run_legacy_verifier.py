#!/usr/bin/env python3
"""Isolated bridge for running an unmodified MobiFlow checkout.

This file is intentionally outside MobiFlow.  Point --mobiflow-root at either a
historical worktree or the current checkout; only public avdag APIs are used.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mobiflow-root", required=True)
    parser.add_argument("--rule", required=True)
    parser.add_argument("--trace", required=True)
    args = parser.parse_args()

    root = Path(args.mobiflow_root).resolve()
    sys.path.insert(0, str(root))
    started = time.perf_counter()
    payload = {"raw_ok": None, "manual_review_needed": False, "reason": None, "matched": [], "error": None}
    try:
        from avdag.types import VerifierOptions
        from avdag.verifier import verify_task_folder

        result = verify_task_folder(args.rule, args.trace, VerifierOptions(ocr=None, llm=None))
        payload.update({
            "raw_ok": bool(result.ok),
            "manual_review_needed": bool(getattr(result, "manual_review_needed", False)),
            "reason": getattr(result, "reason", None),
            "matched": [
                {"node_id": item.node_id, "frame_index": item.frame_index}
                for item in getattr(result, "matched", [])
            ],
        })
    except Exception as exc:  # noqa: BLE001 - serialized as verifier ERROR
        payload["error"] = f"{type(exc).__name__}: {exc}"
    payload["latency_ms"] = round((time.perf_counter() - started) * 1000, 3)
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if payload["error"] is None else 2


if __name__ == "__main__":
    raise SystemExit(main())
