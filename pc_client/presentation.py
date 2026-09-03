"""Read-only presentation models for the desktop client."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Mapping

from app_test_agent.schema import load_test_case


@dataclass(frozen=True)
class TestCaseSummary:
    test_case_id: str
    app_name: str
    package: str | None
    feature: str
    risk_level: str
    step_count: int
    assertion_count: int
    verification_step_count: int
    verification_runner_policy: str
    preconditions: tuple[str, ...]
    steps: tuple[tuple[str, str, str], ...]
    assertions: tuple[tuple[str, str], ...]

    @property
    def mutates_device(self) -> bool:
        return self.risk_level not in {"LOW", "READ_ONLY", "NONE"}


@dataclass(frozen=True)
class ReportSummary:
    overall_result: str
    attribution: str
    execution_status: str
    app_behavior_status: str
    verification_status: str
    completed_steps: int
    step_count: int
    reason: str
    assertions: tuple[tuple[str, str, str], ...]
    artifacts: tuple[tuple[str, str], ...]


def load_test_case_summary(path: Path) -> TestCaseSummary:
    """Load the canonical test case and retain only operator-facing fields."""

    test_case = load_test_case(path)
    return TestCaseSummary(
        test_case_id=test_case.test_case_id,
        app_name=test_case.app_under_test.name,
        package=test_case.app_under_test.package,
        feature=test_case.feature,
        risk_level=test_case.risk_level,
        step_count=len(test_case.steps),
        assertion_count=len(test_case.expected_results),
        verification_step_count=len(test_case.verification_steps),
        verification_runner_policy=test_case.verification_runner_policy,
        preconditions=tuple(
            item.description or item.condition_id for item in test_case.preconditions
        ),
        steps=tuple(
            (item.step_id, item.action_type, item.instruction) for item in test_case.steps
        ),
        assertions=tuple(
            (item.assertion_id, item.type) for item in test_case.expected_results
        ),
    )


def load_report_summary(output_dir: Path) -> ReportSummary | None:
    """Return a compact report view when a complete report bundle exists."""

    report_path = output_dir / "report.json"
    if not report_path.is_file():
        return None
    payload = json.loads(report_path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, Mapping):
        raise ValueError("report.json root must be an object")
    execution = _mapping(payload.get("execution_result"))
    behavior = _mapping(payload.get("app_behavior_result"))
    attribution = _mapping(payload.get("attribution"))
    verification = _mapping(payload.get("verification_runner_result"))

    assertions: list[tuple[str, str, str]] = []
    raw_assertions = behavior.get("assertion_results")
    if isinstance(raw_assertions, list):
        for item in raw_assertions:
            if isinstance(item, Mapping):
                assertions.append(
                    (
                        str(item.get("assertion_id") or "-"),
                        str(item.get("status") or "UNKNOWN"),
                        str(item.get("reason") or ""),
                    )
                )

    artifacts: list[tuple[str, str]] = []
    raw_artifacts = _mapping(payload.get("artifacts"))
    for name, value in raw_artifacts.items():
        if isinstance(value, str) and value.strip():
            artifacts.append((str(name), value.strip()))

    return ReportSummary(
        overall_result=str(payload.get("overall_result") or "UNKNOWN"),
        attribution=str(attribution.get("attribution") or "NOT_RECORDED"),
        execution_status=str(execution.get("status") or "NOT_RECORDED"),
        app_behavior_status=str(behavior.get("status") or "NOT_RECORDED"),
        verification_status=str(verification.get("status") or "NOT_RUN"),
        completed_steps=_non_negative_int(payload.get("completed_step_count")),
        step_count=_non_negative_int(payload.get("step_count")),
        reason=str(attribution.get("reason") or ""),
        assertions=tuple(assertions),
        artifacts=tuple(artifacts),
    )


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _non_negative_int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


__all__ = [
    "ReportSummary",
    "TestCaseSummary",
    "load_report_summary",
    "load_test_case_summary",
]
