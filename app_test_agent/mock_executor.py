"""Deterministic mock executor for App-test control-flow validation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .executor import (
    EvidenceState,
    ExecutionRecord,
    StepExecutionResult,
    StepStatus,
    completed_step,
)
from .schema import TestCaseSpec


MOCK_SCENARIOS = (
    "pass",
    "app_fail",
    "execution_fail",
    "wrong_order",
    "input_mismatch",
    "env_blocked",
    "inconclusive",
    "forbidden_effect",
    "unsupported",
)


@dataclass(frozen=True)
class MockStepExecutor:
    scenario: str = "pass"
    name: str = "mock"

    def execute(self, test_case: TestCaseSpec) -> ExecutionRecord:
        scenario = self.scenario or str(test_case.metadata.get("mock_scenario") or "pass")
        scenario = scenario.lower().strip()
        if scenario == "execution_fail":
            return self._step_failure(test_case)
        if scenario == "env_blocked":
            return self._env_blocked(test_case)
        if scenario == "unsupported":
            return self._unsupported(test_case, test_case.steps[0].step_id, "mock unsupported scenario")
        if scenario == "wrong_order":
            return self._wrong_order(test_case)
        if scenario == "input_mismatch":
            return self._input_mismatch(test_case)

        step_results = tuple(
            completed_step(step, test_case, index)
            for index, step in enumerate(test_case.steps)
        )
        visible_texts: list[str] = []
        success_signals: list[str] = []
        state_changed: bool | None = True
        sufficient = True
        notes: list[str] = [f"mock_scenario={scenario}"]
        if scenario == "pass":
            visible_texts.append("Feed")
            visible_texts.extend(_expected_texts(test_case))
            success_signals.append("success")
        elif scenario == "app_fail":
            visible_texts.append("unchanged feed")
            notes.append("expected App effect intentionally omitted")
        elif scenario == "forbidden_effect":
            visible_texts.append("Feed")
            visible_texts.extend(_expected_texts(test_case))
            visible_texts.extend(_forbidden_texts(test_case))
            success_signals.append("success")
            notes.append("forbidden effect intentionally present")
        elif scenario == "inconclusive":
            sufficient = False
            state_changed = None
            notes.append("mock evidence intentionally insufficient")
        else:
            return self._unsupported(test_case, test_case.steps[0].step_id, f"unknown mock scenario: {scenario}")
        step_results = _with_mock_terminal_observation_window(test_case, step_results)
        return ExecutionRecord(
            test_case_id=test_case.test_case_id,
            executor=self.name,
            step_results=step_results,
            final_state=EvidenceState(
                visible_texts=tuple(visible_texts),
                state_changed=state_changed,
                success_signals=tuple(success_signals),
                evidence_sufficient=sufficient,
                notes=tuple(notes),
            ),
            metadata=_mock_metadata(test_case, step_results, scenario, visible_texts),
        )

    def _step_failure(self, test_case: TestCaseSpec) -> ExecutionRecord:
        results: list[StepExecutionResult] = []
        failed_index = min(1, len(test_case.steps) - 1)
        for index, step in enumerate(test_case.steps):
            if index < failed_index:
                results.append(completed_step(step, test_case, index))
                continue
            if index == failed_index:
                results.append(
                    StepExecutionResult(
                        step_id=step.step_id,
                        status=StepStatus.STEP_FAILED,
                        action_type=step.action_type,
                        attempts=step.max_retries + 1,
                        resolved_value=step.resolved_value(test_case.test_data),
                        target=step.target,
                        pre_frame=index,
                        post_frames=(index + 1,),
                        error="mock could not locate the requested target",
                    )
                )
                break
        return ExecutionRecord(
            test_case_id=test_case.test_case_id,
            executor=self.name,
            step_results=tuple(results),
            final_state=EvidenceState(evidence_sufficient=True, notes=("mock_scenario=execution_fail",)),
            metadata=_mock_metadata(test_case, tuple(results), "execution_fail", _expected_texts(test_case)),
        )

    def _wrong_order(self, test_case: TestCaseSpec) -> ExecutionRecord:
        results = [
            completed_step(step, test_case, index)
            for index, step in enumerate(test_case.steps)
        ]
        if len(results) > 1:
            results[0], results[1] = results[1], results[0]
        return ExecutionRecord(
            test_case_id=test_case.test_case_id,
            executor=self.name,
            step_results=tuple(results),
            final_state=EvidenceState(
                visible_texts=tuple(_expected_texts(test_case)),
                state_changed=True,
                success_signals=("success",),
                evidence_sufficient=True,
                notes=("mock_scenario=wrong_order",),
            ),
            metadata=_mock_metadata(test_case, tuple(results), "wrong_order", _expected_texts(test_case)),
        )

    def _input_mismatch(self, test_case: TestCaseSpec) -> ExecutionRecord:
        results: list[StepExecutionResult] = []
        mutated = False
        for index, step in enumerate(test_case.steps):
            result = completed_step(step, test_case, index)
            if not mutated and step.resolved_value(test_case.test_data) is not None:
                result = StepExecutionResult(
                    step_id=result.step_id,
                    status=result.status,
                    action_type=result.action_type,
                    attempts=result.attempts,
                    resolved_value="mutated input",
                    target=result.target,
                    pre_frame=result.pre_frame,
                    post_frames=result.post_frames,
                    blocker=result.blocker,
                    error=result.error,
                    evidence={**dict(result.evidence), "mock_mutation": True},
                )
                mutated = True
            results.append(result)
        return ExecutionRecord(
            test_case_id=test_case.test_case_id,
            executor=self.name,
            step_results=tuple(results),
            final_state=EvidenceState(
                visible_texts=tuple(_expected_texts(test_case)),
                state_changed=True,
                success_signals=("success",),
                evidence_sufficient=True,
                notes=("mock_scenario=input_mismatch",),
            ),
            metadata=_mock_metadata(test_case, tuple(results), "input_mismatch", _expected_texts(test_case)),
        )

    def _env_blocked(self, test_case: TestCaseSpec) -> ExecutionRecord:
        step = test_case.steps[0]
        return ExecutionRecord(
            test_case_id=test_case.test_case_id,
            executor=self.name,
            step_results=(
                StepExecutionResult(
                    step_id=step.step_id,
                    status=StepStatus.ENV_BLOCKED,
                    action_type=step.action_type,
                    attempts=1,
                    target=step.target,
                    blocker="login_or_permission_dialog",
                    error="mock environment blocker",
                ),
            ),
            final_state=EvidenceState(
                visible_texts=("Please log in",),
                evidence_sufficient=True,
                notes=("mock_scenario=env_blocked",),
            ),
            metadata={
                "mock_scenario": "env_blocked",
                "initial_visible_texts": ["Please log in"],
                "frame_visible_texts": {"0": ["Please log in"]},
                "frames": [
                    _mock_frame(0, ("Please log in",), relative_to_action_ms=0)
                ],
            },
        )

    def _unsupported(self, test_case: TestCaseSpec, step_id: str, reason: str) -> ExecutionRecord:
        first_step = test_case.steps[0]
        return ExecutionRecord(
            test_case_id=test_case.test_case_id,
            executor=self.name,
            step_results=(
                StepExecutionResult(
                    step_id=step_id,
                    status=StepStatus.UNSUPPORTED,
                    action_type=first_step.action_type,
                    attempts=0,
                    error=reason,
                ),
            ),
            final_state=EvidenceState(
                evidence_sufficient=False,
                notes=("mock_scenario=unsupported",),
            ),
            metadata={
                "mock_scenario": "unsupported",
                "reason": reason,
                "initial_visible_texts": ["Feed"],
                "frame_visible_texts": {"0": ["Feed"]},
                "frames": [_mock_frame(0, ("Feed",), relative_to_action_ms=0)],
            },
        )


def _expected_texts(test_case: TestCaseSpec) -> list[str]:
    values: list[str] = []
    for assertion in test_case.expected_results:
        if assertion.type == "TEXT_VISIBLE":
            value = assertion.resolved_value(test_case.test_data)
            if value is not None:
                values.append(value)
    return values


def _forbidden_texts(test_case: TestCaseSpec) -> list[str]:
    values: list[str] = []
    for effect in test_case.forbidden_effects:
        resolved = effect.resolved_values(test_case.test_data)
        if resolved:
            values.append(resolved[0])
    return values


def _mock_metadata(
    test_case: TestCaseSpec,
    step_results: tuple[StepExecutionResult, ...],
    scenario: str,
    final_visible_texts: list[str],
) -> dict[str, Any]:
    del test_case
    frame_texts: dict[str, list[str]] = {"0": ["Feed"]}
    frames = [_mock_frame(0, ("Feed",), relative_to_action_ms=0)]
    for result in step_results:
        if result.pre_frame is not None and str(result.pre_frame) not in frame_texts:
            frame_texts[str(result.pre_frame)] = ["Feed"]
            frames.append(_mock_frame(result.pre_frame, ("Feed",), relative_to_action_ms=0))
        for frame_id in result.post_frames:
            texts = tuple(final_visible_texts)
            frame_texts[str(frame_id)] = list(texts)
            raw_offsets = result.evidence.get("mock_post_frame_offsets")
            relative = (
                raw_offsets.get(str(frame_id), 500)
                if isinstance(raw_offsets, dict)
                else 500
            )
            frames.append(
                _mock_frame(
                    frame_id,
                    texts,
                    relative_to_action_ms=int(relative),
                )
            )
    unique_frames = {
        int(frame["frame_id"]): frame for frame in frames if isinstance(frame.get("frame_id"), int)
    }
    return {
        "mock_scenario": scenario,
        "initial_visible_texts": ["Feed"],
        "frame_visible_texts": frame_texts,
        "frames": [unique_frames[key] for key in sorted(unique_frames)],
    }


def _with_mock_terminal_observation_window(
    test_case: TestCaseSpec,
    step_results: tuple[StepExecutionResult, ...],
) -> tuple[StepExecutionResult, ...]:
    """Make ordinary mock absence scenarios represent a complete window."""

    policy = test_case.observation_policy
    max_wait = policy.get("max_wait_ms")
    if not isinstance(max_wait, int) or isinstance(max_wait, bool) or max_wait < 500:
        # The short-window regression deliberately leaves the fixture's 500ms
        # frame outside the selected policy window.
        return step_results
    raw_delays = policy.get("delays_ms", ())
    delays = [
        value
        for value in raw_delays
        if isinstance(value, int)
        and not isinstance(value, bool)
        and 0 <= value <= max_wait
    ] if isinstance(raw_delays, (list, tuple)) else []
    offsets = ([0] if policy.get("immediate") is True else []) + sorted(set(delays))
    if not offsets or max(offsets) <= 500:
        return step_results
    selected_step_ids = {
        assertion.after_step
        for assertion in test_case.expected_results
        if assertion.type in {"TEXT_VISIBLE", "TEXT_ABSENT"}
        and assertion.after_step is not None
    }
    if test_case.forbidden_effects and step_results:
        selected_step_ids.add(step_results[-1].step_id)
    next_frame_id = (
        max(
            frame_id
            for result in step_results
            for frame_id in (
                *((result.pre_frame,) if result.pre_frame is not None else ()),
                *result.post_frames,
            )
        )
        + 1
    )
    expanded: list[StepExecutionResult] = []
    for result in step_results:
        if result.step_id not in selected_step_ids or not result.post_frames:
            expanded.append(result)
            continue
        frame_ids = [result.post_frames[0]]
        while len(frame_ids) < len(offsets):
            frame_ids.append(next_frame_id)
            next_frame_id += 1
        expanded.append(
            StepExecutionResult(
                step_id=result.step_id,
                status=result.status,
                action_type=result.action_type,
                attempts=result.attempts,
                resolved_value=result.resolved_value,
                target=result.target,
                pre_frame=result.pre_frame,
                post_frames=tuple(frame_ids),
                blocker=result.blocker,
                error=result.error,
                evidence={
                    **dict(result.evidence),
                    "mock_post_frame_offsets": {
                        str(frame_id): offset
                        for frame_id, offset in zip(frame_ids, offsets)
                    },
                },
            )
        )
    return tuple(expanded)


def _mock_frame(
    frame_id: int,
    visible_texts: tuple[str, ...],
    *,
    relative_to_action_ms: int,
) -> dict[str, Any]:
    import hashlib

    seed = f"mock:{frame_id}:{'|'.join(visible_texts)}".encode("utf-8")
    digest = hashlib.sha256(seed).hexdigest()
    return {
        "frame_id": frame_id,
        "timestamp_ms": frame_id * 1000,
        "relative_to_action_ms": relative_to_action_ms,
        "screenshot": f"mock://frame/{frame_id}.png",
        "screenshot_sha256": digest,
        "hierarchy": f"mock://frame/{frame_id}.xml",
        "hierarchy_sha256": hashlib.sha256((digest + ":xml").encode("utf-8")).hexdigest(),
        "stability": "STABLE",
        "visible_texts": list(visible_texts),
    }


@dataclass(frozen=True)
class ScriptedStepExecutor:
    """Executor test double that returns a pre-built execution record."""

    record: ExecutionRecord
    name: str = "scripted"

    def execute(self, test_case: TestCaseSpec) -> ExecutionRecord:
        return ExecutionRecord(
            test_case_id=test_case.test_case_id,
            executor=self.name,
            step_results=self.record.step_results,
            final_state=self.record.final_state,
            raw_trace_dir=self.record.raw_trace_dir,
            metadata={**dict(self.record.metadata), "scripted": True},
        )
