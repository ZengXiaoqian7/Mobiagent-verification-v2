"""Development-only Phase 5 verifier smoke over frozen single-operator GT traces."""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from .phase5_intake import (
    CLAIM_BOUNDARY,
    Phase5IntakeError,
    file_sha256,
    semantic_sha256,
    source_file_manifest,
)
from .phase5_ground_truth import (
    ALLOWED_GT_STATUSES,
    GT_VERDICTS,
    ground_truth_verdict as _gt_verdict,
    validate_frozen_ground_truth as _validate_gt,
)
from .phase5_trace_case import (
    CasePaths,
    find_run_manifest as _find_run_manifest,
    first_source_sort_frame as _first_source_sort_frame,
    input_texts as _input_texts,
    load_actions as _load_actions,
    load_json as _load_json,
    open_app_targets as _open_app_targets,
    trace_dir as _trace_dir,
)


SMOKE_VERSION = "harmony-eval-phase5-development-verifier-smoke-v3"
REPORT_SCHEMA_VERSION = "harmony-eval-phase5-development-verifier-smoke-report-v1"
CRITERION_PASS = "SATISFIED"
CRITERION_FAIL = "VIOLATED"
CRITERION_UNKNOWN = "UNKNOWN_EVIDENCE"


def _defer_visual_semantics(frame_index: int | None) -> Mapping[str, Any]:
    """Leave UI meaning to a Contract-routed semantic checker.

    This deterministic smoke intentionally contains no application name,
    coordinate, color, theme or device-layout rule.  Action evidence can locate
    a candidate frame but cannot establish the requested visible UI state.
    """

    if frame_index is None:
        return {
            "status": CRITERION_UNKNOWN,
            "reason": "no candidate frame is available for semantic verification",
            "evidence": None,
        }
    return {
        "status": CRITERION_UNKNOWN,
        "reason": "visible UI state requires Contract-routed semantic evidence",
        "evidence": {
            "frame": frame_index,
            "required_checker": "contract_semantic",
        },
    }


def _criterion(
    status: str, reason: str, evidence: Mapping[str, Any] | None = None
) -> Mapping[str, Any]:
    return {"status": status, "reason": reason, "evidence": evidence}


def _verdict(criteria: Mapping[str, Mapping[str, Any]]) -> str:
    statuses = [item["status"] for item in criteria.values()]
    if CRITERION_FAIL in statuses:
        return "FAIL"
    if any(status != CRITERION_PASS for status in statuses):
        return "ABSTAIN"
    return "PASS"


def verify_case_without_ground_truth(paths: CasePaths) -> Mapping[str, Any]:
    """Run deterministic Phase 5 checks without reading an answer key."""

    run_dir = paths.run_dir.resolve(strict=True)
    intake = _load_json(
        paths.intake_receipt.resolve(strict=True), "Phase 5 intake receipt"
    )
    run_manifest = _find_run_manifest(run_dir)
    run_id = str(run_manifest["run_id"])
    task_id = str(run_manifest["task_id"])
    trace = _trace_dir(run_dir, run_manifest)
    action_payload, actions = _load_actions(trace)
    source_files = list(source_file_manifest(run_dir))
    if intake.get("source_tree_sha256") != semantic_sha256(source_files):
        raise Phase5IntakeError("intake receipt source tree hash drift")

    input_texts = _input_texts(actions)
    source_query = input_texts[0] if input_texts else ""
    target_query = input_texts[-1] if len(input_texts) >= 2 else ""
    open_targets = _open_app_targets(actions)
    target_app = str(run_manifest.get("target_app") or "")
    done_actions = [row for row in actions if row.get("type") == "done"]
    last_action = actions[-1]

    criteria: dict[str, Mapping[str, Any]] = {
        "trace.integrity": _criterion(
            CRITERION_PASS,
            "intake source tree hash matches current raw run",
            {"source_tree_sha256": intake.get("source_tree_sha256")},
        ),
        "process.source_query_input": _criterion(
            CRITERION_PASS if bool(source_query) else CRITERION_FAIL,
            (
                "source query input is present"
                if source_query
                else "no source query input found"
            ),
            {
                "source_query_sha256": (
                    semantic_sha256(source_query) if source_query else None
                )
            },
        ),
        "process.target_app_open": _criterion(
            CRITERION_PASS if target_app in open_targets else CRITERION_FAIL,
            (
                "target app open_app action is present"
                if target_app in open_targets
                else "target app open_app action missing"
            ),
            {"target_app": target_app, "open_app_targets": open_targets},
        ),
        "process.target_query_input": _criterion(
            (
                CRITERION_PASS
                if bool(target_query) and target_query != source_query
                else CRITERION_FAIL
            ),
            (
                "target-side query is a non-generic transferred phrase"
                if bool(target_query) and target_query != source_query
                else "target-side query is missing or repeats only the source query"
            ),
            {
                "target_query_sha256": (
                    semantic_sha256(target_query) if target_query else None
                ),
                "source_query_sha256": (
                    semantic_sha256(source_query) if source_query else None
                ),
            },
        ),
        "termination.done_after_target": _criterion(
            (
                CRITERION_PASS
                if done_actions
                and last_action.get("type") == "done"
                and target_app in open_targets
                else CRITERION_FAIL
            ),
            (
                "done is the final action after target app was requested"
                if done_actions
                and last_action.get("type") == "done"
                and target_app in open_targets
                else "done is missing or not final after target app request"
            ),
            {
                "done_count": len(done_actions),
                "last_action_type": last_action.get("type"),
            },
        ),
    }

    sort_frame = _first_source_sort_frame(actions, target_app)
    criteria[
        "process.sales_sort_visual_activation"
    ] = _defer_visual_semantics(sort_frame)
    criteria["process.source_selection_rule"] = (
        _criterion(
            CRITERION_FAIL,
            "source selection rule depends on a visibly activated sales sort, which was not satisfied",
            {"blocked_by": "process.sales_sort_visual_activation"},
        )
        if criteria["process.sales_sort_visual_activation"]["status"] == CRITERION_FAIL
        else _criterion(
            CRITERION_UNKNOWN,
            "non-ad first eligible card selection is not yet implemented in deterministic smoke",
            None,
        )
    )
    criteria["process.no_forbidden_surface"] = _criterion(
        CRITERION_UNKNOWN,
        "forbidden semantic surfaces require VLM/OCR or human review; action-log-only smoke does not claim this",
        {"dangerous_action_types_seen": []},
    )

    predicted = _verdict(criteria)
    return {
        "run_id": run_id,
        "task_id": task_id,
        "experiment_id": run_manifest["experiment_id"],
        "runner_profile": "MOBIAGENT_BASELINE",
        "trace_relpath": run_manifest["trace_relpath"],
        "trace_action_count": action_payload.get("action_count"),
        "verifier_version": SMOKE_VERSION,
        "claim_boundary": CLAIM_BOUNDARY,
        "verdict": predicted,
        "criteria": criteria,
        "evidence_frames": {
            "source": sort_frame,
            "terminal": int(
                last_action.get("action_index")
                or action_payload.get("action_count")
                or 0
            ),
        },
    }


