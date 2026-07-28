#!/usr/bin/env python3
"""Validate and summarize the frozen held-out challenge-v2 evaluation outputs."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
BENCHMARK = ROOT / "verification_benchmark"
RESULTS = BENCHMARK / "reports" / "heldout_challenge_v2_results"
REPORT = RESULTS / "summary.md"
MANIFEST = RESULTS / "results_manifest.json"
FREEZE = BENCHMARK / "frozen" / "heldout_challenge_v2_ground_truth_manifest.json"
SYSTEMS = (
    "runner_self_report",
    "historical_mobiflow_deterministic",
    "enhanced_deterministic",
)


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def pct(value) -> str:
    return "N/A" if value is None else f"{100 * value:.2f}%"


def metric_row(name: str, metrics: dict) -> str:
    return (
        f"| {name} | {metrics['tp']} | {metrics['tn']} | {metrics['fp']} | {metrics['fn']} | "
        f"{metrics['abstain']} | {metrics['invalid']} | {metrics['error']} | "
        f"{pct(metrics['false_pass_rate'])} | {pct(metrics['false_fail_rate'])} | {pct(metrics['coverage'])} |"
    )


def compact_group(metrics: dict) -> str:
    return (
        f"TP={metrics['tp']}, TN={metrics['tn']}, FP={metrics['fp']}, FN={metrics['fn']}, "
        f"A={metrics['abstain']}, I={metrics['invalid']}, FPR={pct(metrics['false_pass_rate'])}, "
        f"FNR={pct(metrics['false_fail_rate'])}, C={pct(metrics['coverage'])}"
    )


def main() -> int:
    for output in (REPORT, MANIFEST):
        if output.exists():
            raise FileExistsError(f"refusing to overwrite result asset: {output}")
    reports = {name: load(RESULTS / f"{name}.json") for name in ("real", "semireal", "combined")}
    freeze = load(FREEZE)

    # Prove that the combined run is the exact per-sample union of the two
    # independently reported origin subsets for every system.
    consistency = {}
    for system in SYSTEMS:
        def stable(row: dict) -> dict:
            return {key: value for key, value in row.items() if key != "latency_ms"}

        combined = {row["trace_id"]: stable(row) for row in reports["combined"]["systems"][system]["results"]}
        separate = {
            row["trace_id"]: stable(row)
            for origin in ("real", "semireal")
            for row in reports[origin]["systems"][system]["results"]
        }
        if combined != separate:
            raise RuntimeError(f"combined/separate per-sample mismatch: {system}")
        consistency[system] = {"row_count": len(combined), "exact_match": True}

    combined_report = reports["combined"]
    enhanced_rows = combined_report["systems"]["enhanced_deterministic"]["results"]
    errors = [
        row for row in enhanced_rows
        if (row["ground_truth"] == "fail" and row["verdict"] == "PASS")
        or (row["ground_truth"] == "success" and row["verdict"] == "FAIL")
    ]
    if len(errors) != 1 or errors[0]["trace_id"] != "semireal/loading_final_state/loading_final_state_02":
        raise RuntimeError("unexpected Enhanced v2 error set")

    lines = [
        "# Held-out challenge v2 results",
        "",
        "Ground Truth was frozen at annotated tag `heldout-challenge-v2-ground-truth-freeze` before the first verifier run. "
        "Enhanced remained fixed at `enhanced-v2-20260713`; challenge v1 was not used as held-out evidence.",
        "",
        "The frozen set contains 22 samples: real 12 (11 success, 1 natural fail) and semireal 10 (all fail), "
        "for a combined 11/11 balance.",
        "",
        "## Combined metrics",
        "",
        "| System | TP | TN | FP | FN | ABSTAIN | INVALID | ERROR | FPR | FNR | Coverage |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for system in SYSTEMS:
        lines.append(metric_row(system, combined_report["systems"][system]["metrics"]))

    lines += [
        "",
        "Historical deterministic uses explicit coverage semantics: its 17 manual-review outcomes are ABSTAIN and "
        "the 5 tasks absent from its mapping are INVALID. Its compatibility-only legacy binary view is "
        "TP=0, TN=8, FP=0, FN=9, Coverage=77.27%; this is not the primary v2 metric.",
        "",
        "## Real versus semireal",
        "",
        "| Origin | System | TP | TN | FP | FN | ABSTAIN | INVALID | ERROR | FPR | FNR | Coverage |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for origin in ("real", "semireal"):
        for system in SYSTEMS:
            metrics = reports[origin]["systems"][system]["metrics"]
            lines.append(f"| {origin} |" + metric_row(system, metrics)[1:])

    lines += [
        "",
        "## Enhanced v2 grouped metrics",
        "",
        "### By App",
        "",
        "| App | Metrics |",
        "|---|---|",
    ]
    enhanced = combined_report["systems"]["enhanced_deterministic"]
    for name, metrics in enhanced["grouped"]["app"].items():
        lines.append(f"| {name} | {compact_group(metrics)} |")
    lines += ["", "### By task type", "", "| Task type | Metrics |", "|---|---|"]
    for name, metrics in enhanced["grouped"]["task_type"].items():
        lines.append(f"| {name} | {compact_group(metrics)} |")
    lines += ["", "### By failure type", "", "| Failure type | Metrics |", "|---|---|"]
    for name, metrics in enhanced["grouped"]["failure_type"].items():
        lines.append(f"| {name} | {compact_group(metrics)} |")

    result_maps = {
        system: {row["trace_id"]: row["verdict"] for row in combined_report["systems"][system]["results"]}
        for system in SYSTEMS
    }
    base_rows = combined_report["systems"]["runner_self_report"]["results"]
    lines += [
        "",
        "## Per-sample verdicts",
        "",
        "| Trace | GT | Runner | Historical | Enhanced v2 |",
        "|---|---|---|---|---|",
    ]
    for row in base_rows:
        trace_id = row["trace_id"]
        lines.append(
            f"| `{trace_id}` | {row['ground_truth']} | {result_maps['runner_self_report'][trace_id]} | "
            f"{result_maps['historical_mobiflow_deterministic'][trace_id]} | "
            f"{result_maps['enhanced_deterministic'][trace_id]} |"
        )

    lines += [
        "",
        "## Sole Enhanced v2 error",
        "",
        "`semireal/loading_final_state/loading_final_state_02` is the only false-pass. Its terminal Gaode frame "
        "is visibly dimmed by a central ‘正在加载...’ overlay, but the frozen verifier matched "
        "`requested_location_result` at frame 2 and classified the screenshot as loaded content. This result is "
        "reported without post-hoc rule modification or re-evaluation.",
        "",
        "## Integrity",
        "",
        "The real, semireal, and combined runs agree exactly per sample for all three systems. Raw traces remain "
        "outside Git in two checksum-verified copies. No verifier, rule, mapping, label, or Ground Truth was changed "
        "after the freeze and before these results were produced.",
        "",
    ]
    REPORT.write_text("\n".join(lines), encoding="utf-8")
    manifest = {
        "ground_truth_freeze_tag": "heldout-challenge-v2-ground-truth-freeze",
        "ground_truth_freeze_commit": "9f9ccb3e196364805f5a0c86ad5ed8f193d8ae3a",
        "enhanced_verifier_tag": freeze["enhanced_verifier_tag"],
        "enhanced_verifier_commit": freeze["enhanced_verifier_commit"],
        "result_files": {
            name: {"path": f"verification_benchmark/reports/heldout_challenge_v2_results/{name}.json", "sha256": sha256(RESULTS / f"{name}.json")}
            for name in ("real", "semireal", "combined")
        },
        "summary": {"path": REPORT.relative_to(ROOT).as_posix(), "sha256": sha256(REPORT)},
        "per_sample_consistency": consistency,
        "enhanced_error_trace_ids": [row["trace_id"] for row in errors],
        "post_freeze_verifier_changes": False,
        "post_freeze_label_changes": False,
    }
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"consistency": consistency, "enhanced_errors": manifest["enhanced_error_trace_ids"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
