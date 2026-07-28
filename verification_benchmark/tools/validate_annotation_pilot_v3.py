#!/usr/bin/env python3
"""Validate and materialize the adjudicated V3 criterion/evidence pilot."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List


ROOT = Path(__file__).resolve().parents[2]
BENCHMARK = ROOT / "verification_benchmark"
DEFAULT_MANIFEST = BENCHMARK / "benchmark_v3/manifests/annotation_pilot_v1.json"
VALID_STATUSES = {"SATISFIED", "VIOLATED", "UNKNOWN", "NOT_APPLICABLE", "ERROR"}


def jsonl(path: Path) -> List[Dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def dump_jsonl(path: Path, rows: Iterable[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    parser.add_argument("--materialize", action="store_true")
    args = parser.parse_args()

    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    source_rows = jsonl(ROOT / manifest["source_labels"])
    sources = {row["trace_id"]: row for row in source_rows}
    annotations = jsonl(ROOT / manifest["canonical_annotations"])
    rubrics = jsonl(BENCHMARK / "benchmark_v3/manifests/rubrics.jsonl")
    rubric_map = {row["rubric_id"]: row for row in rubrics}
    trace_root = Path(manifest["trace_root"])
    errors: List[str] = []
    seen = set()
    trajectory_rows: List[Dict[str, Any]] = []
    criterion_rows: List[Dict[str, Any]] = []
    evidence_rows: List[Dict[str, Any]] = []

    for annotation in annotations:
        trace_id = annotation["trace_id"]
        if trace_id in seen:
            errors.append(f"duplicate trace_id: {trace_id}")
        seen.add(trace_id)
        source = sources.get(trace_id)
        if source is None:
            errors.append(f"trace missing from source labels: {trace_id}")
            continue
        expected_outcome = "success" if source["ground_truth"] == "success" else "fail"
        if annotation["outcome_label"] != expected_outcome:
            errors.append(f"outcome mismatch: {trace_id}")
        frame = int(annotation["terminal_frame"])
        if frame not in source.get("evidence_frames", []):
            errors.append(f"terminal frame is not frozen evidence: {trace_id}@{frame}")
        trace = trace_root / trace_id
        for name in ("actions.json", "react.json", f"{frame}.jpg", f"{frame}.xml"):
            if not (trace / name).is_file():
                errors.append(f"missing artifact: {trace_id}/{name}")
        rubric = rubric_map.get(annotation["rubric_id"])
        if rubric is None:
            errors.append(f"unknown rubric: {annotation['rubric_id']}")
            continue
        expected_criteria = {item["criterion_id"] for item in rubric["criteria"]}
        actual_criteria = set(annotation["criteria"])
        if actual_criteria != expected_criteria:
            errors.append(f"criterion set mismatch: {trace_id}")

        trajectory_rows.append({
            "trace_id": trace_id,
            "rubric_id": annotation["rubric_id"],
            "app": source["app"],
            "task_type": source["task_type"],
            "outcome_label": annotation["outcome_label"],
            "process_label": annotation["process_label"],
            "primary_failure_code": annotation["primary_failure_code"],
            "terminal_evidence_frames": [frame],
            "source_ground_truth": source["ground_truth"],
            "source_failure_type": source.get("failure_type"),
            "adjudication_status": "adjudicated",
        })
        for criterion_id, label in annotation["criteria"].items():
            status = label.get("status")
            if status not in VALID_STATUSES:
                errors.append(f"invalid status: {trace_id}/{criterion_id}={status}")
            evidence_id = f"{trace_id}#frame-{frame}#{criterion_id}"
            criterion_rows.append({
                "trace_id": trace_id,
                "rubric_id": annotation["rubric_id"],
                "criterion_id": criterion_id,
                "status": status,
                "evidence_ids": [evidence_id],
                "reason": label.get("reason"),
            })
            role = "support" if status == "SATISFIED" else "contradict" if status == "VIOLATED" else "context"
            evidence_rows.append({
                "evidence_id": evidence_id,
                "trace_id": trace_id,
                "criterion_id": criterion_id,
                "frame_index": frame,
                "role": role,
                "modality": label.get("modality"),
                "artifact_uri": f"{trace_id}/{frame}.jpg",
                "sufficiency": "sufficient" if status in {"SATISFIED", "VIOLATED"} else "context_only",
            })

    outcome_counts = Counter(row["outcome_label"] for row in trajectory_rows)
    app_counts = Counter(row["app"] for row in trajectory_rows)
    if len(annotations) != manifest["sample_count"]:
        errors.append(f"sample count mismatch: {len(annotations)}")
    if outcome_counts != Counter({"success": manifest["success_count"], "fail": manifest["fail_count"]}):
        errors.append(f"outcome balance mismatch: {dict(outcome_counts)}")
    if sorted(app_counts) != sorted(manifest["apps"]):
        errors.append(f"app set mismatch: {sorted(app_counts)}")

    if args.materialize and not errors:
        output = BENCHMARK / "benchmark_v3/annotations/materialized"
        dump_jsonl(output / "trajectory_labels.jsonl", trajectory_rows)
        dump_jsonl(output / "criterion_labels.jsonl", criterion_rows)
        dump_jsonl(output / "evidence_labels.jsonl", evidence_rows)

    summary = {
        "ok": not errors,
        "samples": len(trajectory_rows),
        "criteria": len(criterion_rows),
        "evidence": len(evidence_rows),
        "outcomes": dict(outcome_counts),
        "apps": dict(app_counts),
        "errors": errors,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