def evaluate_case(paths: CasePaths) -> Mapping[str, Any]:
    row = dict(verify_case_without_ground_truth(paths))
    if paths.ground_truth is None:
        raise Phase5IntakeError("development evaluation requires ground truth")
    gt_path = paths.ground_truth.resolve(strict=True)
    gt = _load_json(gt_path, "Phase 5 single-operator GT")
    _validate_gt(gt, run_id=str(row["run_id"]), task_id=str(row["task_id"]))
    truth = _gt_verdict(gt)
    row.update(
        {
            "ground_truth": {
                "verdict": truth,
                "failure_codes": gt.get("failure_codes", []),
                "file_sha256": file_sha256(gt_path),
                "semantic_sha256": semantic_sha256(gt),
                "consumed_after_verifier_decision": True,
                "publication_eligible": False,
            },
            "match_gt": (
                row["verdict"] == truth if truth in {"PASS", "FAIL"} else None
            ),
        }
    )
    return row


def summarize(rows: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    comparable = [
        row for row in rows if row["ground_truth"]["verdict"] in {"PASS", "FAIL"}
    ]
    correct = [row for row in comparable if row["match_gt"] is True]
    counts = {
        "total": len(rows),
        "comparable": len(comparable),
        "correct": len(correct),
        "predicted_pass": sum(row["verdict"] == "PASS" for row in rows),
        "predicted_fail": sum(row["verdict"] == "FAIL" for row in rows),
        "predicted_abstain": sum(row["verdict"] == "ABSTAIN" for row in rows),
        "gt_pass": sum(row["ground_truth"]["verdict"] == "PASS" for row in rows),
        "gt_fail": sum(row["ground_truth"]["verdict"] == "FAIL" for row in rows),
    }
    return {
        "counts": counts,
        "accuracy_on_comparable_development_cases": (
            len(correct) / len(comparable) if comparable else None
        ),
        "publication_eligible": False,
        "note": "development smoke only; sample is tiny and selected from already collected single-operator GT",
    }


def build_report(cases: Sequence[CasePaths]) -> Mapping[str, Any]:
    rows = [evaluate_case(case) for case in cases]
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "verifier_version": SMOKE_VERSION,
        "claim_boundary": CLAIM_BOUNDARY,
        "publication_eligible": False,
        "ground_truth_consumed_by_verifier": False,
        "ground_truth_consumed_after_verifier_decision_for_reporting": True,
        "external_model_calls": 0,
        "api_key_reads": 0,
        "device_actions": 0,
        "rows": rows,
        "summary": summarize(rows),
    }


__all__ = [name for name in globals() if not name.startswith("_")]
