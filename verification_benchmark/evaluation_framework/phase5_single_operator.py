"""Hash-bound single-operator Ground Truth for Phase 5 development evaluation."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .phase5_intake import (
    CLAIM_BOUNDARY,
    PENDING_GROUND_TRUTH,
    Phase5IntakeError,
    _canonical_string,
    _exact_keys,
    file_sha256,
    find_task,
    semantic_sha256,
    strict_json_bytes,
    validate_experiment_manifest,
    validate_intake_receipt,
    verify_intake_receipt,
)


SINGLE_OPERATOR_SCHEMA_VERSION = "harmony-eval-phase5-single-operator-ground-truth-v1"
SINGLE_OPERATOR_AUTHORITY = "PHASE5_SINGLE_OPERATOR_LIVE_OBSERVATION"
SINGLE_OPERATOR_STATUS = "FROZEN_SINGLE_OPERATOR_EXPERIMENT_GROUND_TRUTH"
VERDICTS = {"PASS", "FAIL", "AMBIGUOUS"}
FAILURE_CODES = {
    "NONE",
    "WRONG_APP",
    "WRONG_QUERY",
    "SOURCE_RECOMMENDATION_NOT_OBSERVED",
    "CROSS_APP_TRANSFER_INCORRECT",
    "TARGET_RESULTS_NOT_LOADED",
    "IRRELEVANT_RESULTS",
    "FORBIDDEN_ACTION_OR_SURFACE",
    "PREMATURE_DONE",
    "TASK_INCOMPLETE",
    "EVIDENCE_AMBIGUOUS",
    "OTHER",
}


def _normalize_frames(values: Sequence[int], maximum: int) -> list[int]:
    if not values:
        raise Phase5IntakeError("single-operator review requires evidence frames")
    frames = sorted(set(values))
    if any(
        not isinstance(frame, int)
        or isinstance(frame, bool)
        or frame < 1
        or frame > maximum
        for frame in frames
    ):
        raise Phase5IntakeError("single-operator evidence frame is out of range")
    return frames


def _normalize_failure_codes(verdict: str, values: Sequence[str]) -> list[str]:
    codes = sorted(set(values))
    if not codes:
        if verdict == "PASS":
            return ["NONE"]
        if verdict == "AMBIGUOUS":
            return ["EVIDENCE_AMBIGUOUS"]
        raise Phase5IntakeError("FAIL requires at least one failure code")
    if any(code not in FAILURE_CODES for code in codes):
        raise Phase5IntakeError("unknown single-operator failure code")
    if verdict == "PASS" and codes != ["NONE"]:
        raise Phase5IntakeError("PASS must use failure code NONE only")
    if verdict != "PASS" and "NONE" in codes:
        raise Phase5IntakeError("non-PASS verdict cannot use failure code NONE")
    return codes


def build_single_operator_ground_truth(
    *,
    experiment_manifest: Mapping[str, Any],
    intake_receipt_path: Path,
    run_dir: Path,
    operator_alias: str,
    verdict: str,
    failure_codes: Sequence[str],
    evidence_frames: Sequence[int],
    notes: str,
    observed_live: bool,
    acknowledged_nonpublication: bool,
) -> Mapping[str, Any]:
    validate_experiment_manifest(experiment_manifest)
    receipt = validate_intake_receipt(
        strict_json_bytes(
            intake_receipt_path.read_bytes(), context="Phase 5 intake receipt"
        )
    )
    verify_intake_receipt(receipt, run_dir)
    if receipt["status"] != "ACCEPTED_PENDING_BLIND_REVIEW":
        raise Phase5IntakeError("only an accepted intake receipt can receive Ground Truth")
    if receipt["ground_truth_status"] != PENDING_GROUND_TRUTH:
        raise Phase5IntakeError("intake receipt Ground Truth is not pending")
    if receipt["experiment_manifest_sha256"] != semantic_sha256(experiment_manifest):
        raise Phase5IntakeError("review experiment manifest hash drift")
    task = find_task(experiment_manifest, receipt["task_id"])
    if verdict not in VERDICTS:
        raise Phase5IntakeError("single-operator verdict is invalid")
    if observed_live is not True:
        raise Phase5IntakeError("single-operator authority requires live device observation")
    if acknowledged_nonpublication is not True:
        raise Phase5IntakeError("single-operator non-publication acknowledgement is required")
    alias = _canonical_string(operator_alias, "operator_alias")
    if not isinstance(notes, str) or notes != notes.strip():
        raise Phase5IntakeError("review notes must be a trimmed string")
    capability = receipt["evidence_capability_profile"]
    if not isinstance(capability, Mapping):
        raise Phase5IntakeError("accepted receipt has no evidence capability profile")
    frames = _normalize_frames(evidence_frames, capability["action_count"])
    codes = _normalize_failure_codes(verdict, failure_codes)
    rubric = {
        "task_id": task["task_id"],
        "expected_observable_criteria": task["expected_observable_criteria"],
        "forbidden_actions": task["forbidden_actions"],
    }
    result = {
        "schema_version": SINGLE_OPERATOR_SCHEMA_VERSION,
        "authority": SINGLE_OPERATOR_AUTHORITY,
        "ground_truth_status": SINGLE_OPERATOR_STATUS,
        "publication_eligible": False,
        "eligible_for_development_performance_evaluation": True,
        "formal_double_blind_review_completed": False,
        "claim_boundary": CLAIM_BOUNDARY,
        "experiment_id": receipt["experiment_id"],
        "experiment_manifest_sha256": receipt["experiment_manifest_sha256"],
        "run_id": receipt["run_id"],
        "task_id": receipt["task_id"],
        "intake_receipt_file_sha256": file_sha256(intake_receipt_path),
        "intake_receipt_semantic_sha256": semantic_sha256(receipt),
        "source_tree_sha256": receipt["source_tree_sha256"],
        "review_rubric_sha256": semantic_sha256(rubric),
        "operator_alias_sha256": hashlib.sha256(alias.encode("utf-8")).hexdigest(),
        "observation_mode": "LIVE_DEVICE_PLUS_HASH_BOUND_RECEIPT",
        "observed_live": True,
        "automated_verifier_run_before_review": False,
        "runner_self_report_used_as_authority": False,
        "verdict": verdict,
        "failure_codes": codes,
        "evidence_frames": frames,
        "notes": notes,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    return validate_single_operator_ground_truth(result)


def validate_single_operator_ground_truth(value: Mapping[str, Any]) -> Mapping[str, Any]:
    _exact_keys(
        value,
        {
            "schema_version",
            "authority",
            "ground_truth_status",
            "publication_eligible",
            "eligible_for_development_performance_evaluation",
            "formal_double_blind_review_completed",
            "claim_boundary",
            "experiment_id",
            "experiment_manifest_sha256",
            "run_id",
            "task_id",
            "intake_receipt_file_sha256",
            "intake_receipt_semantic_sha256",
            "source_tree_sha256",
            "review_rubric_sha256",
            "operator_alias_sha256",
            "observation_mode",
            "observed_live",
            "automated_verifier_run_before_review",
            "runner_self_report_used_as_authority",
            "verdict",
            "failure_codes",
            "evidence_frames",
            "notes",
            "created_at_utc",
        },
        "Phase 5 single-operator Ground Truth",
    )
    if value["schema_version"] != SINGLE_OPERATOR_SCHEMA_VERSION:
        raise Phase5IntakeError("single-operator schema version drift")
    if value["authority"] != SINGLE_OPERATOR_AUTHORITY or value["ground_truth_status"] != SINGLE_OPERATOR_STATUS:
        raise Phase5IntakeError("single-operator authority/status drift")
    fixed_booleans = {
        "publication_eligible": False,
        "eligible_for_development_performance_evaluation": True,
        "formal_double_blind_review_completed": False,
        "observed_live": True,
        "automated_verifier_run_before_review": False,
        "runner_self_report_used_as_authority": False,
    }
    for key, expected in fixed_booleans.items():
        if value[key] is not expected:
            raise Phase5IntakeError(f"single-operator fixed boundary drift: {key}")
    if value["claim_boundary"] != CLAIM_BOUNDARY:
        raise Phase5IntakeError("single-operator claim boundary drift")
    if value["verdict"] not in VERDICTS:
        raise Phase5IntakeError("single-operator verdict drift")
    if not isinstance(value["failure_codes"], list):
        raise Phase5IntakeError("single-operator failure_codes must be an array")
    normalized_codes = _normalize_failure_codes(value["verdict"], value["failure_codes"])
    if value["failure_codes"] != normalized_codes:
        raise Phase5IntakeError("single-operator failure_codes must be sorted and unique")
    frames = value["evidence_frames"]
    if (
        not isinstance(frames, list)
        or not frames
        or frames != sorted(set(frames))
        or any(not isinstance(frame, int) or isinstance(frame, bool) or frame < 1 for frame in frames)
    ):
        raise Phase5IntakeError("single-operator evidence_frames are invalid")
    if value["observation_mode"] != "LIVE_DEVICE_PLUS_HASH_BOUND_RECEIPT":
        raise Phase5IntakeError("single-operator observation mode drift")
    if not isinstance(value["notes"], str) or value["notes"] != value["notes"].strip():
        raise Phase5IntakeError("single-operator notes are invalid")
    for key in ("experiment_id", "run_id", "task_id"):
        _canonical_string(value[key], key)
    for key in (
        "experiment_manifest_sha256",
        "intake_receipt_file_sha256",
        "intake_receipt_semantic_sha256",
        "source_tree_sha256",
        "review_rubric_sha256",
        "operator_alias_sha256",
    ):
        text = value[key]
        if not isinstance(text, str) or len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
            raise Phase5IntakeError(f"single-operator invalid SHA field: {key}")
    _canonical_string(value["created_at_utc"], "created_at_utc")
    return value


__all__ = [name for name in globals() if not name.startswith("_")]
