#!/usr/bin/env python3
"""Measure process/outcome decoupling on Taobao composed-control development traces."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Dict, List


ROOT = Path(__file__).resolve().parents[2]
MOBIFLOW = ROOT / "MobiFlow"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(MOBIFLOW))

from avdag.conditions import get_checker  # noqa: E402
from avdag.loader import load_task  # noqa: E402
from avdag.trace_loader import load_frames_from_dir  # noqa: E402
from avdag.types import VerifierOptions  # noqa: E402
from avdag.verifier import verify  # noqa: E402
from verification_framework.evaluation import compute_metrics  # noqa: E402


def jsonl(path: Path) -> List[Dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def reindex(frames: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    for index, frame in enumerate(frames):
        frame["_index"] = index
        frame["_prev"] = frames[index - 1] if index else None
        frame["_next"] = frames[index + 1] if index + 1 < len(frames) else None
    return frames


def reverse_intermediate_frames(frames: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Keep the initial placeholder and successful terminal frame, reverse only intermediate evidence."""
    return reindex([frames[0], *list(reversed(frames[1:-1])), frames[-1]])


def terminal_success(frame: Dict[str, Any], task: Any) -> bool:
    by_id = {node.id: node for node in task.nodes}
    ids = task.success.any_of if task.success and task.success.any_of else task.success.all_of if task.success else [task.nodes[-1].id]
    hits = []
    for node_id in ids or []:
        node = by_id[node_id]
        hits.append(bool(node.condition and get_checker(node.condition.type).check(frame, node.condition.params, VerifierOptions(ocr=None, llm=None))))
    return all(hits) if task.success and task.success.all_of else any(hits)


def row(trace_id: str, truth: str, verdict: str, **extra: Any) -> Dict[str, Any]:
    return {"trace_id": trace_id, "ground_truth": truth, "verdict": verdict, "app": "taobao", "task_type": "composed_controls", **extra}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--labels", required=True)
    parser.add_argument("--trace-root", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    label_path = Path(args.labels)
    trace_root = Path(args.trace_root)
    labels = jsonl(label_path)
    task = load_task(str(ROOT / "verification_benchmark/rules/taobao/taobao-dynamic-selected-control-results.yaml"))
    original_dag = []
    original_terminal = []
    corrupted_outcome_terminal = []
    corrupted_outcome_dag = []
    corrupted_process_dag = []
    details = []

    for label in labels:
        trace_id = label["trace_id"]
        frames = load_frames_from_dir(str(trace_root / trace_id))
        dag = verify(frames, task, VerifierOptions(ocr=None, llm=None))
        terminal = terminal_success(frames[-1], task)
        original_dag.append(row(trace_id, label["ground_truth"], "PASS" if dag.ok else "FAIL"))
        original_terminal.append(row(trace_id, label["ground_truth"], "PASS" if terminal else "FAIL"))
        item = {
            "trace_id": trace_id,
            "ground_truth": label["ground_truth"],
            "original": {
                "dag_verdict": "PASS" if dag.ok else "FAIL",
                "terminal_outcome_verdict": "PASS" if terminal else "FAIL",
                "matched": [{"node_id": match.node_id, "frame_index": match.frame_index} for match in dag.matched],
            },
        }
        if label["ground_truth"] == "success":
            transformed = reverse_intermediate_frames(load_frames_from_dir(str(trace_root / trace_id)))
            transformed_dag = verify(transformed, task, VerifierOptions(ocr=None, llm=None))
            transformed_terminal = terminal_success(transformed[-1], task)
            transformed_id = trace_id + "#reverse-intermediate-v1"
            corrupted_outcome_terminal.append(row(transformed_id, "success", "PASS" if transformed_terminal else "FAIL", origin="semireal_process_ablation"))
            corrupted_outcome_dag.append(row(transformed_id, "success", "PASS" if transformed_dag.ok else "FAIL", origin="semireal_process_ablation"))
            corrupted_process_dag.append(row(transformed_id, "fail", "PASS" if transformed_dag.ok else "FAIL", origin="semireal_process_ablation"))
            item["reverse_intermediate"] = {
                "transformation": "keep placeholder frame 0 and original terminal frame; reverse frames 1..N-1; recompute adjacency only",
                "outcome_ground_truth": "success",
                "process_ground_truth": "incorrect",
                "dag_process_verdict": "PASS" if transformed_dag.ok else "FAIL",
                "terminal_outcome_verdict": "PASS" if transformed_terminal else "FAIL",
                "matched": [{"node_id": match.node_id, "frame_index": match.frame_index} for match in transformed_dag.matched],
            }
        details.append(item)

    report = {
        "schema_version": "3.0-process-outcome-development",
        "data_status": "historical Taobao development assets plus in-memory semireal process transformations; no held-out claim",
        "source": {
            "labels": str(label_path.resolve()),
            "labels_sha256": sha256(label_path),
            "trace_root": str(trace_root.resolve()),
            "raw_assets_modified": False,
        },
        "original_six": {
            "dag_coupled_verdict": compute_metrics(original_dag),
            "terminal_outcome_only": compute_metrics(original_terminal),
        },
        "reverse_intermediate_success_five": {
            "terminal_outcome_only": compute_metrics(corrupted_outcome_terminal),
            "dag_incorrectly_used_as_outcome": compute_metrics(corrupted_outcome_dag),
            "dag_used_as_process_checker": compute_metrics(corrupted_process_dag),
            "interpretation": "The transformation preserves the reviewed successful terminal frame but makes recorded process order incorrect. Outcome and process labels are intentionally different.",
        },
        "details": details,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"original_six": report["original_six"], "reverse_intermediate_success_five": report["reverse_intermediate_success_five"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
