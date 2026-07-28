"""Packaged API for specialized Phase 5 offline trace verification."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Protocol, Sequence

from .phase5_full_verifier_comparison import (
    FULL_VERIFIER_VERSION,
    ProviderConfig,
    VisionCallRecorder,
    evaluate_full_case,
)
from .phase5_ground_truth import (
    ground_truth_verdict,
    validate_frozen_ground_truth,
)
from .phase5_intake import (
    CLAIM_BOUNDARY,
    Phase5IntakeError,
    file_sha256,
    semantic_sha256,
)
from .phase5_trace_case import CasePaths, find_run_manifest, load_json
from .verifier_report import (
    EVALUATE_MODE,
    OFFLINE_CASE_SCHEMA_VERSION,
    TaskContractError,
    VERIFY_ONLY_MODE,
    build_offline_report,
    provider_metadata,
    task_contract_identity,
    validate_case_report,
)


PACKAGED_VERIFIER_VERSION = "harmony-eval-offline-trace-verifier-v1"


class BaselineAdapter(Protocol):
    def verify(
        self, case: CasePaths, recorder: VisionCallRecorder
    ) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class VerifierConfig:
    provider: ProviderConfig
    continue_on_error: bool = True


def _case_identity(case: CasePaths) -> Mapping[str, Any]:
    try:
        run_dir = case.run_dir.resolve(strict=True)
        intake_receipt = case.intake_receipt.resolve(strict=True)
        run = find_run_manifest(run_dir)
    except Phase5IntakeError:
        raise
    except OSError as exc:
        raise Phase5IntakeError(f"trace case input is unavailable: {exc}") from exc
    return {
        "run_id": str(run["run_id"]),
        "task_id": str(run["task_id"]),
        "experiment_id": run.get("experiment_id"),
        "runner_profile": run.get("runner_profile", "MOBIAGENT_BASELINE"),
        "run_dir": str(run_dir),
        "intake_receipt": str(intake_receipt),
    }


def _error_case_report(
    *,
    case: CasePaths,
    provider: ProviderConfig,
    verdict: str,
    failure_code: str,
    error: Exception,
    elapsed_ms: float,
    calls: Sequence[Mapping[str, Any]],
) -> Mapping[str, Any]:
    try:
        identity = _case_identity(case)
    except Exception:  # noqa: BLE001 - preserve original classified error.
        identity = {
            "run_id": None,
            "task_id": None,
            "experiment_id": None,
            "runner_profile": None,
            "run_dir": str(case.run_dir.resolve()),
            "intake_receipt": str(case.intake_receipt.resolve()),
        }
    return {
        "schema_version": OFFLINE_CASE_SCHEMA_VERSION,
        **identity,
        "verdict": verdict,
        "criteria": {},
        "evidence_frames": {},
        "task_contract": None,
        "claim_boundary": CLAIM_BOUNDARY,
        "verifier": {
            "name": "OFFLINE_TRACE_VERIFIER",
            "package_version": PACKAGED_VERIFIER_VERSION,
            "engine_version": FULL_VERIFIER_VERSION,
        },
        "provider": provider_metadata(provider),
        "measurements": {
            "latency_ms": round(elapsed_ms, 3),
            "model_request_count": len(calls),
            "model_calls": list(calls),
        },
        "failure": {
            "code": failure_code,
            "error_type": type(error).__name__,
            "message": str(error),
        },
    }


def verify_trace_case(
    case: CasePaths,
    config: VerifierConfig,
    *,
    recorder: Optional[VisionCallRecorder] = None,
) -> Mapping[str, Any]:
    """Verify one trace without reading its optional ground truth."""

    active_recorder = recorder or VisionCallRecorder(config.provider)
    before_calls = len(active_recorder.calls)
    started = time.perf_counter()
    try:
        identity = _case_identity(case)
        contract = task_contract_identity(case.task_contract)
        result = evaluate_full_case(case, active_recorder)
        calls = active_recorder.calls[before_calls:]
        row = {
            "schema_version": OFFLINE_CASE_SCHEMA_VERSION,
            **identity,
            "verdict": result["verdict"],
            "criteria": result["criteria"],
            "evidence_frames": result["evidence_frames"],
            "task_contract": contract,
            "claim_boundary": CLAIM_BOUNDARY,
            "verifier": {
                "name": "OFFLINE_TRACE_VERIFIER",
                "package_version": PACKAGED_VERIFIER_VERSION,
                "engine_version": result["verifier_version"],
                "engine": result["verifier"],
            },
            "provider": provider_metadata(config.provider),
            "measurements": {
                "latency_ms": round((time.perf_counter() - started) * 1000, 3),
                "model_request_count": len(calls),
                "model_calls": list(calls),
            },
            "failure": None,
        }
        validate_case_report(row)
        return row
    except TaskContractError as exc:
        if not config.continue_on_error:
            raise
        return _error_case_report(
            case=case,
            provider=config.provider,
            verdict="UNSUPPORTED",
            failure_code="TASK_CONTRACT_UNSUPPORTED",
            error=exc,
            elapsed_ms=(time.perf_counter() - started) * 1000,
            calls=active_recorder.calls[before_calls:],
        )
    except Phase5IntakeError as exc:
        if not config.continue_on_error:
            raise
        return _error_case_report(
            case=case,
            provider=config.provider,
            verdict="INVALID_TRACE",
            failure_code="TRACE_INTAKE_INVALID",
            error=exc,
            elapsed_ms=(time.perf_counter() - started) * 1000,
            calls=active_recorder.calls[before_calls:],
        )
    except Exception as exc:  # noqa: BLE001 - fail closed into ABSTAIN report.
        if not config.continue_on_error:
            raise
        return _error_case_report(
            case=case,
            provider=config.provider,
            verdict="ABSTAIN",
            failure_code="VERIFIER_BACKEND_ERROR",
            error=exc,
            elapsed_ms=(time.perf_counter() - started) * 1000,
            calls=active_recorder.calls[before_calls:],
        )


def _attach_baseline(
    rows: Sequence[Mapping[str, Any]],
    cases: Sequence[CasePaths],
    adapter: BaselineAdapter,
    recorder: VisionCallRecorder,
) -> list[Mapping[str, Any]]:
    combined: list[Mapping[str, Any]] = []
    for row, case in zip(rows, cases):
        mutable = dict(row)
        try:
            mutable["baseline"] = adapter.verify(case, recorder)
        except Exception as exc:  # noqa: BLE001 - baseline cannot abort primary.
            mutable["baseline"] = {
                "verdict": "ABSTAIN",
                "role": "OPTIONAL_COMPARISON_NOT_PRIMARY",
                "failure": {
                    "code": "BASELINE_ERROR",
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                },
            }
        combined.append(mutable)
    return combined


def verify_trace_batch(
    cases: Sequence[CasePaths],
    config: VerifierConfig,
    *,
    baseline_adapter: BaselineAdapter | None = None,
) -> Mapping[str, Any]:
    if not cases:
        raise ValueError("at least one trace case is required")
    primary_recorder = VisionCallRecorder(config.provider)
    rows = [
        verify_trace_case(case, config, recorder=primary_recorder) for case in cases
    ]
    if baseline_adapter is not None:
        rows = _attach_baseline(
            rows,
            cases,
            baseline_adapter,
            VisionCallRecorder(config.provider),
        )
    return build_offline_report(
        mode=VERIFY_ONLY_MODE,
        rows=rows,
        provider=config.provider,
        verifier_version=PACKAGED_VERIFIER_VERSION,
        baseline_enabled=baseline_adapter is not None,
    )


def _ground_truth_after_decision(
    case: CasePaths, row: Mapping[str, Any]
) -> Mapping[str, Any]:
    if case.ground_truth is None:
        raise Phase5IntakeError("evaluate mode requires ground truth for every case")
    path = case.ground_truth.resolve(strict=True)
    gt = load_json(path, "Phase 5 single-operator ground truth")
    validate_frozen_ground_truth(
        gt, run_id=str(row["run_id"]), task_id=str(row["task_id"])
    )
    return {
        "verdict": ground_truth_verdict(gt),
        "failure_codes": gt.get("failure_codes", []),
        "file_sha256": file_sha256(path),
        "semantic_sha256": semantic_sha256(gt),
        "consumed_after_verifier_decision": True,
        "publication_eligible": False,
    }


def evaluate_trace_batch(
    cases: Sequence[CasePaths],
    config: VerifierConfig,
    *,
    baseline_adapter: BaselineAdapter | None = None,
) -> Mapping[str, Any]:
    """Verify every case first, then attach frozen GT for development metrics."""

    verification = verify_trace_batch(cases, config, baseline_adapter=baseline_adapter)
    evaluated_rows: list[Mapping[str, Any]] = []
    for row, case in zip(verification["rows"], cases):
        mutable = dict(row)
        gt = _ground_truth_after_decision(case, row)
        mutable["ground_truth"] = gt
        mutable["match_ground_truth"] = (
            row["verdict"] == gt["verdict"]
            if row["verdict"] in {"PASS", "FAIL"} and gt["verdict"] in {"PASS", "FAIL"}
            else None
        )
        evaluated_rows.append(mutable)
    return build_offline_report(
        mode=EVALUATE_MODE,
        rows=evaluated_rows,
        provider=config.provider,
        verifier_version=PACKAGED_VERIFIER_VERSION,
        baseline_enabled=baseline_adapter is not None,
    )


__all__ = [
    "BaselineAdapter",
    "PACKAGED_VERIFIER_VERSION",
    "VerifierConfig",
    "evaluate_trace_batch",
    "verify_trace_batch",
    "verify_trace_case",
]
