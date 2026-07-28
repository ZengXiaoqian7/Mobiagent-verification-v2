#!/usr/bin/env python3
"""Unified paired evaluator for Runner, historical MobiFlow, and Enhanced.

The script is an evaluation/compatibility layer only.  It does not alter any
MobiFlow checkout, rule, trace, label, or historical report.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


ROOT = Path(__file__).resolve().parents[2]
BENCHMARK = ROOT / "verification_benchmark"
BRIDGE = Path(__file__).with_name("run_legacy_verifier.py")
VERDICTS = {"PASS", "FAIL", "ABSTAIN", "INVALID", "ERROR"}


def load_jsonl(path: Path) -> List[Dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def load_mapping(path: Path) -> Dict[str, str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return {str(item["benchmark_task_id"]): str(item["rule_file"]) for item in data["mvp_tasks"]}


def trace_valid(trace: Path) -> tuple[bool, Optional[str]]:
    if not trace.is_dir():
        return False, "trace directory missing"
    for name in ("actions.json", "react.json"):
        if not (trace / name).is_file():
            return False, f"{name} missing"
    frames = [p for p in trace.iterdir() if p.suffix.lower() in {".jpg", ".xml"} and p.stem.isdigit()]
    return (True, None) if frames else (False, "no numeric frame artifacts")


def runner_verdict(trace: Path) -> Dict[str, Any]:
    valid, reason = trace_valid(trace)
    if not valid:
        return {"verdict": "INVALID", "reason": reason, "latency_ms": 0.0}
    try:
        actions = json.loads((trace / "actions.json").read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {"verdict": "INVALID", "reason": f"actions.json: {type(exc).__name__}: {exc}", "latency_ms": 0.0}
    stop = str(actions.get("stop_reason") or "").upper()
    done_statuses = [
        str(action.get("status") or "").upper()
        for action in actions.get("actions", [])
        if str(action.get("type") or "").lower() == "done"
    ]
    positive = "SUCCESS" in stop or any("SUCCESS" in value for value in done_statuses)
    explicit_failure = any(token in stop for token in ("FAIL", "MAX_STEPS", "ERROR", "ABORT")) or any(
        any(token in value for token in ("FAIL", "ERROR")) for value in done_statuses
    )
    if positive:
        verdict = "PASS"
    elif explicit_failure:
        verdict = "FAIL"
    else:
        verdict = "ABSTAIN"
    return {"verdict": verdict, "reason": f"stop_reason={stop or '<empty>'}; done_statuses={done_statuses}", "latency_ms": 0.0}


def verifier_verdict(mobiflow_root: Path, rule: Path, trace: Path) -> Dict[str, Any]:
    valid, reason = trace_valid(trace)
    if not valid:
        return {"verdict": "INVALID", "reason": reason, "latency_ms": 0.0}
    if not rule.is_file():
        return {"verdict": "INVALID", "reason": f"rule missing: {rule}", "latency_ms": 0.0}
    command = [sys.executable, str(BRIDGE), "--mobiflow-root", str(mobiflow_root), "--rule", str(rule), "--trace", str(trace)]
    completed = subprocess.run(command, capture_output=True)
    stdout = completed.stdout.decode("utf-8", errors="replace")
    stderr = completed.stderr.decode("utf-8", errors="replace")
    lines = [line for line in stdout.splitlines() if line.strip()]
    try:
        raw = json.loads(lines[-1])
    except (IndexError, json.JSONDecodeError) as exc:
        return {"verdict": "ERROR", "reason": f"bridge output error: {exc}; stderr={stderr[-500:]}", "latency_ms": 0.0}
    if raw.get("error"):
        verdict = "ERROR"
    elif raw.get("raw_ok") is True:
        verdict = "PASS"
    elif raw.get("manual_review_needed"):
        verdict = "ABSTAIN"
    else:
        verdict = "FAIL"
    raw.update({"verdict": verdict})
    return raw


def metrics(rows: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    counts = Counter()
    for row in rows:
        truth, verdict = row["ground_truth"], row["verdict"]
        counts[verdict.lower()] += 1
        if verdict not in {"PASS", "FAIL"} or truth not in {"success", "fail"}:
            continue
        if truth == "success" and verdict == "PASS": counts["tp"] += 1
        elif truth == "success": counts["fn"] += 1
        elif verdict == "PASS": counts["fp"] += 1
        else: counts["tn"] += 1
    decided = counts["tp"] + counts["tn"] + counts["fp"] + counts["fn"]
    eligible = sum(1 for row in rows if row["ground_truth"] in {"success", "fail"})
    div = lambda a, b: None if not b else a / b
    return {
        "tp": counts["tp"], "tn": counts["tn"], "fp": counts["fp"], "fn": counts["fn"],
        "pass": counts["pass"], "fail": counts["fail"], "abstain": counts["abstain"],
        "invalid": counts["invalid"], "error": counts["error"], "eligible": eligible, "decided": decided,
        "false_pass_rate": div(counts["fp"], counts["fp"] + counts["tn"]),
        "false_fail_rate": div(counts["fn"], counts["fn"] + counts["tp"]),
        "success_recall": div(counts["tp"], counts["tp"] + counts["fn"]),
        "failure_recall": div(counts["tn"], counts["tn"] + counts["fp"]),
        "coverage": div(decided, eligible),
    }


def legacy_binary_metrics(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Compatibility view used by historical reports (raw ok=False => FAIL).

    This is intentionally separate from V2 coverage metrics, where a legacy
    manual-review signal is represented as ABSTAIN.
    """
    converted = []
    for row in rows:
        item = dict(row)
        if row.get("raw_ok") is True:
            item["verdict"] = "PASS"
        elif row.get("raw_ok") is False:
            item["verdict"] = "FAIL"
        converted.append(item)
    return metrics(converted)


