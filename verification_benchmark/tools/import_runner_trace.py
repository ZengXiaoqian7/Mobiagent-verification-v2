#!/usr/bin/env python3
"""Import a Runner output trace into the verification benchmark tree."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List


REPO_ROOT = Path(__file__).resolve().parents[2]
BENCHMARK_ROOT = REPO_ROOT / "verification_benchmark"
DEFAULT_LABELS = BENCHMARK_ROOT / "labels.jsonl"
TOOLS_DIR = Path(__file__).resolve().parent

if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from check_trace_schema import inspect_trace  # noqa: E402


def rel(path: Path) -> str:
    try:
        return path.resolve().relative_to(REPO_ROOT.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def validate_source_trace(path: Path) -> List[str]:
    return list(inspect_trace(path).get("errors") or [])


def append_label(path: Path, row: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def safe_remove_destination(dest: Path) -> None:
    root = BENCHMARK_ROOT.resolve()
    target = dest.resolve()
    if not target.is_relative_to(root):
        raise RuntimeError(f"refusing to overwrite outside benchmark root: {target}")
    shutil.rmtree(target)


def main() -> int:
    parser = argparse.ArgumentParser(description="Import Runner trace output into verification benchmark traces.")
    parser.add_argument("--source", required=True, help="Runner trace directory containing actions.json/react.json.")
    parser.add_argument("--benchmark-task-id", required=True, help="Task ID from configs/mvp_tasks.json.")
    parser.add_argument("--trace-id", required=True, help="Relative trace ID under verification_benchmark/traces.")
    parser.add_argument("--app", required=True, help="Normalized app ID, e.g. taobao or bilibili.")
    parser.add_argument("--task-type", required=True, help="Benchmark task type, e.g. search/filter/play.")
    parser.add_argument("--task-description", required=True)
    parser.add_argument(
        "--ground-truth",
        choices=["success", "fail", "ambiguous"],
        default="ambiguous",
        help="Initial human label. Use ambiguous until reviewed.",
    )
    parser.add_argument("--failure-type", default=None)
    parser.add_argument("--evidence-frames", default="", help="Comma-separated frame indices, e.g. 3,4.")
    parser.add_argument("--notes", default="")
    parser.add_argument("--reviewer", default="")
    parser.add_argument("--labels", default=str(DEFAULT_LABELS))
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    source = Path(args.source)
    schema = inspect_trace(source)
    errors = list(schema.get("errors") or [])
    if errors:
        for error in errors:
            print(f"error: {error}")
        return 1
    for warning in schema.get("warnings") or []:
        print(f"warning: {warning}")

    dest = BENCHMARK_ROOT / "traces" / args.trace_id
    if dest.exists():
        if not args.overwrite:
            print(f"error: destination already exists: {rel(dest)}")
            return 1
        safe_remove_destination(dest)

    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, dest)

    evidence_frames = [
        int(item.strip())
        for item in args.evidence_frames.split(",")
        if item.strip()
    ]
    label = {
        "trace_id": args.trace_id,
        "benchmark_task_id": args.benchmark_task_id,
        "app": args.app,
        "task_type": args.task_type,
        "task_description": args.task_description,
        "ground_truth": args.ground_truth,
        "failure_type": args.failure_type,
        "expected_slots": {},
        "evidence_frames": evidence_frames,
        "notes": args.notes,
        "trace_schema": {
            "source_warnings": schema.get("warnings") or [],
            "action_count": schema.get("action_count"),
            "react_count": schema.get("react_count"),
            "extra_artifacts": schema.get("extra_artifacts") or [],
            "missing_jpg": schema.get("missing_jpg") or [],
            "missing_xml": schema.get("missing_xml") or [],
        },
        "reviewer": args.reviewer,
        "reviewed_at": datetime.now().isoformat(timespec="seconds") if args.ground_truth != "ambiguous" else "",
    }
    append_label(Path(args.labels), label)

    print("Imported trace.")
    print(f"source: {source.resolve()}")
    print(f"dest: {dest.resolve()}")
    print(f"label: {Path(args.labels).resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
