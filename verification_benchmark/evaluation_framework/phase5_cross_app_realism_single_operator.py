"""Single-operator Ground Truth for the Phase 5 v3 realism pilot."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .phase5_cross_app_realism import (
    CLAIM_BOUNDARY,
    EXPERIMENT_ID,
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


SCHEMA_VERSION = "harmony-eval-phase5-cross-app-realism-single-operator-ground-truth-v3"
AUTHORITY = "PHASE5_SINGLE_OPERATOR_LIVE_REALISM_PILOT_OBSERVATION"
STATUS = "FROZEN_SINGLE_OPERATOR_REALISM_PILOT_GROUND_TRUTH"
VERDICTS = {"PASS", "FAIL", "AMBIGUOUS"}
OBSERVATION_STATES = {"YES", "NO", "AMBIGUOUS"}
FAILURE_CODES = {
    "NONE",
    "SOURCE_APP_NOT_OBSERVED",
    "SOURCE_QUERY_INCORRECT",
    "SALES_SORT_NOT_ACTIVATED",
    "SELECTION_RULE_VIOLATED",
    "SOURCE_IDENTITY_NOT_DISTINCTIVE",
    "TARGET_APP_NOT_OBSERVED",
    "TARGET_QUERY_MISSING_OR_INCORRECT",
    "TARGET_NO_SAME_PRODUCT_EVIDENCE",
    "FORBIDDEN_ACTION_OR_SURFACE",
    "PREMATURE_DONE",
    "TASK_INCOMPLETE",
    "EVIDENCE_AMBIGUOUS",
    "OTHER",
}
OBSERVATION_KEYS = {
    "source_query_visible",
    "sales_sort_activated",
    "selection_rule_satisfied",
    "source_identity_distinctive",
    "target_app_observed",
    "target_query_contains_source_identity",
    "same_product_target_evidence_visible",
    "forbidden_action_or_surface_observed",
    "task_complete_before_done",
}


def _frames(
    values: Sequence[int], maximum: int, context: str, *, required: bool
) -> list[int]:
    frames = sorted(set(values))
    if required and not frames:
        raise Phase5IntakeError(f"{context} requires at least one evidence frame")
    if any(
        not isinstance(frame, int)
        or isinstance(frame, bool)
        or frame < 1
        or frame > maximum
        for frame in frames
    ):
        raise Phase5IntakeError(f"{context} contains an out-of-range evidence frame")
    return frames


def _failure_codes(verdict: str, values: Sequence[str]) -> list[str]:
    codes = sorted(set(values))
    if not codes:
        if verdict == "PASS":
            return ["NONE"]
        if verdict == "AMBIGUOUS":
            return ["EVIDENCE_AMBIGUOUS"]
        raise Phase5IntakeError("realism-pilot FAIL requires a failure code")
    if any(code not in FAILURE_CODES for code in codes):
        raise Phase5IntakeError("unknown realism-pilot failure code")
    if verdict == "PASS" and codes != ["NONE"]:
        raise Phase5IntakeError("realism-pilot PASS must use NONE only")
    if verdict != "PASS" and "NONE" in codes:
        raise Phase5IntakeError("realism-pilot non-PASS cannot use NONE")
    return codes


def _observations(value: Mapping[str, str], verdict: str) -> Mapping[str, str]:
    _exact_keys(value, OBSERVATION_KEYS, "realism-pilot rubric observations")
    if any(state not in OBSERVATION_STATES for state in value.values()):
        raise Phase5IntakeError("realism-pilot rubric observation state is invalid")
    if verdict == "PASS":
        expected = {key: "YES" for key in OBSERVATION_KEYS}
        expected["forbidden_action_or_surface_observed"] = "NO"
        if value != expected:
            raise Phase5IntakeError("realism-pilot PASS requires every rubric item")
    return value


def build_realism_single_operator_ground_truth(
    *,
    experiment_manifest: Mapping[str, Any],
    intake_receipt_path: Path,
    run_dir: Path,
    operator_alias: str,
    verdict: str,
    failure_codes: Sequence[str],
    source_evidence_frames: Sequence[int],
    target_evidence_frames: Sequence[int],
    observed_source_identity: str,
    observed_target_query: str,
    rubric_observations: Mapping[str, str],
    notes: str,
    observed_live: bool,
    acknowledged_nonpublication: bool,
) -> Mapping[str, Any]:
    validate_experiment_manifest(experiment_manifest)
    receipt = validate_intake_receipt(
        strict_json_bytes(
            intake_receipt_path.read_bytes(),
            context="Phase 5 realism-pilot intake receipt",
        )
    )
    verify_intake_receipt(receipt, run_dir)
    if receipt["status"] != "ACCEPTED_PENDING_SINGLE_OPERATOR_REVIEW":
        raise Phase5IntakeError("only an accepted realism receipt can receive GT")
    if receipt["ground_truth_status"] != PENDING_GROUND_TRUTH:
        raise Phase5IntakeError("realism receipt Ground Truth is not pending")
    if receipt["experiment_manifest_sha256"] != semantic_sha256(experiment_manifest):
        raise Phase5IntakeError("realism review manifest hash drift")
    task = find_task(experiment_manifest, receipt["task_id"])
    if verdict not in VERDICTS:
        raise Phase5IntakeError("realism-pilot verdict is invalid")
    if observed_live is not True:
        raise Phase5IntakeError("realism-pilot authority requires live observation")
    if acknowledged_nonpublication is not True:
        raise Phase5IntakeError(
            "realism-pilot non-publication acknowledgement required"
        )
    alias = _canonical_string(operator_alias, "operator_alias")
    note_text = _canonical_string(notes, "review notes")
    capability = receipt["evidence_capability_profile"]
    if not isinstance(capability, Mapping):
        raise Phase5IntakeError("accepted realism receipt lacks evidence capability")
    source_frames = _frames(
        source_evidence_frames,
        capability["action_count"],
        "source evidence",
        required=verdict == "PASS",
    )
    target_frames = _frames(
        target_evidence_frames,
        capability["action_count"],
        "target evidence",
        required=verdict == "PASS",
    )
    codes = _failure_codes(verdict, failure_codes)
    observations = _observations(dict(rubric_observations), verdict)
    identity = observed_source_identity.strip()
    query = observed_target_query.strip()
    if identity != observed_source_identity or query != observed_target_query:
        raise Phase5IntakeError("observed identity and target query must be trimmed")
    if verdict == "PASS" and (not identity or identity not in query):
        raise Phase5IntakeError(
            "realism-pilot PASS target query must contain the visible source identity"
        )
    rubric = {
        "task_id": task["task_id"],
        "source_query": task["source_query"],
        "source_sort": task["source_sort"],
        "selection_policy": task["selection_policy"],
        "transfer_policy": task["transfer_policy"],
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
        "experiment_id": EXPERIMENT_ID,
        "experiment_manifest_sha256": receipt["experiment_manifest_sha256"],
        "run_id": receipt["run_id"],
        "task_id": receipt["task_id"],
        "intake_receipt_file_sha256": file_sha256(intake_receipt_path),
        "intake_receipt_semantic_sha256": semantic_sha256(receipt),
        "source_tree_sha256": receipt["source_tree_sha256"],
        "review_rubric_sha256": semantic_sha256(rubric),
        "operator_alias_sha256": hashlib.sha256(alias.encode("utf-8")).hexdigest(),
        "observation_mode": "LIVE_DEVICE_PLUS_HASH_BOUND_REALISM_RECEIPT",
        "observed_live": True,
        "automated_verifier_run_before_review": False,
        "runner_self_report_used_as_authority": False,
        "verdict": verdict,
        "failure_codes": codes,
        "source_evidence_frames": source_frames,
        "target_evidence_frames": target_frames,
        "observed_source_identity": identity,
        "observed_target_query": query,
        "rubric_observations": observations,
        "notes": note_text,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    return validate_realism_single_operator_ground_truth(result)


def validate_realism_single_operator_ground_truth(
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
            "observed_source_identity",
            "observed_target_query",
            "rubric_observations",
            "notes",
            "created_at_utc",
        },
        "Phase 5 realism-pilot single-operator Ground Truth",
    )
    fixed = {
        "schema_version": SCHEMA_VERSION,
        "authority": AUTHORITY,
        "ground_truth_status": STATUS,
        "publication_eligible": False,
        "eligible_for_development_performance_evaluation": True,
        "formal_double_blind_review_completed": False,
        "claim_boundary": CLAIM_BOUNDARY,
        "experiment_id": EXPERIMENT_ID,
        "observation_mode": "LIVE_DEVICE_PLUS_HASH_BOUND_REALISM_RECEIPT",
        "observed_live": True,
        "automated_verifier_run_before_review": False,
        "runner_self_report_used_as_authority": False,
    }
    for key, expected in fixed.items():
        if value[key] != expected:
            raise Phase5IntakeError(f"realism-pilot GT fixed boundary drift at {key}")
    verdict = value["verdict"]
    if verdict not in VERDICTS:
        raise Phase5IntakeError("realism-pilot GT verdict drift")
    if value["failure_codes"] != _failure_codes(verdict, value["failure_codes"]):
        raise Phase5IntakeError("realism-pilot GT failure codes are not canonical")
    _observations(value["rubric_observations"], verdict)
    for key in ("source_evidence_frames", "target_evidence_frames"):
        frames = value[key]
        if (
            not isinstance(frames, list)
            or frames != sorted(set(frames))
            or any(
                not isinstance(frame, int) or isinstance(frame, bool) or frame < 1
                for frame in frames
            )
            or (verdict == "PASS" and not frames)
        ):
            raise Phase5IntakeError(f"realism-pilot GT invalid frames: {key}")
    identity = value["observed_source_identity"]
    query = value["observed_target_query"]
    if (
        not isinstance(identity, str)
        or identity != identity.strip()
        or not isinstance(query, str)
        or query != query.strip()
    ):
        raise Phase5IntakeError("realism-pilot GT identity/query is invalid")
    if verdict == "PASS" and (not identity or identity not in query):
        raise Phase5IntakeError("realism-pilot PASS identity/query binding drift")
    for key in ("run_id", "task_id", "notes", "created_at_utc"):
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
            raise Phase5IntakeError(f"realism-pilot GT invalid SHA field: {key}")
    return value


__all__ = [name for name in globals() if not name.startswith("_")]
