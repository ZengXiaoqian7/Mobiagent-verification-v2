"""Compatibility exports for the split App-test verifier modules."""

from .app_verifier import verify_app_behavior
from .attribution import attribute_result
from .execution_verifier import verify_execution_conformance
from .result_types import (
    AppBehaviorResult,
    AppBehaviorStatus,
    AssertionResult,
    AttributionResult,
    ExecutionConformanceResult,
    ExecutionStatus,
    OverallResult,
)

__all__ = [
    "AppBehaviorResult",
    "AppBehaviorStatus",
    "AssertionResult",
    "AttributionResult",
    "ExecutionConformanceResult",
    "ExecutionStatus",
    "OverallResult",
    "attribute_result",
    "verify_app_behavior",
    "verify_execution_conformance",
]
