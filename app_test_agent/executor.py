"""Executor protocol and step-level evidence records."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol

from .schema import TestCaseSpec, TestStep


class StepStatus:
    STEP_COMPLETED = "STEP_COMPLETED"
    STEP_FAILED = "STEP_FAILED"
    ENV_BLOCKED = "ENV_BLOCKED"
    INCONCLUSIVE = "INCONCLUSIVE"
    UNSUPPORTED = "UNSUPPORTED"


@dataclass(frozen=True)
class EvidenceState:
    visible_texts: tuple[str, ...] = ()
    state_changed: bool | None = None
    success_signals: tuple[str, ...] = ()
    evidence_sufficient: bool = True
    notes: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "visible_texts": list(self.visible_texts),
            "state_changed": self.state_changed,
            "success_signals": list(self.success_signals),
            "evidence_sufficient": self.evidence_sufficient,
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class StepExecutionResult:
    step_id: str
    status: str
    action_type: str
    attempts: int = 1
    resolved_value: str | None = None
    target: Mapping[str, Any] = field(default_factory=dict)
    pre_frame: int | None = None
    post_frames: tuple[int, ...] = ()
    blocker: str | None = None
    error: str | None = None
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "status": self.status,
            "action_type": self.action_type,
            "attempts": self.attempts,
            "resolved_value": self.resolved_value,
            "target": dict(self.target),
            "pre_frame": self.pre_frame,
            "post_frames": list(self.post_frames),
            "blocker": self.blocker,
            "error": self.error,
            "evidence": dict(self.evidence),
        }


@dataclass(frozen=True)
class ExecutionRecord:
    test_case_id: str
    executor: str
    step_results: tuple[StepExecutionResult, ...]
    final_state: EvidenceState
    raw_trace_dir: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "test_case_id": self.test_case_id,
            "executor": self.executor,
            "step_results": [item.as_dict() for item in self.step_results],
            "final_state": self.final_state.as_dict(),
            "raw_trace_dir": self.raw_trace_dir,
            "metadata": dict(self.metadata),
        }


class StepExecutor(Protocol):
    name: str

    def execute(self, test_case: TestCaseSpec) -> ExecutionRecord: ...


def completed_step(step: TestStep, test_case: TestCaseSpec, index: int) -> StepExecutionResult:
    return StepExecutionResult(
        step_id=step.step_id,
        status=StepStatus.STEP_COMPLETED,
        action_type=step.action_type,
        attempts=1,
        resolved_value=step.resolved_value(test_case.test_data),
        target=step.target,
        pre_frame=index,
        post_frames=(index + 1,),
        evidence={"mock": True, "instruction": step.instruction},
    )
