"""Top-level App functional test orchestration."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4
from typing import Mapping

from .app_verifier import verify_app_behavior
from .attribution import attribute_result
from .contract import compile_app_test_contract
from .executor import StepExecutor
from .execution_verifier import verify_execution_conformance
from .manifest import build_manifest_from_execution_record, write_execution_manifest
from .offline_verifier import (
    OfflineTraceRole,
    OfflineTraceReview,
    review_app_test_trace,
)
from .reporting import build_report, write_report_bundle
from .result_types import AppBehaviorResult, AppBehaviorStatus, ExecutionStatus
from .run_envelope import build_run_envelope
from .schema import TestCaseSpec
from .verification_intent import compile_verification_intent
from .verification_runner import (
    ScriptedVerificationRunner,
    VerificationRunResult,
    VerificationRunStatus,
    VerificationRunner,
)


def run_app_test(
    test_case: TestCaseSpec,
    executor: StepExecutor,
    output_dir: Path | None = None,
    run_id: str | None = None,
    verification_runner: VerificationRunner | None = None,
) -> Mapping[str, object]:
    resolved_run_id = run_id or f"{test_case.test_case_id}-{uuid4().hex[:12]}"
    runtime_test_case = test_case.with_runtime_context(run_id=resolved_run_id)
    contract = compile_app_test_contract(runtime_test_case)
    execution = executor.execute(runtime_test_case)
    execution_manifest = build_manifest_from_execution_record(
        test_case=runtime_test_case,
        execution=execution,
        run_id=resolved_run_id,
        contract_sha256=contract.sha256,
    )
    conformance = verify_execution_conformance(runtime_test_case, execution, contract)
    business_offline_review = review_app_test_trace(
        test_case=runtime_test_case,
        execution=execution,
        contract=contract,
        role=OfflineTraceRole.BUSINESS_EXECUTION,
    )
    direct_behavior = verify_app_behavior(
        runtime_test_case,
        execution,
        conformance,
        contract,
        offline_review=business_offline_review,
    )
    verification_result, verification_offline_review, behavior = _maybe_run_verification(
        test_case=runtime_test_case,
        execution=execution,
        conformance=conformance,
        direct_behavior=direct_behavior,
        contract=contract,
        verification_runner=verification_runner,
    )
    attribution = attribute_result(conformance, behavior)
    run_envelope = build_run_envelope(
        run_id=resolved_run_id,
        test_case=runtime_test_case,
        contract=contract,
        execution=execution,
        execution_manifest=execution_manifest,
        conformance=conformance,
        direct_behavior=direct_behavior,
        behavior=behavior,
        verification_result=verification_result,
        business_offline_review=business_offline_review,
        verification_offline_review=verification_offline_review,
        attribution=attribution,
    )
    report = build_report(
        test_case=runtime_test_case,
        contract=contract,
        execution=execution,
        conformance=conformance,
        behavior=behavior,
        direct_behavior=direct_behavior,
        verification_result=verification_result,
        business_offline_review=business_offline_review,
        verification_offline_review=verification_offline_review,
        attribution=attribution,
        run_envelope=run_envelope,
    )
    if output_dir is not None:
        write_report_bundle(output_dir, report, runtime_test_case, run_envelope=run_envelope)
        write_execution_manifest(
            output_dir.resolve() / "test_execution_manifest.json",
            execution_manifest,
        )
    return report


def _maybe_run_verification(
    *,
    test_case: TestCaseSpec,
    execution,
    conformance,
    direct_behavior: AppBehaviorResult,
    contract,
    verification_runner: VerificationRunner | None,
) -> tuple[VerificationRunResult, OfflineTraceReview | None, AppBehaviorResult]:
    if conformance.status != ExecutionStatus.COMPLETED:
        return (
            VerificationRunResult(
                status=VerificationRunStatus.NOT_RUN,
                used_runner=False,
                reason="verification runner is not started when execution conformance fails",
                contract_sha256=contract.sha256,
            ),
            None,
            direct_behavior,
        )
    policy = test_case.verification_runner_policy
    if policy == "NEVER":
        return (
            VerificationRunResult(
                status=VerificationRunStatus.NOT_RUN,
                used_runner=False,
                reason="verification runner is disabled by test case policy",
                contract_sha256=contract.sha256,
            ),
            None,
            direct_behavior,
        )
    if (
        policy == "IF_DIRECT_UNKNOWN"
        and direct_behavior.status != AppBehaviorStatus.UNKNOWN_EVIDENCE
    ):
        return (
            VerificationRunResult(
                status=VerificationRunStatus.NOT_RUN,
                used_runner=False,
                reason="direct App evidence was decisive",
                contract_sha256=contract.sha256,
            ),
            None,
            direct_behavior,
        )
    runner = verification_runner
    verification_intent = compile_verification_intent(test_case)
    if runner is None and (test_case.verification_steps or verification_intent.has_observable_goal):
        runner = ScriptedVerificationRunner()
    if runner is None:
        reason = (
            "verification runner is required by test case policy but no runner "
            "or observable verification intent is available"
            if policy == "REQUIRED_FOR_RESULT"
            else "direct App evidence was insufficient and no verification runner was available"
        )
        return (
            VerificationRunResult(
                status=(
                    VerificationRunStatus.UNSUPPORTED
                    if policy == "REQUIRED_FOR_RESULT"
                    else VerificationRunStatus.NOT_RUN
                ),
                used_runner=False,
                reason=reason,
                contract_sha256=contract.sha256,
            ),
            None,
            (
                AppBehaviorResult(
                    status=AppBehaviorStatus.UNSUPPORTED,
                    assertion_results=direct_behavior.assertion_results,
                    reason=reason,
                    contract_sha256=contract.sha256,
                )
                if policy == "REQUIRED_FOR_RESULT"
                else direct_behavior
            ),
        )
    verification_result = runner.execute(
        test_case=test_case,
        business_execution=execution,
        contract=contract,
    )
    if (
        verification_result.status == VerificationRunStatus.COMPLETED
        and verification_result.reached_surface
        and verification_result.observation_sufficient
        and verification_result.observation_record is not None
    ):
        verification_offline_review = review_app_test_trace(
            test_case=test_case,
            execution=verification_result.observation_record,
            contract=contract,
            role=OfflineTraceRole.VERIFICATION_OBSERVATION,
            verification_context=verification_result.as_dict(),
        )
        behavior = verify_app_behavior(
            test_case,
            execution,
            conformance,
            contract,
            verification_execution=verification_result.observation_record,
            verification_context=verification_result.as_dict(),
            offline_review=verification_offline_review,
        )
        return verification_result, verification_offline_review, behavior
    if verification_result.status == VerificationRunStatus.ENV_BLOCKED:
        return (
            verification_result,
            None,
            AppBehaviorResult(
                status=AppBehaviorStatus.ENV_BLOCKED,
                assertion_results=direct_behavior.assertion_results,
                reason=verification_result.reason,
                contract_sha256=contract.sha256,
            ),
        )
    if verification_result.status == VerificationRunStatus.UNSUPPORTED:
        return (
            verification_result,
            None,
            AppBehaviorResult(
                status=AppBehaviorStatus.UNSUPPORTED,
                assertion_results=direct_behavior.assertion_results,
                reason=verification_result.reason,
                contract_sha256=contract.sha256,
            ),
        )
    return (
        verification_result,
        None,
        AppBehaviorResult(
            status=AppBehaviorStatus.UNKNOWN_EVIDENCE,
            assertion_results=direct_behavior.assertion_results,
            reason=(
                "direct App evidence was insufficient and verification runner "
                f"did not produce usable result evidence: {verification_result.reason}"
            ),
            contract_sha256=contract.sha256,
        ),
    )
