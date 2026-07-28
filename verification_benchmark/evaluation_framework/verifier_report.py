"""Stable report helpers for the packaged offline trace verifier."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence

from .phase5_intake import file_sha256, semantic_sha256, strict_json_bytes


OFFLINE_REPORT_SCHEMA_VERSION = "harmony-eval-offline-trace-verifier-report-v1"
OFFLINE_CASE_SCHEMA_VERSION = "harmony-eval-offline-trace-verifier-case-v1"
VERIFY_ONLY_MODE = "VERIFY_ONLY"
EVALUATE_MODE = "EVALUATE"
VERDICTS = {"PASS", "FAIL", "ABSTAIN", "INVALID_TRACE", "UNSUPPORTED"}
CRITERION_STATUSES = {
    "SATISFIED",
    "VIOLATED",
    "UNKNOWN_EVIDENCE",
    "UNSUPPORTED_CAPABILITY",
}


class TaskContractError(ValueError):
    """The optional contract provenance artifact cannot be accepted."""


def task_contract_identity(path: Path | None) -> Mapping[str, Any] | None:
    """Validate and fingerprint an optional task contract without executing it."""

    if path is None:
        return None
    try:
        resolved = path.resolve(strict=True)
        payload = strict_json_bytes(resolved.read_bytes(), context="task contract")
    except Exception as exc:  # noqa: BLE001 - normalize configuration boundary.
        raise TaskContractError(f"invalid task contract: {exc}") from exc
    return {
        "path": str(resolved),
        "contract_id": payload.get("contract_id"),
        "schema_version": payload.get("schema_version"),
        "file_sha256": file_sha256(resolved),
        "semantic_sha256": semantic_sha256(payload),
        "decision_role": "PROVENANCE_ONLY_NOT_EXECUTED_BY_SPECIALIZED_VERIFIER",
    }


def provider_metadata(provider: Any) -> Mapping[str, Any]:
    """Return allowlisted provider fields; never serialize the API key."""

    return {
        "base_url": provider.base_url,
        "model": provider.model,
        "api_key_env": provider.api_key_env,
        "transport": provider.transport,
    }


def summarize_cases(rows: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    counts = {verdict.lower(): 0 for verdict in sorted(VERDICTS)}
    for row in rows:
        verdict = str(row.get("verdict"))
        if verdict not in VERDICTS:
            raise ValueError(f"unsupported verifier verdict in report row: {verdict}")
        counts[verdict.lower()] += 1
    summary: dict[str, Any] = {"total": len(rows), **counts}

    evaluated = [row for row in rows if isinstance(row.get("ground_truth"), Mapping)]
    if evaluated:
        comparable = [
            row
            for row in evaluated
            if row["ground_truth"].get("verdict") in {"PASS", "FAIL"}
            and row["verdict"] in {"PASS", "FAIL"}
        ]
        correct = [row for row in comparable if row.get("match_ground_truth") is True]
        summary["evaluation"] = {
            "with_ground_truth": len(evaluated),
            "comparable": len(comparable),
            "correct": len(correct),
            "accuracy_on_comparable_development_cases": (
                len(correct) / len(comparable) if comparable else None
            ),
            "ground_truth_pass": sum(
                row["ground_truth"].get("verdict") == "PASS" for row in evaluated
            ),
            "ground_truth_fail": sum(
                row["ground_truth"].get("verdict") == "FAIL" for row in evaluated
            ),
            "ground_truth_ambiguous": sum(
                row["ground_truth"].get("verdict") == "AMBIGUOUS" for row in evaluated
            ),
        }
    return summary


def validate_case_report(row: Mapping[str, Any]) -> None:
    if row.get("schema_version") != OFFLINE_CASE_SCHEMA_VERSION:
        raise ValueError("unsupported offline verifier case schema")
    if row.get("verdict") not in VERDICTS:
        raise ValueError("offline verifier case has invalid verdict")
    criteria = row.get("criteria")
    if not isinstance(criteria, Mapping):
        raise ValueError("offline verifier case criteria must be an object")
    for criterion_id, criterion in criteria.items():
        if not isinstance(criterion_id, str) or not isinstance(criterion, Mapping):
            raise ValueError("offline verifier criterion record is invalid")
        if criterion.get("status") not in CRITERION_STATUSES:
            raise ValueError(f"invalid criterion status for {criterion_id}")
    provider = row.get("provider")
    if not isinstance(provider, Mapping) or "api_key" in provider:
        raise ValueError("offline verifier provider metadata is unsafe")


def build_offline_report(
    *,
    mode: str,
    rows: Sequence[Mapping[str, Any]],
    provider: Any,
    verifier_version: str,
    baseline_enabled: bool,
) -> Mapping[str, Any]:
    if mode not in {VERIFY_ONLY_MODE, EVALUATE_MODE}:
        raise ValueError("offline verifier mode must be VERIFY_ONLY or EVALUATE")
    for row in rows:
        validate_case_report(row)
    report = {
        "schema_version": OFFLINE_REPORT_SCHEMA_VERSION,
        "report_kind": "OFFLINE_TRACE_VERIFICATION",
        "mode": mode,
        "verifier_version": verifier_version,
        "architecture_scope": "PHASE5_CROSS_APP_SPECIALIZED_OFFLINE_AUDIT",
        "contract_ir_execution": False,
        "ground_truth_consumed_by_verifier": False,
        "ground_truth_consumed_after_verifier_decision_for_reporting": (
            mode == EVALUATE_MODE
        ),
        "provider": provider_metadata(provider),
        "baseline": {
            "enabled": baseline_enabled,
            "role": "OPTIONAL_COMPARISON_NOT_PRIMARY",
        },
        "rows": list(rows),
        "summary": summarize_cases(rows),
        "publication_eligible": False,
    }
    return report


__all__ = [
    "CRITERION_STATUSES",
    "EVALUATE_MODE",
    "OFFLINE_CASE_SCHEMA_VERSION",
    "OFFLINE_REPORT_SCHEMA_VERSION",
    "TaskContractError",
    "VERDICTS",
    "VERIFY_ONLY_MODE",
    "build_offline_report",
    "provider_metadata",
    "summarize_cases",
    "task_contract_identity",
    "validate_case_report",
]
