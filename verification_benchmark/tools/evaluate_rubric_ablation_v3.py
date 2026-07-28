#!/usr/bin/env python3
"""Development-only deterministic rubric/DAG ablation on the v1 pilot."""

from __future__ import annotations

import argparse
import copy
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional


ROOT = Path(__file__).resolve().parents[2]
BENCHMARK = ROOT / "verification_benchmark"
MOBIFLOW = ROOT / "MobiFlow"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(MOBIFLOW))

from avdag.conditions import get_checker  # noqa: E402
from avdag.loader import load_task  # noqa: E402
from avdag.trace_loader import load_frames_from_dir  # noqa: E402
from avdag.types import VerifierOptions  # noqa: E402
from verification_framework.evaluation import compute_metrics  # noqa: E402


CRITERION_PRIORITY = [
    "negative.no_blocking_overlay",
    "negative.stable_terminal",
    "outcome.target_entity",
    "outcome.terminal_structure",
    "process.no_premature_done",
]


def jsonl(path: Path) -> List[Dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def mapping(path: Path) -> Dict[str, str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {row["benchmark_task_id"]: row["rule_file"] for row in payload["mvp_tasks"]}


def status(passed: bool) -> str:
    return "SATISFIED" if passed else "VIOLATED"


def strong_blocking_overlay(xml: str) -> bool:
    tokens = ("华为账号一键登录", "其他登录方式", "验证码登录", "登录后继续", "权限申请")
    return any(token in xml for token in tokens)


def success_nodes(task: Any) -> List[Any]:
    node_by_id = {node.id: node for node in task.nodes}
    if task.success and task.success.any_of:
        ids = task.success.any_of
    elif task.success and task.success.all_of:
        ids = task.success.all_of
    else:
        ids = [task.nodes[-1].id]
    return [node_by_id[node_id] for node_id in ids]


def terminal_predictions(rule: Path, trace: Path) -> Dict[str, Any]:
    frames = load_frames_from_dir(str(trace))
    frame = frames[-1]
    task = load_task(str(rule))
    nodes = success_nodes(task)
    options = VerifierOptions(ocr=None, llm=None)
    composite_hits = []
    target_hits = []
    structure_hits = []
    stable_hits = []
    for node in nodes:
        if node.condition is None:
            continue
        composite_hits.append(bool(get_checker(node.condition.type).check(frame, node.condition.params, options)))
        params = node.condition.params or {}
        if node.condition.type != "juxtaposition":
            target_hits.append(composite_hits[-1])
            structure_hits.append(composite_hits[-1])
            stable_hits.append(True)
            continue
        dynamic = copy.deepcopy(params.get("dynamic_match") or {})
        dynamic.pop("require_any", None)
        dynamic.pop("require_all", None)
        target_hits.append(bool(dynamic) and bool(get_checker("dynamic_match").check(frame, dynamic, options)))
        xml_params = params.get("xml") or {}
        if xml_params:
            structure_hits.append(bool(get_checker("xml_text_match").check(frame, xml_params, options)))
        else:
            structure_hits.append(target_hits[-1])
        visual = params.get("visual_state") or {"not_loading_skeleton": True, "require_image": True}
        stable_hits.append(bool(get_checker("visual_state").check(frame, visual, options)))

    terminal_composite = any(composite_hits)
    target = any(target_hits)
    structure = any(structure_hits)
    stable = any(stable_hits)
    no_overlay = not strong_blocking_overlay(str(frame.get("xml_text") or ""))
    actions = json.loads((trace / "actions.json").read_text(encoding="utf-8")).get("actions", [])
    final_done = bool(actions and str(actions[-1].get("type") or "").lower() == "done")
    outcome_pass = target and structure and stable and no_overlay
    process_status = "NOT_APPLICABLE" if not final_done else status(outcome_pass)
    criteria = {
        "process.no_premature_done": process_status,
        "outcome.target_entity": status(target),
        "outcome.terminal_structure": status(structure),
        "negative.stable_terminal": status(stable),
        "negative.no_blocking_overlay": status(no_overlay),
    }
    return {
        "terminal_frame": int(frame["_index"]),
        "terminal_composite": "PASS" if terminal_composite else "FAIL",
        "decomposed_rubric": "PASS" if outcome_pass else "FAIL",
        "criteria": criteria,
    }


def first_violation(criteria: Mapping[str, str]) -> Optional[str]:
    return next((criterion for criterion in CRITERION_PRIORITY if criteria.get(criterion) == "VIOLATED"), None)


def categorical_macro_f1(pairs: List[tuple[str, str]]) -> float:
    labels = sorted({value for pair in pairs for value in pair})
    scores = []
    for label in labels:
        tp = sum(truth == label and pred == label for truth, pred in pairs)
        fp = sum(truth != label and pred == label for truth, pred in pairs)
        fn = sum(truth == label and pred != label for truth, pred in pairs)
        precision = 0.0 if tp + fp == 0 else tp / (tp + fp)
        recall = 0.0 if tp + fn == 0 else tp / (tp + fn)
        scores.append(0.0 if precision + recall == 0 else 2 * precision * recall / (precision + recall))
    return sum(scores) / len(scores) if scores else 0.0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trace-root", required=True)
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    source_labels = {row["trace_id"]: row for row in jsonl(BENCHMARK / "labels_cross_app_heldout_challenge_v1_challenge.jsonl")}
    annotations = jsonl(BENCHMARK / "benchmark_v3/annotations/pilot_annotations.jsonl")
    rule_map = mapping(BENCHMARK / "configs/mvp_tasks.json")
    baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
    dag_rows = {row["trace_id"]: row for row in baseline["systems"]["enhanced_deterministic"]["results"]}
    systems: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    criterion_pairs: Dict[str, List[tuple[str, str]]] = defaultdict(list)
    evidence_hits = 0
    diagnosis_hits = 0
    fail_count = 0
    detail = []

    for annotation in annotations:
        trace_id = annotation["trace_id"]
        label = source_labels[trace_id]
        rule = ROOT / rule_map[label["benchmark_task_id"]]
        prediction = terminal_predictions(rule, Path(args.trace_root) / trace_id)
        dag_verdict = dag_rows[trace_id]["verdict"]
        rubric_verdict = prediction["decomposed_rubric"]
        combined = "PASS" if dag_verdict == "PASS" and rubric_verdict == "PASS" else "FAIL"
        base = {
            "trace_id": trace_id,
            "ground_truth": label["ground_truth"],
            "app": label["app"],
            "task_type": label["task_type"],
            "failure_type": label.get("failure_type"),
        }
        for system, verdict in (
            ("dag_only", dag_verdict),
            ("terminal_composite_without_dag", prediction["terminal_composite"]),
            ("decomposed_rubric_without_dag", rubric_verdict),
            ("rubric_and_dag", combined),
        ):
            systems[system].append({**base, "verdict": verdict})
        for criterion_id, truth_payload in annotation["criteria"].items():
            criterion_pairs[criterion_id].append((truth_payload["status"], prediction["criteria"][criterion_id]))
        evidence_hits += int(prediction["terminal_frame"] == annotation["terminal_frame"])
        truth_failure = first_violation({key: value["status"] for key, value in annotation["criteria"].items()})
        predicted_failure = first_violation(prediction["criteria"])
        if label["ground_truth"] == "fail":
            fail_count += 1
            diagnosis_hits += int(truth_failure == predicted_failure)
        detail.append({
            **base,
            "dag_verdict": dag_verdict,
            **prediction,
            "truth_criteria": {key: value["status"] for key, value in annotation["criteria"].items()},
            "truth_primary_criterion": truth_failure,
            "predicted_primary_criterion": predicted_failure,
        })

    criterion_metrics = {}
    for criterion_id, pairs in criterion_pairs.items():
        criterion_metrics[criterion_id] = {
            "count": len(pairs),
            "accuracy": sum(truth == pred for truth, pred in pairs) / len(pairs),
            "macro_f1": categorical_macro_f1(pairs),
            "confusion": dict(Counter(f"{truth}->{pred}" for truth, pred in pairs)),
        }
    report = {
        "schema_version": "3.0-development-ablation",
        "data_status": "challenge v1 development/regression; no held-out claim",
        "sample_count": len(annotations),
        "systems": {name: {"metrics": compute_metrics(rows), "results": rows} for name, rows in systems.items()},
        "criterion_metrics": criterion_metrics,
        "evidence_frame_recall_at_1": evidence_hits / len(annotations),
        "primary_failure_criterion_accuracy": diagnosis_hits / fail_count,
        "details": detail,
        "interpretation_guardrail": "All selected cross-app Enhanced v2 rules have a single terminal success node; this pilot cannot identify an independent benefit from multi-step DAG constraints.",
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "systems": {name: payload["metrics"] for name, payload in report["systems"].items()},
        "criterion_accuracy": {name: row["accuracy"] for name, row in criterion_metrics.items()},
        "evidence_frame_recall_at_1": report["evidence_frame_recall_at_1"],
        "primary_failure_criterion_accuracy": report["primary_failure_criterion_accuracy"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
