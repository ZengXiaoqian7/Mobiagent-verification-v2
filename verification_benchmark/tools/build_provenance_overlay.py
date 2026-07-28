#!/usr/bin/env python3
"""Build a conservative provenance overlay without changing legacy labels."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict


ROOT = Path(__file__).resolve().parents[2]
BENCHMARK = ROOT / "verification_benchmark"


def classify(trace_id: str, source_name: str, label: Dict[str, Any]) -> Dict[str, Any]:
    notes = str(label.get("notes") or "")
    construction = str(label.get("construction") or "")
    evidence = []

    if source_name == "labels_fixture.jsonl":
        origin = "fixture"
        evidence.append("fixture label source")
    elif source_name == "labels_taobao_open_detail_hard_negatives.jsonl" and "semi-real" in construction.lower():
        origin = "semireal"
        evidence.append(f"explicit construction={construction}")
    elif "真实 Runner trace 截断" in notes or "reviewed core slice" in notes:
        origin = "semireal"
        evidence.append("legacy notes explicitly identify truncation/core slicing")
    elif source_name == "labels.jsonl" and trace_id == "taobao/search/runner_taobao_type1_clean_success_001":
        origin = "unknown"
        evidence.append("notes say normalized from a real trace but do not document whether artifacts were transformed")
    elif source_name == "labels.jsonl" and "runner_taobao_batch_false_done" in trace_id:
        origin = "unknown"
        evidence.append("Runner-derived label, but no per-sample source/transform record is preserved")
    elif source_name.startswith("labels_"):
        origin = "real"
        evidence.append(f"dedicated Runner collection label source: {source_name}")
    else:
        origin = "unknown"
        evidence.append("insufficient preserved provenance")

    if origin == "fixture":
        current_role = "fixture"
    else:
        current_role = "development"
    original_protocol_role = "development"
    if source_name == "labels_cross_app_round2_scale.jsonl" and not trace_id.startswith("cloudmusic/"):
        original_protocol_role = "held_out_at_collection"
    elif source_name == "labels_cross_app_round2_scale.jsonl" and trace_id.startswith("cloudmusic/"):
        original_protocol_role = "development_posthoc"
    elif source_name == "labels_cross_app_pilot_round1.jsonl":
        original_protocol_role = "development"
    elif origin == "fixture":
        original_protocol_role = "fixture"

    return {
        "trace_id": trace_id,
        "origin": origin,
        "current_enhanced_role": current_role,
        "original_protocol_role": original_protocol_role,
        "source_label_file": f"verification_benchmark/{source_name}",
        "parent_trace_id": None,
        "transformation": "truncated_or_recomposed" if origin == "semireal" else None,
        "evidence": evidence,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=str(BENCHMARK / "provenance_overlay.jsonl"))
    args = parser.parse_args()
    records: Dict[str, Dict[str, Any]] = {}
    # More specific label files override labels.jsonl for duplicate trace IDs.
    sources = sorted(BENCHMARK.glob("labels*.jsonl"), key=lambda p: (p.name != "labels.jsonl", p.name))
    for source in sources:
        for line in source.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            label = json.loads(line)
            trace_id = str(label["trace_id"])
            records[trace_id] = classify(trace_id, source.name, label)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for _, row in sorted(records.items())), encoding="utf-8")
    counts: Dict[str, int] = {}
    for row in records.values(): counts[row["origin"]] = counts.get(row["origin"], 0) + 1
    print(json.dumps({"records": len(records), "origin": dict(sorted(counts.items()))}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
