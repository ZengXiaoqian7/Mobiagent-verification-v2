#!/usr/bin/env python3
"""Run a real vision-language terminal-state judge on the development pilot.

Ground Truth and criterion labels are never included in the model request.
The output records model, latency and token usage, but never the API key.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests


ROOT = Path(__file__).resolve().parents[2]
BENCHMARK = ROOT / "verification_benchmark"
sys.path.insert(0, str(ROOT))

from verification_framework.evaluation import compute_metrics  # noqa: E402


CRITERIA = (
    "outcome.target_entity",
    "outcome.terminal_structure",
    "negative.stable_terminal",
    "negative.no_blocking_overlay",
)
VALID_STATUSES = {"SATISFIED", "VIOLATED", "UNKNOWN"}
AUTHORIZED_BASE_URL = "https://api.horizon1123.top/v1"
AUTHORIZED_API_KEY_ENV = "MOBIAGENT_API_KEY"


def validate_authorized_service(base_url: str, api_key_env: str) -> None:
    """Fail closed unless the explicitly approved model service is selected."""
    if base_url.rstrip("/") != AUTHORIZED_BASE_URL:
        raise ValueError(
            f"unauthorized model endpoint: {base_url!r}; "
            f"expected {AUTHORIZED_BASE_URL!r}"
        )
    if api_key_env != AUTHORIZED_API_KEY_ENV:
        raise ValueError(
            f"unauthorized API key environment variable: {api_key_env!r}; "
            f"expected {AUTHORIZED_API_KEY_ENV!r}"
        )


def jsonl(path: Path) -> List[Dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def deterministic_rows(report: Dict[str, Any], source_path: Path) -> Dict[str, Dict[str, Any]]:
    details = report.get("details")
    if not isinstance(details, list) or not details:
        raise ValueError(
            f"deterministic report must contain a non-empty 'details' list: {source_path}"
        )
    rows: Dict[str, Dict[str, Any]] = {}
    for index, row in enumerate(details):
        if not isinstance(row, dict) or not row.get("trace_id") or not isinstance(row.get("criteria"), dict):
            raise ValueError(
                f"deterministic report details[{index}] must contain trace_id and criteria: {source_path}"
            )
        rows[row["trace_id"]] = row
    return rows


def extract_json(text: str) -> Dict[str, Any]:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.IGNORECASE)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        if not match:
            raise
        return json.loads(match.group(0))


def verdict(criteria: Dict[str, str]) -> str:
    if any(criteria.get(name) == "VIOLATED" for name in CRITERIA):
        return "FAIL"
    if any(criteria.get(name) != "SATISFIED" for name in CRITERIA):
        return "ABSTAIN"
    return "PASS"


def prompt(task_description: str, task_type: str) -> str:
    return f"""You are an independent verifier for a mobile-agent terminal screenshot.
Judge only visible screenshot evidence. Do not trust an agent's self-report. The user task is:
{task_description}

Task family: {task_type}
Return one JSON object only. Use SATISFIED, VIOLATED, or UNKNOWN for each criterion:
- outcome.target_entity: every entity required by the task is visibly represented in the terminal state.
- outcome.terminal_structure: the page type/structure is the requested final state, not an editor, search suggestion, unrelated page, or merely a partial step.
- negative.stable_terminal: the page is fully loaded; no spinner, skeleton, dim loading layer, or unfinished blank result remains. SATISFIED means stable.
- negative.no_blocking_overlay: no login, permission, error, or other modal blocks completion. SATISFIED means no blocker.