def grouped(rows: List[Dict[str, Any]], key: str) -> Dict[str, Any]:
    groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows: groups[str(row.get(key) if row.get(key) is not None else "<none>")].append(row)
    return {name: metrics(items) for name, items in sorted(groups.items())}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--labels", required=True)
    parser.add_argument(
        "--trace-root",
        default=str(BENCHMARK / "traces"),
        help="Trace root containing the repo-relative trace_id paths.",
    )
    parser.add_argument("--historical-root", required=True, help="Worktree root at the confirmed historical commit.")
    parser.add_argument("--historical-config", default=str(BENCHMARK / "configs/mvp_tasks_original_cross_app.json"))
    parser.add_argument("--enhanced-config", default=str(BENCHMARK / "configs/mvp_tasks.json"))
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    labels = load_jsonl(Path(args.labels))
    trace_root = Path(args.trace_root).resolve()
    historical_root = Path(args.historical_root).resolve()
    mappings = {"historical": load_mapping(Path(args.historical_config)), "enhanced": load_mapping(Path(args.enhanced_config))}
    systems: Dict[str, List[Dict[str, Any]]] = {"runner_self_report": [], "historical_mobiflow_deterministic": [], "enhanced_deterministic": []}

    for label in labels:
        trace = trace_root / str(label["trace_id"])
        base = {key: label.get(key) for key in ("trace_id", "benchmark_task_id", "app", "task_type", "ground_truth", "failure_type")}
        result = runner_verdict(trace)
        systems["runner_self_report"].append({**base, **result})
        for system, root, mapping_name in (
            ("historical_mobiflow_deterministic", historical_root, "historical"),
            ("enhanced_deterministic", ROOT, "enhanced"),
        ):
            relative_rule = mappings[mapping_name].get(str(label["benchmark_task_id"]))
            if relative_rule is None:
                result = {"verdict": "INVALID", "reason": "task absent from baseline mapping", "latency_ms": 0.0}
            else:
                rule = root / relative_rule
                result = verifier_verdict(root / "MobiFlow", rule, trace)
            systems[system].append({**base, **result})

    report = {
        "schema_version": "2.0-minimal",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "inputs": {
            "labels": str(Path(args.labels).resolve()),
            "trace_root": str(trace_root),
            "historical_root": str(historical_root),
        },
        "verdicts": sorted(VERDICTS),
        "systems": {
            name: {
                "metrics": metrics(rows),
                "legacy_binary_metrics": legacy_binary_metrics(rows),
                "grouped": {key: grouped(rows, key) for key in ("app", "task_type", "failure_type")},
                "results": rows,
            }
            for name, rows in systems.items()
        },
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({name: data["metrics"] for name, data in report["systems"].items()}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
