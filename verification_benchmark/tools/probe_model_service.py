#!/usr/bin/env python3
"""Run a minimal, sanitized model-service probe without touching a device."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any

from app_test_agent.model_client import (
    extract_json_object,
    model_config_from_env,
    post_chat_completion,
)


def run_probe(*, timeout: int = 90) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        config = model_config_from_env(
            base_url_names=("MOBIAGENT_BASE_URL",),
            model_names=("MOBIAGENT_MODEL",),
        )
        response_text = post_chat_completion(
            config,
            messages=[
                {
                    "role": "user",
                    "content": (
                        'Return exactly this JSON object and no other text: '
                        '{"status":"ok"}'
                    ),
                }
            ],
            max_tokens=128,
            timeout=timeout,
        )
        payload = extract_json_object(response_text)
        if payload.get("status") != "ok":
            raise ValueError("model returned an unexpected structured response")
        return {
            "schema_version": "model-service-probe-v1",
            "status": "PASS",
            "model": config.model,
            "wire_api": config.wire_api,
            "reasoning_effort": config.reasoning_effort,
            "store": config.store,
            "elapsed_ms": round((time.perf_counter() - started) * 1000),
            "response_sha256": hashlib.sha256(response_text.encode("utf-8")).hexdigest(),
            "response_character_count": len(response_text),
            "device_interaction": "NONE",
        }
    except Exception as exc:  # noqa: BLE001 - emit a sanitized failure report
        status_match = re.search(r"HTTP\s+(\d{3})", str(exc))
        return {
            "schema_version": "model-service-probe-v1",
            "status": "FAIL",
            "error_type": type(exc).__name__,
            "http_status": int(status_match.group(1)) if status_match else None,
            "elapsed_ms": round((time.perf_counter() - started) * 1000),
            "device_interaction": "NONE",
        }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Probe the configured MobiAgent model service without printing secrets or response text."
    )
    parser.add_argument("--timeout", type=int, default=90)
    parser.add_argument("--output-json", type=Path)
    args = parser.parse_args()

    result = run_probe(timeout=args.timeout)
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output_json:
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
