"""Report serialization for App functional tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from .contract import AppTestContract
from .executor import ExecutionRecord
from .offline_verifier import OfflineTraceReview
from .schema import TestCaseSpec, dump_json
from .result_types import (
    AppBehaviorResult,
    AttributionResult,
    ExecutionConformanceResult,
)
from .run_envelope import RUN_ENVELOPE_FILE, write_run_envelope
from .verification_runner import VerificationRunResult


REPORT_SCHEMA_VERSION = "app-test-report-v1"


def build_report(
    *,
    test_case: TestCaseSpec,
    contract: AppTestContract,
    execution: ExecutionRecord,
    conformance: ExecutionConformanceResult,
    behavior: AppBehaviorResult,
    attribution: AttributionResult,
    direct_behavior: AppBehaviorResult | None = None,
    verification_result: VerificationRunResult | None = None,
    business_offline_review: OfflineTraceReview | None = None,
    verification_offline_review: OfflineTraceReview | None = None,
    run_envelope: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    direct = direct_behavior or behavior
    verification_payload = (
        verification_result.as_dict() if verification_result is not None else None
    )
    verification_trace = (
        verification_payload.get("observation_record")
        if isinstance(verification_payload, Mapping)
        else None
    )
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "test_case_id": test_case.test_case_id,
        "test_case_sha256": test_case.sha256,
        "contract": contract.as_dict(),
        "contract_sha256": contract.sha256,
        "run_envelope_sha256": (
            run_envelope.get("run_envelope_sha256")
            if isinstance(run_envelope, Mapping)
            else None
        ),
        "app_under_test": test_case.app_under_test.as_dict(),
        "feature": test_case.feature,
        "verification_runner_policy": test_case.verification_runner_policy,
        "overall_result": attribution.overall_result,
        "attribution": attribution.as_dict(),
        "execution_result": conformance.as_dict(),
        "business_offline_review": (
            business_offline_review.as_dict()
            if business_offline_review is not None
            else None
        ),
        "direct_app_behavior_result": direct.as_dict(),
        "app_behavior_result": behavior.as_dict(),
        "verification_offline_review": (
            verification_offline_review.as_dict()
            if verification_offline_review is not None
            else None
        ),
        "verification_runner_result": verification_payload,
        "temporal_boundaries": (
            dict(run_envelope.get("temporal_boundaries", {}))
            if isinstance(run_envelope, Mapping)
            else None
        ),
        "executor": execution.executor,
        "step_count": len(test_case.steps),
        "completed_step_count": sum(
            item.status == "STEP_COMPLETED" for item in execution.step_results
        ),
        "step_results": [item.as_dict() for item in execution.step_results],
        "business_execution_trace": {
            "executor": execution.executor,
            "step_results": [item.as_dict() for item in execution.step_results],
            "final_evidence_state": execution.final_state.as_dict(),
            "raw_trace_dir": execution.raw_trace_dir,
        },
        "verification_trace": verification_trace,
        "final_evidence_state": execution.final_state.as_dict(),
        "artifacts": {
            "raw_trace_dir": execution.raw_trace_dir,
            "test_execution_manifest": "test_execution_manifest.json",
            "verification_trace": (
                "verification_trace.json"
                if isinstance(verification_trace, Mapping)
                else None
            ),
            "verification_runner_result": (
                "verification_runner_result.json"
                if verification_payload is not None
                else None
            ),
            "business_offline_review": (
                "business_offline_review.json"
                if business_offline_review is not None
                else None
            ),
            "verification_offline_review": (
                "verification_offline_review.json"
                if verification_offline_review is not None
                else None
            ),
            "direct_app_behavior_result": "direct_app_behavior_result.json",
            "execution_result": "execution_result.json",
            "app_behavior_result": "app_behavior_result.json",
            "attribution_result": "attribution_result.json",
            "run_envelope": RUN_ENVELOPE_FILE if isinstance(run_envelope, Mapping) else None,
            "contract": "app_test_contract.json",
        },
    }


def write_report_bundle(
    output_dir: Path,
    report: Mapping[str, Any],
    test_case: TestCaseSpec,
    *,
    run_envelope: Mapping[str, Any] | None = None,
) -> None:
    root = output_dir.resolve()
    root.mkdir(parents=True, exist_ok=True)
    dump_json(root / "test_case.normalized.json", test_case.as_dict())
    if isinstance(report.get("contract"), Mapping):
        dump_json(root / "app_test_contract.json", report["contract"])
    if isinstance(report.get("execution_result"), Mapping):
        dump_json(root / "execution_result.json", report["execution_result"])
    if isinstance(report.get("business_offline_review"), Mapping):
        dump_json(root / "business_offline_review.json", report["business_offline_review"])
    if isinstance(report.get("direct_app_behavior_result"), Mapping):
        dump_json(root / "direct_app_behavior_result.json", report["direct_app_behavior_result"])
    if isinstance(report.get("app_behavior_result"), Mapping):
        dump_json(root / "app_behavior_result.json", report["app_behavior_result"])
    if isinstance(report.get("verification_offline_review"), Mapping):
        dump_json(root / "verification_offline_review.json", report["verification_offline_review"])
    if isinstance(report.get("attribution"), Mapping):
        dump_json(root / "attribution_result.json", report["attribution"])
    if isinstance(report.get("verification_runner_result"), Mapping):
        dump_json(root / "verification_runner_result.json", report["verification_runner_result"])
    if isinstance(report.get("verification_trace"), Mapping):
        dump_json(root / "verification_trace.json", report["verification_trace"])
    if isinstance(run_envelope, Mapping):
        write_run_envelope(root / RUN_ENVELOPE_FILE, run_envelope)
    dump_json(root / "report.json", report)
    timeline = "\n".join(
        _json_line(item)
        for item in report.get("step_results", [])
        if isinstance(item, Mapping)
    )
    (root / "execution_timeline.jsonl").write_text(
        timeline + ("\n" if timeline else ""),
        encoding="utf-8",
    )
    verification = report.get("verification_runner_result")
    verification_steps = (
        verification.get("step_results", [])
        if isinstance(verification, Mapping)
        else []
    )
    verification_timeline = "\n".join(
        _json_line(item)
        for item in verification_steps
        if isinstance(item, Mapping)
    )
    (root / "verification_timeline.jsonl").write_text(
        verification_timeline + ("\n" if verification_timeline else ""),
        encoding="utf-8",
    )
    (root / "report.md").write_text(render_markdown(report), encoding="utf-8")


def render_markdown(report: Mapping[str, Any]) -> str:
    behavior = report.get("app_behavior_result", {})
    execution = report.get("execution_result", {})
    lines = [
        f"# App Test Report: {report.get('test_case_id')}",
        "",
        f"- Overall result: `{report.get('overall_result')}`",
        f"- Attribution: `{report.get('attribution', {}).get('attribution')}`",
        f"- Execution: `{execution.get('status')}`",
        f"- App behavior: `{behavior.get('status')}`",
        f"- Completed steps: {report.get('completed_step_count')}/{report.get('step_count')}",
        f"- Verification runner: `{_verification_status(report)}`",
        "",
        "## Reason",
        "",
        str(report.get("attribution", {}).get("reason") or ""),
        "",
        "## Assertions",
        "",
    ]
    assertion_results = behavior.get("assertion_results", [])
    if not assertion_results:
        lines.append("No App behavior assertions were evaluated.")
    else:
        for item in assertion_results:
            if isinstance(item, Mapping):
                lines.append(
                    f"- `{item.get('assertion_id')}`: `{item.get('status')}` - {item.get('reason')}"
                )
    lines.append("")
    return "\n".join(lines)


def _verification_status(report: Mapping[str, Any]) -> str:
    result = report.get("verification_runner_result")
    if not isinstance(result, Mapping):
        return "NOT_RECORDED"
    return str(result.get("status") or "UNKNOWN")


def _json_line(value: Mapping[str, Any]) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, sort_keys=True)
