"""Recompute current Step Gate decisions from frozen raw trace facts."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path, PurePath
from typing import Any, Mapping

from .executor import ExecutionRecord, StepExecutionResult, StepStatus
from .mobiagent_executor import (
    _evaluate_input_effect,
    _evaluate_post_action_context,
    _parse_hierarchy_dump,
    _resolve_target,
)
from .schema import TestCaseSpec, TestStep
from .step_gate import (
    ActionConformance,
    StepGateDecision,
    evaluate_dispatch_failure_gate,
    evaluate_step_gate,
)


RAW_STEP_GATE_REPLAY_SCHEMA_VERSION = "app-test-raw-step-gate-replay-v1"


class RawStepGateReplayError(RuntimeError):
    pass


def recompute_step_gates_from_raw_trace(
    test_case: TestCaseSpec,
    execution: ExecutionRecord,
) -> ExecutionRecord:
    """Return an execution record whose step statuses use the current gate.

    Only raw action facts and frozen observation frames are inputs. Historical
    gate decisions remain in a compact comparison payload but are never passed
    into ``evaluate_step_gate``.
    """

    if not execution.raw_trace_dir:
        raise RawStepGateReplayError("execution record has no raw_trace_dir")
    trace_root = Path(execution.raw_trace_dir).resolve(strict=True)
    actions_path = trace_root / "actions.json"
    needs_actions = any(
        result.status not in {StepStatus.ENV_BLOCKED, StepStatus.UNSUPPORTED}
        for result in execution.step_results
    )
    if not actions_path.is_file() and not needs_actions:
        raw_actions: list[Any] = []
    else:
        try:
            actions_payload = json.loads(actions_path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RawStepGateReplayError(f"cannot load raw actions.json: {exc}") from exc
        raw_actions = actions_payload.get("actions") if isinstance(actions_payload, Mapping) else None
        if not isinstance(raw_actions, list):
            raise RawStepGateReplayError("raw actions.json does not contain an actions list")
    actions = tuple(dict(item) for item in raw_actions if isinstance(item, Mapping))
    frames = _runtime_frames(execution, trace_root)
    frames_by_id = {
        int(frame["frame_id"]): frame
        for frame in frames
        if isinstance(frame.get("frame_id"), int)
        and not isinstance(frame.get("frame_id"), bool)
    }
    action_by_index = {
        int(action["action_index"]): action
        for action in actions
        if isinstance(action.get("action_index"), int)
        and not isinstance(action.get("action_index"), bool)
    }
    replayed_steps: list[StepExecutionResult] = []
    comparisons: list[dict[str, Any]] = []
    step_by_id = {step.step_id: step for step in test_case.steps}
    index_by_id = {step.step_id: index for index, step in enumerate(test_case.steps)}

    for historical in execution.step_results:
        step = step_by_id.get(historical.step_id)
        if step is None:
            raise RawStepGateReplayError(
                f"historical step is not present in current test case: {historical.step_id}"
            )
        step_index = index_by_id[step.step_id]
        next_step = (
            test_case.steps[step_index + 1]
            if step_index + 1 < len(test_case.steps)
            else None
        )
        replayed, comparison = _recompute_one_step(
            test_case=test_case,
            step=step,
            next_step=next_step,
            historical=historical,
            frames_by_id=frames_by_id,
            action_by_index=action_by_index,
            actions=actions,
        )
        replayed_steps.append(replayed)
        comparisons.append(comparison)
        if replayed.status != StepStatus.STEP_COMPLETED:
            break

    replay_metadata = {
        "schema_version": RAW_STEP_GATE_REPLAY_SCHEMA_VERSION,
        "status": "COMPLETED",
        "actions_path": str(actions_path) if actions_path.is_file() else None,
        "actions_file_sha256": (
            hashlib.sha256(actions_path.read_bytes()).hexdigest()
            if actions_path.is_file()
            else None
        ),
        "historical_executor": execution.executor,
        "step_comparisons": comparisons,
    }
    return replace(
        execution,
        executor=f"{execution.executor}+raw_step_gate_replay",
        step_results=tuple(replayed_steps),
        metadata={
            **dict(execution.metadata),
            "frames": [dict(frame) for frame in frames],
            "raw_step_gate_replay": replay_metadata,
        },
    )


def _recompute_one_step(
    *,
    test_case: TestCaseSpec,
    step: TestStep,
    next_step: TestStep | None,
    historical: StepExecutionResult,
    frames_by_id: Mapping[int, Mapping[str, Any]],
    action_by_index: Mapping[int, Mapping[str, Any]],
    actions: tuple[Mapping[str, Any], ...],
) -> tuple[StepExecutionResult, dict[str, Any]]:
    historical_gate = _historical_gate_summary(historical)
    if historical.status in {StepStatus.ENV_BLOCKED, StepStatus.UNSUPPORTED}:
        comparison = {
            "step_id": step.step_id,
            "recomputed": False,
            "reason": "terminal environment/unsupported status has no dispatched action to re-gate",
            "historical": historical_gate,
            "current": historical_gate,
        }
        return replace(
            historical,
            evidence={
                **dict(historical.evidence),
                "raw_step_gate_replay": comparison,
            },
        ), comparison

    action = _action_for_step(historical, step, action_by_index, actions)
    pre_frame = frames_by_id.get(historical.pre_frame) if historical.pre_frame is not None else None
    post_frames = tuple(
        frames_by_id[frame_id]
        for frame_id in historical.post_frames
        if frame_id in frames_by_id
    )
    if action is None:
        error = historical.error or "raw action was not found before dispatch"
        gate = evaluate_dispatch_failure_gate(
            test_case=test_case,
            step=step,
            attempt=max(1, historical.attempts),
            pre_frame=pre_frame,
            error=error,
            max_retries=step.max_retries,
        )
    else:
        action = _current_action_facts(action)
        post_action_context = _evaluate_post_action_context(step, post_frames)
        if post_action_context is not None:
            action["post_action_context"] = post_action_context
        input_effect = _evaluate_input_effect(step, action, post_frames)
        if input_effect is not None:
            action["input_effect"] = input_effect
        next_target_status = _current_next_target_status(next_step, post_frames)
        gate = evaluate_step_gate(
            test_case=test_case,
            step=step,
            action_record=action,
            attempt=max(1, historical.attempts),
            pre_frame=pre_frame,
            post_frames=post_frames,
            next_step=next_step,
            next_step_target_evidence=next_target_status,
        )

    current_status = _status_from_gate(gate.gate_decision)
    current_summary = {
        "status": current_status,
        "gate_decision": gate.gate_decision,
        "target_evidence": gate.target_evidence,
        "action_conformance": gate.action_conformance,
        "progress_status": gate.progress_status,
        "environment_signal": gate.environment_signal,
        "reason": gate.reason,
    }
    comparison = {
        "step_id": step.step_id,
        "recomputed": True,
        "historical": historical_gate,
        "current": current_summary,
        "changed": historical_gate != current_summary,
    }
    action_payload = action if action is not None else {}
    return (
        StepExecutionResult(
            step_id=historical.step_id,
            status=current_status,
            action_type=historical.action_type,
            attempts=historical.attempts,
            resolved_value=historical.resolved_value,
            target=historical.target,
            pre_frame=historical.pre_frame,
            post_frames=historical.post_frames,
            blocker=gate.environment_signal,
            error=None if current_status == StepStatus.STEP_COMPLETED else gate.reason,
            evidence={
                **dict(action_payload),
                "gate_decision": gate.gate_decision,
                "target_evidence": gate.target_evidence,
                "action_conformance": gate.action_conformance,
                "progress_status": gate.progress_status,
                "environment_signal": gate.environment_signal,
                "step_gate": gate.as_dict(),
                "raw_step_gate_replay": comparison,
            },
        ),
        comparison,
    )


def _current_action_facts(action: Mapping[str, Any]) -> dict[str, Any]:
    derived_fields = {
        "action_conformance",
        "environment_signal",
        "gate_decision",
        "input_effect",
        "next_step_target_resolution",
        "post_action_context",
        "progress_status",
        "step_gate",
        "step_gate_attempts",
        "target_evidence",
    }
    return {key: value for key, value in action.items() if key not in derived_fields}


def _current_next_target_status(
    next_step: TestStep | None,
    post_frames: tuple[Mapping[str, Any], ...],
) -> str | None:
    if next_step is None:
        return None
    final_frame = post_frames[-1] if post_frames else None
    target = next_step.target if isinstance(next_step.target, Mapping) else {}
    resolved = _resolve_target(
        final_frame,
        target,
        wants_text_input=next_step.action_type == "INPUT",
    )
    return ActionConformance.CONFORMANT if resolved is not None else ActionConformance.UNKNOWN


def _action_for_step(
    historical: StepExecutionResult,
    step: TestStep,
    action_by_index: Mapping[int, Mapping[str, Any]],
    actions: tuple[Mapping[str, Any], ...],
) -> Mapping[str, Any] | None:
    raw_ids = historical.evidence.get("action_ids")
    if isinstance(raw_ids, list):
        candidates = [
            action_by_index[action_id]
            for action_id in raw_ids
            if isinstance(action_id, int) and action_id in action_by_index
        ]
        if candidates:
            return candidates[-1]
    index = historical.evidence.get("action_index")
    if isinstance(index, int) and index in action_by_index:
        return action_by_index[index]
    return next(
        (action for action in reversed(actions) if action.get("step_id") == step.step_id),
        None,
    )


def _runtime_frames(
    execution: ExecutionRecord,
    trace_root: Path,
) -> tuple[dict[str, Any], ...]:
    frames: list[dict[str, Any]] = []
    for raw in execution.metadata.get("frames", ()):
        if not isinstance(raw, Mapping):
            continue
        frame = dict(raw)
        hierarchy_ref = frame.get("hierarchy")
        hierarchy_path = _safe_trace_artifact(trace_root, hierarchy_ref)
        if hierarchy_path is not None and hierarchy_path.is_file():
            try:
                _text, _suffix, nodes = _parse_hierarchy_dump(
                    hierarchy_path.read_text(encoding="utf-8-sig")
                )
            except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
                nodes = []
            if nodes:
                frame["xml_nodes"] = nodes
        frames.append(frame)
    return tuple(frames)


def _safe_trace_artifact(trace_root: Path, value: Any) -> Path | None:
    if not isinstance(value, str) or not value.strip() or "://" in value:
        return None
    reference = PurePath(value)
    if reference.is_absolute() or ".." in reference.parts:
        return None
    candidate = (trace_root / Path(*reference.parts)).resolve()
    try:
        candidate.relative_to(trace_root)
    except ValueError:
        return None
    return candidate


def _status_from_gate(gate_decision: str) -> str:
    if gate_decision == StepGateDecision.CONTINUE:
        return StepStatus.STEP_COMPLETED
    if gate_decision == StepGateDecision.ENV_BLOCKED:
        return StepStatus.ENV_BLOCKED
    if gate_decision == StepGateDecision.INCONCLUSIVE:
        return StepStatus.INCONCLUSIVE
    if gate_decision == StepGateDecision.RETRY:
        # Replay cannot fabricate the follow-up attempt requested by current
        # policy.  The frozen trace is therefore insufficient, not proof that
        # execution ultimately failed.
        return StepStatus.INCONCLUSIVE
    return StepStatus.STEP_FAILED


def _historical_gate_summary(result: StepExecutionResult) -> dict[str, Any]:
    evidence = result.evidence if isinstance(result.evidence, Mapping) else {}
    return {
        "status": result.status,
        "gate_decision": evidence.get("gate_decision"),
        "target_evidence": evidence.get("target_evidence"),
        "action_conformance": evidence.get("action_conformance"),
        "progress_status": evidence.get("progress_status"),
        "environment_signal": evidence.get("environment_signal"),
        "reason": result.error,
    }


__all__ = [
    "RAW_STEP_GATE_REPLAY_SCHEMA_VERSION",
    "RawStepGateReplayError",
    "recompute_step_gates_from_raw_trace",
]
