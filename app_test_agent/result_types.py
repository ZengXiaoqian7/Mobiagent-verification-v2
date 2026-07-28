"""Shared result types for App-test verification."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


class ExecutionStatus:
    COMPLETED = "COMPLETED"
    TEST_EXECUTION_FAILED = "TEST_EXECUTION_FAILED"
    ENV_BLOCKED = "ENV_BLOCKED"
    INCONCLUSIVE = "INCONCLUSIVE"
    UNSUPPORTED = "UNSUPPORTED"


class AppBehaviorStatus:
    SATISFIED = "SATISFIED"
    VIOLATED = "VIOLATED"
    UNKNOWN_EVIDENCE = "UNKNOWN_EVIDENCE"
    NOT_EVALUATED = "NOT_EVALUATED"
    UNSUPPORTED = "UNSUPPORTED"
    ENV_BLOCKED = "ENV_BLOCKED"


class OverallResult:
    APP_PASS = "APP_PASS"
    APP_FAIL = "APP_FAIL"
    TEST_EXECUTION_FAIL = "TEST_EXECUTION_FAIL"
    ENV_BLOCKED = "ENV_BLOCKED"
    INCONCLUSIVE = "INCONCLUSIVE"
    UNSUPPORTED = "UNSUPPORTED"


@dataclass(frozen=True)
class AssertionResult:
    assertion_id: str
    status: str
    reason: str
    expected_value: str | None = None
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "assertion_id": self.assertion_id,
            "status": self.status,
            "reason": self.reason,
            "expected_value": self.expected_value,
            "evidence": dict(self.evidence),
        }


@dataclass(frozen=True)
class ExecutionConformanceResult:
    status: str
    failed_step: str | None = None
    environment_blocker: str | None = None
    evidence_sufficient: bool = True
    reason: str = ""
    contract_sha256: str | None = None
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "failed_step": self.failed_step,
            "environment_blocker": self.environment_blocker,
            "evidence_sufficient": self.evidence_sufficient,
            "reason": self.reason,
            "contract_sha256": self.contract_sha256,
            "evidence": dict(self.evidence),
        }


@dataclass(frozen=True)
class AppBehaviorResult:
    status: str
    assertion_results: tuple[AssertionResult, ...]
    reason: str
    contract_sha256: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "contract_sha256": self.contract_sha256,
            "assertion_results": [item.as_dict() for item in self.assertion_results],
        }


@dataclass(frozen=True)
class AttributionResult:
    overall_result: str
    attribution: str
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "overall_result": self.overall_result,
            "attribution": self.attribution,
            "reason": self.reason,
        }
