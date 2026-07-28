#!/usr/bin/env python3
"""Read-only inventory of verification benchmark assets.

The inventory deliberately treats every labels*.jsonl file as a source and
deduplicates records by trace_id.  It never rewrites traces, labels, or reports.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple


ROOT = Path(__file__).resolve().parents[2]
BENCHMARK = ROOT / "verification_benchmark"


def _read_jsonl(path: Path) -> Iterable[Tuple[int, Dict[str, Any]]]:
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.strip():
            yield line_number, json.loads(line)


def _provenance(label: Dict[str, Any], source: Path) -> str:
    construction = str(label.get("construction") or "").lower()
    notes = str(label.get("notes") or "").lower()
    trace_id = str(label.get("trace_id") or "").lower()
    if "fixture" in source.name.lower() or "/fixture" in trace_id:
        return "fixture"
    if "semi-real" in construction or "半真实" in construction:
        return "semireal"
    semireal_markers = ("截断", "替换", "扰动", "拼接", "构造", "slice", "truncat")
    if any(marker in notes or marker in construction for marker in semireal_markers):
        return "semireal_inferred"
    return "real_or_unspecified"


def _trace_summary(trace_dir: Path) -> Dict[str, Any]:
    if not trace_dir.is_dir():
        return {"exists": False, "frames": 0, "jpg": 0, "xml": 0}
    jpg = {p.stem for p in trace_dir.glob("*.jpg") if p.stem.isdigit()}
    xml = {p.stem for p in trace_dir.glob("*.xml") if p.stem.isdigit()}
    return {
        "exists": True,
        "frames": len(jpg | xml),
        "jpg": len(jpg),
        "xml": len(xml),
        "actions": (trace_dir / "actions.json").is_file(),
        "react": (trace_dir / "react.json").is_file(),
    }


def build_inventory() -> Dict[str, Any]:
    label_sources: List[Dict[str, Any]] = []
    records: Dict[str, Dict[str, Any]] = {}
    duplicates: Dict[str, List[str]] = defaultdict(list)
    conflicts: List[Dict[str, Any]] = []

    for source in sorted(BENCHMARK.glob("labels*.jsonl")):
        rows = list(_read_jsonl(source))
        source_counts = Counter(str(row.get("ground_truth")) for _, row in rows)
        label_sources.append({
            "path": source.relative_to(ROOT).as_posix(),
            "records": len(rows),
            "ground_truth": dict(sorted(source_counts.items())),
        })
        for line_number, label in rows:
            trace_id = str(label.get("trace_id") or "")
            origin = f"{source.relative_to(ROOT).as_posix()}:{line_number}"
            if trace_id in records:
                duplicates[trace_id].append(origin)
                previous = records[trace_id]["label"]
                checked = ("ground_truth", "benchmark_task_id", "app", "task_type")
                changed = {key: [previous.get(key), label.get(key)] for key in checked if previous.get(key) != label.get(key)}
                if changed:
                    conflicts.append({"trace_id": trace_id, "sources": [records[trace_id]["source"], origin], "fields": changed})
                continue
            records[trace_id] = {"label": label, "source": origin, "provenance": _provenance(label, source)}

    counters = {name: Counter() for name in ("app", "task_type", "benchmark_task_id", "ground_truth", "failure_type", "provenance")}
    trace_health = Counter()
    per_app_truth: Dict[str, Counter] = defaultdict(Counter)
    per_task_truth: Dict[str, Counter] = defaultdict(Counter)
    total_frames = 0
    for trace_id, record in records.items():
        label = record["label"]
        for name in counters:
            value = record["provenance"] if name == "provenance" else label.get(name)
            counters[name][str(value if value is not None else "<none>")] += 1
        truth = str(label.get("ground_truth"))
        per_app_truth[str(label.get("app"))][truth] += 1
        per_task_truth[str(label.get("benchmark_task_id"))][truth] += 1
        trace = _trace_summary(BENCHMARK / "traces" / trace_id)
        trace_health["present" if trace["exists"] else "missing"] += 1
        if trace["exists"]:
            total_frames += int(trace["frames"])
            if not trace.get("actions"):
                trace_health["missing_actions"] += 1
            if not trace.get("react"):
                trace_health["missing_react"] += 1

    reports = sorted(BENCHMARK.glob("reports/**/benchmark_eval_*.json"))
    report_modes = Counter()
    readable_reports = 0
    for report in reports:
        try:
            data = json.loads(report.read_text(encoding="utf-8"))
            report_modes[str(data.get("mode") or "<unknown>")] += 1
            readable_reports += 1
        except (OSError, json.JSONDecodeError):
            report_modes["<unreadable>"] += 1

    return {
        "inventory_schema": "1.0",
        "root": str(ROOT),
        "label_sources": label_sources,
        "unique_labeled_traces": len(records),
        "duplicate_trace_ids": {key: value for key, value in sorted(duplicates.items())},
        "label_conflicts": conflicts,
        "counts": {name: dict(sorted(values.items())) for name, values in counters.items()},
        "per_app_ground_truth": {key: dict(sorted(value.items())) for key, value in sorted(per_app_truth.items())},
        "per_task_ground_truth": {key: dict(sorted(value.items())) for key, value in sorted(per_task_truth.items())},
        "trace_health": dict(sorted(trace_health.items())),
        "numeric_frames_across_labeled_traces": total_frames,
        "evaluation_reports": {"files": len(reports), "readable": readable_reports, "modes": dict(sorted(report_modes.items()))},
        "rule_files": {
            "historical_corpus": len(list((ROOT / "MobiFlow" / "task_rules").glob("**/*.yaml"))),
            "benchmark_local": len(list((BENCHMARK / "rules").glob("**/*.yaml"))),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", help="Optional JSON output; stdout is always emitted.")
    args = parser.parse_args()
    inventory = build_inventory()
    rendered = json.dumps(inventory, ensure_ascii=False, indent=2)
    print(rendered)
    if args.output:
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