If text is unreadable or evidence is insufficient, use UNKNOWN rather than guessing.
Schema:
{{"criteria":{{"outcome.target_entity":"...","outcome.terminal_structure":"...","negative.stable_terminal":"...","negative.no_blocking_overlay":"..."}},"reason":"brief visible-evidence reason"}}"""


def judge(
    image: Path,
    task_description: str,
    task_type: str,
    *,
    base_url: str,
    api_key: str,
    model: str,
    timeout: float,
    prompt_override: Optional[str] = None,
) -> Dict[str, Any]:
    encoded = base64.b64encode(image.read_bytes()).decode("ascii")
    payload = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt_override or prompt(task_description, task_type)},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encoded}"}},
            ],
        }],
        "temperature": 0,
        "max_tokens": 500,
        "response_format": {"type": "json_object"},
    }
    started = time.perf_counter()
    response = requests.post(
        base_url.rstrip("/") + "/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=payload,
        timeout=timeout,
    )
    latency = (time.perf_counter() - started) * 1000
    response.raise_for_status()
    body = response.json()
    content = body["choices"][0]["message"]["content"]
    parsed = extract_json(content)
    criteria = parsed.get("criteria") or {}
    normalized = {name: str(criteria.get(name) or "UNKNOWN").upper() for name in CRITERIA}
    for name, value in normalized.items():
        if value not in VALID_STATUSES:
            normalized[name] = "UNKNOWN"
    return {
        "criteria": normalized,
        "verdict": verdict(normalized),
        "reason": str(parsed.get("reason") or ""),
        "latency_ms": round(latency, 3),
        "usage": body.get("usage") or {},
        "response_id": body.get("id"),
    }


def macro_f1(pairs: List[tuple[str, str]]) -> float:
    labels = sorted({value for pair in pairs for value in pair})
    scores = []
    for label in labels:
        tp = sum(truth == label and pred == label for truth, pred in pairs)
        fp = sum(truth != label and pred == label for truth, pred in pairs)
        fn = sum(truth == label and pred != label for truth, pred in pairs)
        precision = 0 if tp + fp == 0 else tp / (tp + fp)
        recall = 0 if tp + fn == 0 else tp / (tp + fn)
        scores.append(0 if precision + recall == 0 else 2 * precision * recall / (precision + recall))
    return sum(scores) / len(scores) if scores else 0.0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trace-root", required=True)
    parser.add_argument("--deterministic-report", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--api-key-env", default=AUTHORIZED_API_KEY_ENV)
    parser.add_argument("--timeout", type=float, default=90)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--max-retries", type=int, default=2)
    parser.add_argument("--request-count-override", type=int)
    args = parser.parse_args()
    try:
        validate_authorized_service(args.base_url, args.api_key_env)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    api_key = os.environ.get(args.api_key_env)
    if not api_key:
        raise SystemExit(f"required API key environment variable is unset: {args.api_key_env}")

    annotations = jsonl(BENCHMARK / "benchmark_v3/annotations/pilot_annotations.jsonl")
    source = {row["trace_id"]: row for row in jsonl(BENCHMARK / "labels_cross_app_heldout_challenge_v1_challenge.jsonl")}
    deterministic_path = Path(args.deterministic_report)
    deterministic = json.loads(deterministic_path.read_text(encoding="utf-8"))
    try:
        deterministic_by_trace = deterministic_rows(deterministic, deterministic_path)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    selected = annotations[: args.limit] if args.limit else annotations
    results = []
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    existing: Dict[str, Dict[str, Any]] = {}
    if args.resume and output.is_file():
        prior = json.loads(output.read_text(encoding="utf-8"))
        existing = {row["trace_id"]: row for row in prior.get("results", []) if row.get("error") is None}

    for index, annotation in enumerate(selected, 1):
        trace_id = annotation["trace_id"]
        row = source[trace_id]
        image = Path(args.trace_root) / trace_id / f"{annotation['terminal_frame']}.jpg"
        if trace_id in existing:
            print(f"[{index}/{len(selected)}] cached {trace_id}", flush=True)
            results.append(existing[trace_id])
            continue
        print(f"[{index}/{len(selected)}] {trace_id}", flush=True)
        error = None
        for attempt in range(args.max_retries + 1):
            try:
                vlm = judge(
                    image,
                    row["task_description"],
                    row["task_type"],
                    base_url=args.base_url,
                    api_key=api_key,
                    model=args.model,
                    timeout=args.timeout,
                )
                error = None
                break
            except Exception as exc:  # noqa: BLE001 - explicit ERROR, never silent fallback
                error = f"{type(exc).__name__}: {exc}"
                print(f"  attempt {attempt + 1} failed: {type(exc).__name__}", flush=True)
        if error is not None:
            vlm = {"criteria": {name: "UNKNOWN" for name in CRITERIA}, "verdict": "ERROR", "reason": "", "latency_ms": 0.0, "usage": {}}
        results.append({
            "trace_id": trace_id,
            "ground_truth": row["ground_truth"],
            "app": row["app"],
            "task_type": row["task_type"],
            "failure_type": row.get("failure_type"),
            "terminal_frame": annotation["terminal_frame"],
            "vlm": vlm,
            "error": error,
            "truth_criteria": {key: value["status"] for key, value in annotation["criteria"].items() if key in CRITERIA},
            "deterministic_criteria": {key: deterministic_by_trace[trace_id]["criteria"][key] for key in CRITERIA},
        })
        output.write_text(json.dumps({"status": "partial", "results": results}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    vlm_rows = [{**{key: row[key] for key in ("trace_id", "ground_truth", "app", "task_type", "failure_type")}, "verdict": row["vlm"]["verdict"]} for row in results]
    fallback_rows = []
    confirmation_rows = []
    for row in results:
        base = {key: row[key] for key in ("trace_id", "ground_truth", "app", "task_type", "failure_type")}
        deterministic_verdict = "PASS" if all(value == "SATISFIED" for value in row["deterministic_criteria"].values()) else "FAIL"
        fallback_rows.append({**base, "verdict": deterministic_verdict})
        confirmation = row["vlm"]["verdict"] if deterministic_verdict == "PASS" else "FAIL"
        confirmation_rows.append({**base, "verdict": confirmation})
    pairs: Dict[str, List[tuple[str, str]]] = defaultdict(list)
    for row in results:
        for name in CRITERIA:
            pairs[name].append((row["truth_criteria"][name], row["vlm"]["criteria"][name]))
    total_usage = Counter()
    for row in results:
        for key, value in row["vlm"].get("usage", {}).items():
            if isinstance(value, int):
                total_usage[key] += value
    report = {
        "schema_version": "3.0-development-vlm",
        "data_status": "challenge v1 development pilot; no held-out claim",
        "model": args.model,
        "base_url": args.base_url,
        "api_key_env": args.api_key_env,
        "api_key_recorded": False,
        "sample_count": len(results),
        "vlm_full_metrics": compute_metrics(vlm_rows),
        "cascade_policies": {
            "vlm_on_deterministic_abstain": {
                "metrics": compute_metrics(fallback_rows),
                "vlm_calls": 0,
                "note": "The deterministic rubric decided every pilot row, so a strict UNKNOWN-only fallback makes no VLM call."
            },
            "vlm_confirmation_for_deterministic_pass": {
                "metrics": compute_metrics(confirmation_rows),
                "vlm_calls": sum(row["verdict"] == "PASS" for row in fallback_rows),
                "note": "Counterfactual policy computed from already observed full-VLM outputs; deterministic FAIL cannot be overridden."
            }
        },
        "criterion_metrics": {
            name: {
                "accuracy": sum(truth == pred for truth, pred in values) / len(values),
                "macro_f1": macro_f1(values),
                "confusion": dict(Counter(f"{truth}->{pred}" for truth, pred in values)),
            }
            for name, values in pairs.items()
        },
        "cost": {
            "successful_final_responses": len(results),
            "api_requests_observed": args.request_count_override or len(results),
            "errors": sum(row["error"] is not None for row in results),
            "latency_ms_total": sum(row["vlm"]["latency_ms"] for row in results),
            "latency_ms_mean": sum(row["vlm"]["latency_ms"] for row in results) / len(results) if results else 0,
            "usage": dict(total_usage),
            "usage_is_lower_bound": bool(args.request_count_override and args.request_count_override > len(results)),
            "usage_note": "Provider usage from parse-failed responses was not available in the retained final rows."
        },
        "results": results,
    }
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"metrics": report["vlm_full_metrics"], "criterion_metrics": report["criterion_metrics"], "cost": report["cost"]}, ensure_ascii=False, indent=2), flush=True)
    return 0 if report["cost"]["errors"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
