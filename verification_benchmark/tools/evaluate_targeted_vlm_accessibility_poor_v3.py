#!/usr/bin/env python3
"""Horizon-only targeted VLM arm for accessibility-poor XML stress v1."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT))

from evaluate_vlm_pilot_v3 import (  # noqa: E402
    AUTHORIZED_API_KEY_ENV,
    judge,
    validate_authorized_service,
)
from verification_framework.evaluation import compute_metrics  # noqa: E402


PROMPT_VERSION = "accessibility-poor-targeted-v1"


def jsonl(path: Path) -> List[Dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def enhanced_rows(path: Path) -> Dict[str, Dict[str, Any]]:
    report = json.loads(path.read_text(encoding="utf-8"))
    try:
        rows = report["systems"]["enhanced_deterministic"]["results"]
    except (KeyError, TypeError) as exc:
        raise ValueError(f"invalid deterministic report shape: {path}") from exc
    if not isinstance(rows, list) or not rows:
        raise ValueError(f"deterministic report has no enhanced rows: {path}")
    return {str(row["trace_id"]): row for row in rows}


def correct_for_truth(verdict: str, truth: str) -> bool:
    return (truth == "success" and verdict == "PASS") or (truth == "fail" and verdict == "FAIL")


def eligibility(sample: Dict[str, Any], label: Dict[str, Any], source: Dict[str, Any], redacted: Dict[str, Any]) -> Tuple[bool, str]:
    if not sample.get("screenshot_sufficient"):
        return False, "terminal screenshot is not preregistered as sufficient"
    if not correct_for_truth(str(source.get("verdict")), str(label.get("ground_truth"))):
        return False, "original-XML deterministic control is not correct"
    if str(redacted.get("verdict")) not in {"FAIL", "ABSTAIN"}:
        return False, "redacted-XML deterministic decision is not an evidence-loss rejection"
    if label.get("ground_truth") != "success":
        return False, "stress-test policy never calls VLM on known failure rows"
    return True, "original control is correct; redaction causes a screenshot-sufficient success rejection"


def targeted_prompt(task_description: str, task_type: str) -> str:
    return f"""You are a conservative independent verifier for one mobile-agent terminal screenshot.
Judge only visible screenshot evidence; XML is deliberately unavailable. Do not trust an agent self-report.

Task: {task_description}
Task family: {task_type}

Return JSON only with SATISFIED, VIOLATED, or UNKNOWN for each criterion.
- outcome.target_entity: every task-required entity is visibly represented. For a search task, a visible matching query plus loaded results containing the entity can satisfy this even if the page has mixed result categories.
- outcome.terminal_structure: the requested final page structure is visibly reached. A search-result user card is not a creator homepage; a creator homepage needs its own profile/homepage surface rather than merely a search tab or result card.
- negative.stable_terminal: required task-critical content is loaded and usable. A spinner, dim loading layer, or missing task-critical result is VIOLATED. Ancillary placeholders alone are not a violation when endpoints and the required actionable terminal controls are visibly complete.
- negative.no_blocking_overlay: no login, permission, error, or other modal blocks completion.

Use UNKNOWN rather than guessing. Cite only visible evidence in a short reason.
Schema: {{"criteria":{{"outcome.target_entity":"...","outcome.terminal_structure":"...","negative.stable_terminal":"...","negative.no_blocking_overlay":"..."}},"reason":"brief visible-evidence reason"}}"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--labels", required=True, type=Path)
    parser.add_argument("--source-report", required=True, type=Path)
    parser.add_argument("--redacted-report", required=True, type=Path)
    parser.add_argument("--trace-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--api-key-env", default=AUTHORIZED_API_KEY_ENV)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    try:
        validate_authorized_service(args.base_url, args.api_key_env)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    if not args.dry_run and not os.environ.get(args.api_key_env):
        raise SystemExit(f"required API key environment variable is unset: {args.api_key_env}")
    if args.output.exists():
        raise SystemExit(f"refusing to overwrite output: {args.output}")

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    labels = {row["trace_id"]: row for row in jsonl(args.labels)}
    source_rows, redacted_rows = enhanced_rows(args.source_report), enhanced_rows(args.redacted_report)
    results = []
    candidates = []
    for sample in manifest["samples"]:
        trace_id = sample["trace_id"]
        if trace_id not in labels or trace_id not in source_rows or trace_id not in redacted_rows:
            raise ValueError(f"missing input row: {trace_id}")
        label, source, redacted = labels[trace_id], source_rows[trace_id], redacted_rows[trace_id]
        candidate, reason = eligibility(sample, label, source, redacted)
        frame = int(sample["terminal_frame"])
        image = args.trace_root / trace_id / f"{frame}.jpg"
        if candidate and not image.is_file():
            raise FileNotFoundError(f"candidate terminal screenshot missing: {image}")
        row = {
            "trace_id": trace_id,
            "ground_truth": label["ground_truth"],
            "app": label["app"],
            "task_type": label["task_type"],
            "failure_type": label.get("failure_type"),
            "terminal_frame": frame,
            "source_deterministic_verdict": source["verdict"],
            "redacted_deterministic_verdict": redacted["verdict"],
            "fallback_candidate": candidate,
            "eligibility_reason": reason,
            "vlm": None,
            "error": None,
            "verdict": redacted["verdict"],
        }
        if candidate:
            candidates.append(trace_id)
            if not args.dry_run:
                row["vlm"] = judge(
                    image, str(label["task_description"]), str(label["task_type"]),
                    base_url=args.base_url, api_key=os.environ.get(args.api_key_env, ""),
                    model=args.model, timeout=90,
                    prompt_override=targeted_prompt(str(label["task_description"]), str(label["task_type"])),
                )
                row["verdict"] = row["vlm"]["verdict"]
        results.append(row)

    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    latency_ms = 0.0
    for row in results:
        if row["vlm"]:
            latency_ms += float(row["vlm"]["latency_ms"])
            for key in usage:
                usage[key] += int(row["vlm"].get("usage", {}).get(key, 0))
    report = {
        "schema_version": "3.0-accessibility-poor-targeted-vlm",
        "data_status": "derived challenge-v1 development mechanism stress test only; not held-out",
        "prompt_version": PROMPT_VERSION,
        "base_url": args.base_url,
        "model": args.model,
        "api_key_env": args.api_key_env,
        "api_key_recorded": False,
        "dry_run": args.dry_run,
        "candidate_count": len(candidates),
        "candidates": candidates,
        "metrics": compute_metrics(results),
        "cost": {"api_requests": 0 if args.dry_run else len(candidates), "usage": usage, "latency_ms_total": latency_ms},
        "results": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"dry_run": args.dry_run, "candidate_count": len(candidates), "candidates": candidates}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
