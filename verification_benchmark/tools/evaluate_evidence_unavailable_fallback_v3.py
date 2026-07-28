#!/usr/bin/env python3
"""VLM fallback for deterministic failures with missing semantic XML evidence."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional


ROOT = Path(__file__).resolve().parents[2]
BENCHMARK = ROOT / "verification_benchmark"
TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(TOOLS))

from evaluate_vlm_pilot_v3 import (  # noqa: E402
    AUTHORIZED_API_KEY_ENV,
    judge,
    validate_authorized_service,
)
from verification_framework.evaluation import compute_metrics  # noqa: E402


NEGATIVE_XML_TOKENS = ("无匹配结果", "无搜索结果", "未找到", "加载失败", "网络错误")


def jsonl(path: Path) -> List[Dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def terminal_frame(trace: Path) -> int:
    indices = [int(path.stem) for path in trace.glob("*.jpg") if path.stem.isdigit()]
    if not indices:
        raise FileNotFoundError(f"no numeric screenshot: {trace}")
    return max(indices)


def creator_entity(task_description: str) -> Optional[str]:
    match = re.search(r"UP主(.+?)的个人主页", task_description)
    return match.group(1).strip() if match else None


def needs_visual_fallback(label: Dict[str, Any], deterministic: Dict[str, Any], trace: Path) -> tuple[bool, str]:
    if deterministic["verdict"] != "FAIL":
        return False, "deterministic verdict is not FAIL"
    if label.get("task_type") != "creator_homepage":
        return False, "pilot policy only covers creator_homepage semantic-XML gaps"
    frame = terminal_frame(trace)
    xml_path = trace / f"{frame}.xml"
    image_path = trace / f"{frame}.jpg"
    if not image_path.is_file() or not xml_path.is_file():
        return False, "terminal artifact missing"
    xml = xml_path.read_text(encoding="utf-8", errors="replace")
    if any(token in xml for token in NEGATIVE_XML_TOKENS):
        return False, "explicit deterministic negative evidence"
    entity = creator_entity(str(label.get("task_description") or ""))
    if entity and entity not in xml:
        return True, f"requested creator {entity!r} absent from semantic XML while screenshot is available"
    return False, "no supported evidence-unavailable signature"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--labels", required=True)
    parser.add_argument("--trace-root", required=True)
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--api-key-env", default=AUTHORIZED_API_KEY_ENV)
    parser.add_argument("--max-retries", type=int, default=2)
    args = parser.parse_args()
    try:
        validate_authorized_service(args.base_url, args.api_key_env)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    api_key = os.environ.get(args.api_key_env)
    if not api_key:
        raise SystemExit(f"required API key environment variable is unset: {args.api_key_env}")

    labels = jsonl(Path(args.labels))
    baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
    deterministic_rows = {row["trace_id"]: row for row in baseline["systems"]["enhanced_deterministic"]["results"]}
    trace_root = Path(args.trace_root)
    results = []
    request_count = 0

    for label in labels:
        trace_id = label["trace_id"]
        deterministic = deterministic_rows[trace_id]
        trace = trace_root / trace_id
        candidate, availability_reason = needs_visual_fallback(label, deterministic, trace)
        vlm = None
        error = None
        if candidate:
            frame = terminal_frame(trace)
            for attempt in range(args.max_retries + 1):
                request_count += 1
                try:
                    vlm = judge(
                        trace / f"{frame}.jpg",
                        label["task_description"],
                        label["task_type"],
                        base_url=args.base_url,
                        api_key=api_key,
                        model=args.model,
                        timeout=90,
                    )
                    error = None
                    break
                except Exception as exc:  # noqa: BLE001
                    error = f"{type(exc).__name__}: {exc}"
            verdict = "ERROR" if vlm is None else vlm["verdict"]
        else:
            verdict = deterministic["verdict"]
        results.append({
            "trace_id": trace_id,
            "ground_truth": label["ground_truth"],
            "app": label["app"],
            "task_type": label["task_type"],
            "failure_type": label.get("failure_type"),
            "deterministic_verdict": deterministic["verdict"],
            "fallback_candidate": candidate,
            "availability_reason": availability_reason,
            "vlm": vlm,
            "error": error,
            "verdict": verdict,
        })
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    latencies = []
    for row in results:
        if not row["vlm"]:
            continue
        latencies.append(row["vlm"]["latency_ms"])
        for key in usage:
            usage[key] += int(row["vlm"].get("usage", {}).get(key, 0))
    report = {
        "schema_version": "3.0-evidence-unavailable-fallback",
        "data_status": "cross-app round2 development only; no held-out claim",
        "policy": "Call VLM only for deterministic creator-homepage FAIL where terminal screenshot exists, semantic XML lacks the requested creator, and XML has no explicit negative token.",
        "model": args.model,
        "api_key_recorded": False,
        "metrics": compute_metrics(results),
        "cost": {
            "fallback_candidates": sum(row["fallback_candidate"] for row in results),
            "api_requests": request_count,
            "usage": usage,
            "latency_ms_total": sum(latencies),
            "latency_ms_mean": sum(latencies) / len(latencies) if latencies else 0,
        },
        "results": results,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"metrics": report["metrics"], "cost": report["cost"], "candidates": [row["trace_id"] for row in results if row["fallback_candidate"]]}, ensure_ascii=False, indent=2))
    return 0 if all(row["error"] is None for row in results) else 2


if __name__ == "__main__":
    raise SystemExit(main())
