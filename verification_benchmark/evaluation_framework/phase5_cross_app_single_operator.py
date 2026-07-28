"""Single-operator Ground Truth for Phase 5 cross-App development runs."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .phase5_cross_app import (
    CLAIM_BOUNDARY,
    PENDING_GROUND_TRUTH,
    Phase5IntakeError,
    _canonical_string,
    _exact_keys,
    file_sha256,
    find_task,
    semantic_sha256,
    validate_experiment_manifest,
    validate_intake_receipt,
    verify_intake_receipt,
)
from .phase5_intake import strict_json_bytes


SCHEMA_VERSION = "harmony-eval-phase5-cross-app-single-operator-ground-truth-v2"
AUTHORITY = "PHASE5_SINGLE_OPERATOR_LIVE_CROSS_APP_OBSERVATION"
STATUS = "FROZEN_SINGLE_OPERATOR_CROSS_APP_EXPERIMENT_GROUND_TRUTH"
VERDICTS = {"PASS", "FAIL", "AMBIGUOUS"}
FAILURE_CODES = {
    "NONE",
    "SOURCE_APP_NOT_OBSERVED",
    "SOURCE_QUERY_INCORRECT",
    "SOURCE_EVIDENCE_NOT_OBSERVED",
    "DYNAMIC_SLOT_UNSUPPORTED",
    "TARGET_APP_NOT_OBSERVED",
    "TARGET_QUERY_MISSING_OR_INCORRECT",
    "CROSS_APP_TRANSFER_LOSS",
    "TARGET_RESULTS_NOT_LOADED",
    "IRRELEVANT_RESULTS",
    "FORBIDDEN_ACTION_OR_SURFACE",
    "PREMATURE_DONE",
    "TASK_INCOMPLETE",
    "EVIDENCE_AMBIGUOUS",
    "OTHER",
}


def _frames(values: Sequence[int], maximum: int, context: str) -> list[int]:
    frames = sorted(set(values))
    if not frames or any(
        not isinstance(frame, int)
        or isinstance(frame, bool)
        or frame < 1
        or frame > maximum
        for frame in frames
    ):
        raise Phase5IntakeError(f"{context} must contain in-range evidence frames")
    return frames


def _failure_codes(verdict: str, values: Sequence[str]) -> list[str]:
    codes = sorted(set(values))
    if not codes:
        if verdict == "PASS":
            return ["NONE"]
        if verdict == "AMBIGUOUS":
            return ["EVIDENCE_AMBIGUOUS"]
        raise Phase5IntakeError("cross-App FAIL requires a failure code")
    if any(code not in FAILURE_CODES for code in codes):
        raise Phase5IntakeError("unknown cross-App failure code")
    if verdict == "PASS" and codes != ["NONE"]:
        raise Phase5IntakeError("cross-App PASS must use NONE only")
    if verdict != "PASS" and "NONE" in codes:
        raise Phase5IntakeError("cross-App non-PASS cannot use NONE")
    return codes


def _slots(values: Sequence[str]) -> list[str]:
    slots: list[str] = []
    for index, value in enumerate(values):
        slot = _canonical_string(value, f"observed_source_slots[{index}]")
        if slot in slots:
            raise Phase5IntakeError("observed source slots must be unique")
        slots.append(slot)
    return slots


def build_cross_app_single_operator_ground_truth(
    *,
    experiment_manifest: Mapping[str, Any],
    intake_receipt_path: Path,
    run_dir: Path,
    operator_alias: str,
    verdict: str,
    failure_codes: Sequence[str],
    source_evidence_frames: Sequence[int],
    target_evidence_frames: Sequence[int],
    observed_source_slots: Sequence[str],
    observed_target_query: str,
    notes: str,
    observed_live: bool,
    acknowledged_nonpublication: bool,
) -> Mapping[str, Any]:
    validate_experiment_manifest(experiment_manifest)
    receipt = validate_intake_receipt(
        strict_json_bytes(
            intake_receipt_path.read_bytes(),
            context="Phase 5 cross-App intake receipt",
        )
    )
    verify_intake_receipt(receipt, run_dir)
    if receipt["status"] != "ACCEPTED_PENDING_SINGLE_OPERATOR_REVIEW":
        raise Phase5IntakeError("only an accepted cross-App receipt can receive GT")
    if receipt["ground_truth_status"] != PENDING_GROUND_TRUTH:
        raise Phase5IntakeError("cross-App receipt Ground Truth is not pending")
    if receipt["experiment_manifest_sha256"] != semantic_sha256(experiment_manifest):
        raise Phase5IntakeError("cross-App review manifest hash drift")
    task = find_task(experiment_manifest, receipt["task_id"])
    if verdict not in VERDICTS:
        raise Phase5IntakeError("cross-App verdict is invalid")
    if observed_live is not True:
        raise Phase5IntakeError("cross-App authority requires live observation")
    if acknowledged_nonpublication is not True:
        raise Phase5IntakeError("cross-App non-publication acknowledgement is required")
    alias = _canonical_string(operator_alias, "operator_alias")
    note_text = _canonical_string(notes, "review notes")
    capability = receipt["evidence_capability_profile"]
    if not isinstance(capability, Mapping):
        raise Phase5IntakeError("accepted cross-App receipt lacks evidence capability")
    source_frames = _frames(
        source_evidence_frames, capability["action_count"], "source evidence"
    )
    target_frames = _frames(
        target_evidence_frames, capability["action_count"], "target evidence"
    )
    codes = _failure_codes(verdict, failure_codes)
    slots = _slots(observed_source_slots)
    target_query = observed_target_query.strip()
    if observed_target_query != target_query:
        raise Phase5IntakeError("observed target query must be trimmed")
    required_slots = task["dynamic_slot_policy"]["minimum_slots"]
    if verdict == "PASS":
        if len(slots) != required_slots:
            raise Phase5IntakeError(
                "cross-App PASS requires the frozen dynamic slot count"
            )
        if not target_query or any(slot not in target_query for slot in slots):
            raise Phase5IntakeError(
                "cross-App PASS target query must contain every observed source slot"
            )
    rubric = {
        "task_id": task["task_id"],
        "source_query": task["source_query"],
        "dynamic_slot_policy": task["dynamic_slot_policy"],
        "expected_observable_criteria": task["expected_observable_criteria"],
        "forbidden_actions": task["forbidden_actions"],
    }
    result = {
        "schema_version": SCHEMA_VERSION,
        "authority": AUTHORITY,
        "ground_truth_status": STATUS,
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
        "observation_mode": "LIVE_DEVICE_PLUS_HASH_BOUND_CROSS_APP_RECEIPT",
        "observed_live": True,
        "automated_verifier_run_before_review": False,
        "runner_self_report_used_as_authority": False,
        "verdict": verdict,
        "failure_codes": codes,
        "source_evidence_frames": source_frames,
        "target_evidence_frames": target_frames,
        "observed_source_slots": slots,
        "observed_target_query": target_query,
        "notes": note_text,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    return validate_cross_app_single_operator_ground_truth(result)


def validate_cross_app_single_operator_ground_truth(
    value: Mapping[str, Any],
) -> Mapping[str, Any]:
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
            "source_evidence_frames",
            "target_evidence_frames",
            "observed_source_slots",
            "observed_target_query",
            "notes",
            "created_at_utc",
        },
        "Phase 5 cross-App single-operator Ground Truth",
    )
    fixed = {
        "schema_version": SCHEMA_VERSION,
        "authority": AUTHORITY,
        "ground_truth_status": STATUS,
        "publication_eligible": False,
        "eligible_for_development_performance_evaluation": True,
        "formal_double_blind_review_completed": False,
        "claim_boundary": CLAIM_BOUNDARY,
        "observation_mode": "LIVE_DEVICE_PLUS_HASH_BOUND_CROSS_APP_RECEIPT",
        "observed_live": True,
        "automated_verifier_run_before_review": False,
        "runner_self_report_used_as_authority": False,
    }
    for key, expected in fixed.items():
        if value[key] != expected:
            raise Phase5IntakeError(f"cross-App GT fixed boundary drift at {key}")
    if value["verdict"] not in VERDICTS:
        raise Phase5IntakeError("cross-App GT verdict drift")
    if not isinstance(value["failure_codes"], list) or value[
        "failure_codes"
    ] != _failure_codes(value["verdict"], value["failure_codes"]):
        raise Phase5IntakeError("cross-App GT failure codes are not canonical")
    for key in ("source_evidence_frames", "target_evidence_frames"):
        frames = value[key]
        if (
            not isinstance(frames, list)
            or not frames
            or frames != sorted(set(frames))
            or any(
                not isinstance(frame, int) or isinstance(frame, bool) or frame < 1
                for frame in frames
            )
        ):
            raise Phase5IntakeError(f"cross-App GT invalid frames: {key}")
    if not isinstance(value["observed_source_slots"], list) or value[
        "observed_source_slots"
    ] != _slots(value["observed_source_slots"]):
        raise Phase5IntakeError("cross-App GT source slots are not canonical")
    if (
        not isinstance(value["observed_target_query"], str)
        or value["observed_target_query"] != value["observed_target_query"].strip()
    ):
        raise Phase5IntakeError("cross-App GT target query is invalid")
    for key in ("experiment_id", "run_id", "task_id", "notes", "created_at_utc"):
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
        if (
            not isinstance(text, str)
            or len(text) != 64
            or any(char not in "0123456789abcdef" for char in text)
        ):
            raise Phase5IntakeError(f"cross-App GT invalid SHA field: {key}")
    return value


__all__ = [name for name in globals() if not name.startswith("_")]
