"""Evaluation-only helpers for frozen Phase 5 single-operator ground truth."""

from __future__ import annotations

from typing import Any, Mapping

from .phase5_intake import CLAIM_BOUNDARY, Phase5IntakeError


ALLOWED_GT_STATUSES = {
    "FROZEN_SINGLE_OPERATOR_REALISM_PILOT_GROUND_TRUTH",
    "FROZEN_SINGLE_OPERATOR_FINAL_COHORT_GROUND_TRUTH",
}
GT_VERDICTS = {"PASS", "FAIL", "AMBIGUOUS"}


def ground_truth_verdict(gt: Mapping[str, Any]) -> str:
    verdict = gt.get("verdict")
    if verdict not in GT_VERDICTS:
        raise Phase5IntakeError("ground truth verdict must be PASS, FAIL, or AMBIGUOUS")
    return verdict


def validate_frozen_ground_truth(
    gt: Mapping[str, Any], *, run_id: str, task_id: str
) -> None:
    if gt.get("ground_truth_status") not in ALLOWED_GT_STATUSES:
        raise Phase5IntakeError(
            "GT is not a supported frozen single-operator Phase 5 artifact"
        )
    if gt.get("automated_verifier_run_before_review") is not False:
        raise Phase5IntakeError("GT was not frozen before verifier")
    if gt.get("publication_eligible") is not False:
        raise Phase5IntakeError(
            "development evaluation requires publication_eligible=false"
        )
    if gt.get("run_id") != run_id or gt.get("task_id") != task_id:
        raise Phase5IntakeError("GT run/task binding drift")
    if gt.get("claim_boundary") != CLAIM_BOUNDARY:
        raise Phase5IntakeError("GT claim boundary drift")


__all__ = [
    "ALLOWED_GT_STATUSES",
    "GT_VERDICTS",
    "ground_truth_verdict",
    "validate_frozen_ground_truth",
]
