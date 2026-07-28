"""Attribution engine for App-test results."""

from __future__ import annotations

from .result_types import (
    AppBehaviorResult,
    AppBehaviorStatus,
    AttributionResult,
    ExecutionConformanceResult,
    ExecutionStatus,
    OverallResult,
)


def attribute_result(
    conformance: ExecutionConformanceResult, behavior: AppBehaviorResult
) -> AttributionResult:
    if (
        conformance.status == ExecutionStatus.UNSUPPORTED
        or behavior.status == AppBehaviorStatus.UNSUPPORTED
    ):
        return AttributionResult(
            overall_result=OverallResult.UNSUPPORTED,
            attribution="SYSTEM_UNSUPPORTED",
            reason=conformance.reason or behavior.reason,
        )
    if conformance.status == ExecutionStatus.ENV_BLOCKED:
        return AttributionResult(
            overall_result=OverallResult.ENV_BLOCKED,
            attribution="ENVIRONMENT",
            reason=conformance.reason,
        )
    if conformance.status == ExecutionStatus.INCONCLUSIVE:
        return AttributionResult(
            overall_result=OverallResult.INCONCLUSIVE,
            attribution="EVIDENCE",
            reason=conformance.reason,
        )
    if conformance.status != ExecutionStatus.COMPLETED:
        return AttributionResult(
            overall_result=OverallResult.TEST_EXECUTION_FAIL,
            attribution="EXECUTOR",
            reason=conformance.reason,
        )
    if behavior.status == AppBehaviorStatus.SATISFIED:
        return AttributionResult(
            overall_result=OverallResult.APP_PASS,
            attribution="APP_BEHAVIOR",
            reason=behavior.reason,
        )
    if behavior.status == AppBehaviorStatus.ENV_BLOCKED:
        return AttributionResult(
            overall_result=OverallResult.ENV_BLOCKED,
            attribution="ENVIRONMENT",
            reason=behavior.reason,
        )
    if behavior.status == AppBehaviorStatus.VIOLATED:
        return AttributionResult(
            overall_result=OverallResult.APP_FAIL,
            attribution="APP_DEFECT",
            reason=behavior.reason,
        )
    return AttributionResult(
        overall_result=OverallResult.INCONCLUSIVE,
        attribution="EVIDENCE",
        reason=behavior.reason,
    )
