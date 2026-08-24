"""Runtime Step Gate for App-test business steps.

The gate evaluates whether a just-dispatched step is trustworthy enough for
the orchestrator to continue.  It does not decide final App behavior.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .schema import TestCaseSpec, TestStep
from .step_intent import StepExecutionIntent, compile_step_execution_intent


class StepGateDecision:
    CONTINUE = "CONTINUE"
    RETRY = "RETRY"
    TEST_EXECUTION_FAIL = "TEST_EXECUTION_FAIL"
    ENV_BLOCKED = "ENV_BLOCKED"
    INCONCLUSIVE = "INCONCLUSIVE"


class ActionConformance:
    CONFORMANT = "CONFORMANT"
    NON_CONFORMANT = "NON_CONFORMANT"
    UNKNOWN = "UNKNOWN"


class ProgressStatus:
    GOAL_RESULT_CONFIRMED = "GOAL_RESULT_CONFIRMED"
    NEXT_STEP_TARGET_AVAILABLE = "NEXT_STEP_TARGET_AVAILABLE"
    INPUT_VALUE_CONFIRMED = "INPUT_VALUE_CONFIRMED"
    INPUT_DISPATCH_CONFIRMED = "INPUT_DISPATCH_CONFIRMED"
    SWIPE_DISPATCH_CONFIRMED = "SWIPE_DISPATCH_CONFIRMED"
    PAGE_CHANGED = "PAGE_CHANGED"
    ASYNC_PAGE_CHANGED = "ASYNC_PAGE_CHANGED"
    LOADING_CLEARED = "LOADING_CLEARED"
    ACTION_CONFORMANT_PROGRESS_UNKNOWN = "ACTION_CONFORMANT_PROGRESS_UNKNOWN"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class StepGateResult:
    step_id: str
    attempt: int
    pre_frame: int | None
    post_frames: tuple[int, ...]
    action_ids: tuple[int, ...] = ()
    target_evidence: str = ActionConformance.UNKNOWN
    action_conformance: str = ActionConformance.UNKNOWN
    progress_status: str = ProgressStatus.UNKNOWN
    environment_signal: str | None = None
    next_step_target_evidence: str = ActionConformance.UNKNOWN
    gate_decision: str = StepGateDecision.INCONCLUSIVE
    reason: str = ""
    runtime_intent: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "attempt": self.attempt,
            "pre_frame": self.pre_frame,
            "post_frames": list(self.post_frames),
            "action_ids": list(self.action_ids),
            "target_evidence": self.target_evidence,
            "action_conformance": self.action_conformance,
            "progress_status": self.progress_status,
            "environment_signal": self.environment_signal,
            "next_step_target_evidence": self.next_step_target_evidence,
            "gate_decision": self.gate_decision,
            "reason": self.reason,
            "runtime_intent": dict(self.runtime_intent),
        }


def evaluate_step_gate(
    *,
    test_case: TestCaseSpec,
    step: TestStep,
    action_record: Mapping[str, Any],
    attempt: int,
    pre_frame: Mapping[str, Any] | None,
    post_frames: tuple[Mapping[str, Any], ...],
    next_step: TestStep | None,
    next_step_target_evidence: str | None = None,
) -> StepGateResult:
    intent = compile_step_execution_intent(step, test_case)
    pre_frame_id = _frame_id(pre_frame)
    post_frame_ids = tuple(
        frame_id for frame in post_frames if (frame_id := _frame_id(frame)) is not None
    )
    action_ids = _action_ids(action_record)
    environment_signal = _environment_signal(post_frames)
    normalized_next_target = (
        next_step_target_evidence
        if next_step_target_evidence in {
            ActionConformance.CONFORMANT,
            ActionConformance.NON_CONFORMANT,
            ActionConformance.UNKNOWN,
        }
        else ActionConformance.UNKNOWN
    )
    if environment_signal is not None:
        return StepGateResult(
            step_id=step.step_id,
            attempt=attempt,
            pre_frame=pre_frame_id,
            post_frames=post_frame_ids,
            action_ids=action_ids,
            target_evidence=ActionConformance.UNKNOWN,
            action_conformance=ActionConformance.UNKNOWN,
            progress_status=ProgressStatus.UNKNOWN,
            environment_signal=environment_signal,
            next_step_target_evidence=normalized_next_target,
            gate_decision=StepGateDecision.ENV_BLOCKED,
            reason=f"environment blocker observed after step: {environment_signal}",
            runtime_intent=intent.as_dict(),
        )
    destination_context = _post_action_context_gate_result(action_record)
    if destination_context is not None:
        context_status, context_reason = destination_context
        if context_status == ActionConformance.NON_CONFORMANT:
            navigation_retry = (
                _is_recoverable_navigation_step(step, action_record)
                and attempt <= step.max_retries
            )
            return StepGateResult(
                step_id=step.step_id,
                attempt=attempt,
                pre_frame=pre_frame_id,
                post_frames=post_frame_ids,
                action_ids=action_ids,
                target_evidence=ActionConformance.NON_CONFORMANT,
                action_conformance=ActionConformance.NON_CONFORMANT,
                progress_status=ProgressStatus.UNKNOWN,
                next_step_target_evidence=normalized_next_target,
                gate_decision=(
                    StepGateDecision.RETRY
                    if navigation_retry
                    else StepGateDecision.TEST_EXECUTION_FAIL
                ),
                reason=(
                    "declared destination context was not reached; returning to the prior "
                    "surface before retrying the navigation step"
                    if navigation_retry
                    else context_reason
                ),
                runtime_intent=intent.as_dict(),
            )
        if context_status == ActionConformance.UNKNOWN:
            return StepGateResult(
                step_id=step.step_id,
                attempt=attempt,
                pre_frame=pre_frame_id,
                post_frames=post_frame_ids,
                action_ids=action_ids,
                target_evidence=ActionConformance.UNKNOWN,
                action_conformance=ActionConformance.UNKNOWN,
                progress_status=ProgressStatus.UNKNOWN,
                next_step_target_evidence=normalized_next_target,
                gate_decision=StepGateDecision.INCONCLUSIVE,
                reason=context_reason,
                runtime_intent=intent.as_dict(),
            )
    goal_micro_blocker = _goal_micro_blocking_result(
        step=step,
        action_record=action_record,
        attempt=attempt,
        pre_frame_id=pre_frame_id,
        post_frame_ids=post_frame_ids,
        action_ids=action_ids,
        intent=intent,
    )
    if goal_micro_blocker is not None:
        return goal_micro_blocker

    action_conformance, target_evidence, reason = _action_conformance(
        step=step,
        test_case=test_case,
        action_record=action_record,
        intent=intent,
    )
    input_effect = action_record.get("input_effect")
    if (
        step.action_type == "INPUT"
        and isinstance(input_effect, Mapping)
        and input_effect.get("status") == ActionConformance.NON_CONFORMANT
    ):
        return StepGateResult(
            step_id=step.step_id,
            attempt=attempt,
            pre_frame=pre_frame_id,
            post_frames=post_frame_ids,
            action_ids=action_ids,
            target_evidence=target_evidence,
            action_conformance=ActionConformance.NON_CONFORMANT,
            progress_status=ProgressStatus.UNKNOWN,
            next_step_target_evidence=normalized_next_target,
            gate_decision=StepGateDecision.TEST_EXECUTION_FAIL,
            reason="dispatched input value was not observed on the declared editable surface",
            runtime_intent=intent.as_dict(),
        )
    if action_conformance == ActionConformance.NON_CONFORMANT:
        overlay_retry = (
            target_evidence == "OVERLAY_BLOCKED" and attempt <= step.max_retries
        )
        return StepGateResult(
            step_id=step.step_id,
            attempt=attempt,
            pre_frame=pre_frame_id,
            post_frames=post_frame_ids,
            action_ids=action_ids,
            target_evidence=target_evidence,
            action_conformance=action_conformance,
            progress_status=ProgressStatus.UNKNOWN,
            next_step_target_evidence=normalized_next_target,
            gate_decision=(
                StepGateDecision.RETRY
                if overlay_retry
                else StepGateDecision.TEST_EXECUTION_FAIL
            ),
            reason=(
                "external overlay blocked the target; retrying after recovery"
                if overlay_retry
                else reason
            ),
            runtime_intent=intent.as_dict(),
        )

    progress_status = _progress_status(
        step=step,
        test_case=test_case,
        action_record=action_record,
        pre_frame=pre_frame,
        post_frames=post_frames,
        next_step=next_step,
        next_step_target_evidence=next_step_target_evidence,
    )
    if action_conformance == ActionConformance.UNKNOWN and not _strong_progress_for_unknown_target(progress_status):
        return StepGateResult(
            step_id=step.step_id,
            attempt=attempt,
            pre_frame=pre_frame_id,
            post_frames=post_frame_ids,
            action_ids=action_ids,
            target_evidence=target_evidence,
            action_conformance=action_conformance,
            progress_status=progress_status,
            next_step_target_evidence=normalized_next_target,
            gate_decision=StepGateDecision.INCONCLUSIVE,
            reason=(
                "target conformance is unknown and post-observation did not provide strong "
                "alternative progress evidence"
            ),
            runtime_intent=intent.as_dict(),
        )
    if step.step_mode == "GOAL" and progress_status != ProgressStatus.GOAL_RESULT_CONFIRMED:
        return StepGateResult(
            step_id=step.step_id,
            attempt=attempt,
            pre_frame=pre_frame_id,
            post_frames=post_frame_ids,
            action_ids=action_ids,
            target_evidence=target_evidence,
            action_conformance=action_conformance,
            progress_status=progress_status,
            next_step_target_evidence=normalized_next_target,
            gate_decision=StepGateDecision.INCONCLUSIVE,
            reason="goal step has not produced confirmed stage-result evidence",
            runtime_intent=intent.as_dict(),
        )
    if progress_status in {
        ProgressStatus.NEXT_STEP_TARGET_AVAILABLE,
        ProgressStatus.GOAL_RESULT_CONFIRMED,
        ProgressStatus.INPUT_VALUE_CONFIRMED,
        ProgressStatus.INPUT_DISPATCH_CONFIRMED,
        ProgressStatus.PAGE_CHANGED,
        ProgressStatus.ASYNC_PAGE_CHANGED,
        ProgressStatus.LOADING_CLEARED,
        ProgressStatus.ACTION_CONFORMANT_PROGRESS_UNKNOWN,
    }:
        return StepGateResult(
            step_id=step.step_id,
            attempt=attempt,
            pre_frame=pre_frame_id,
            post_frames=post_frame_ids,
            action_ids=action_ids,
            target_evidence=target_evidence,
            action_conformance=action_conformance,
            progress_status=progress_status,
            next_step_target_evidence=normalized_next_target,
            gate_decision=StepGateDecision.CONTINUE,
            reason="step action is conformant and process evidence allows continuing",
            runtime_intent=intent.as_dict(),
        )
    return StepGateResult(
        step_id=step.step_id,
        attempt=attempt,
        pre_frame=pre_frame_id,
        post_frames=post_frame_ids,
        action_ids=action_ids,
        target_evidence=target_evidence,
        action_conformance=action_conformance,
        progress_status=progress_status,
        next_step_target_evidence=normalized_next_target,
        gate_decision=StepGateDecision.INCONCLUSIVE,
        reason="step action was dispatched but post-observation did not provide safe progress evidence",
        runtime_intent=intent.as_dict(),
    )


def _post_action_context_gate_result(
    action_record: Mapping[str, Any],
) -> tuple[str, str] | None:
    """Require declared destination context before advancing to later steps."""

    context = action_record.get("post_action_context")
    if not isinstance(context, Mapping) or context.get("required") is not True:
        return None
    status = str(context.get("status") or ActionConformance.UNKNOWN)
    candidates = context.get("text_candidates")
    rendered = ", ".join(str(item) for item in candidates) if isinstance(candidates, list) else "declared context"
    if status == ActionConformance.NON_CONFORMANT:
        return (
            ActionConformance.NON_CONFORMANT,
            f"post-action destination context did not contain required target text: {rendered}",
        )
    if status != ActionConformance.CONFORMANT:
        return (
            ActionConformance.UNKNOWN,
            f"post-action destination context could not be confirmed for: {rendered}",
        )
    return None


def _is_recoverable_navigation_step(
    step: TestStep,
    action_record: Mapping[str, Any],
) -> bool:
    """Allow bounded Back-and-retry only for declared navigation transitions."""

    if step.action_type != "CLICK" or str(action_record.get("type") or "").lower() != "click":
        return False
    target = step.target if isinstance(step.target, Mapping) else {}
    role = str(target.get("role") or "").casefold()
    return role in {
        "conversation",
        "contact",
        "chat",
        "thread",
        "tab",
        "section",
        "navigation",
        "floating_action_button",
    }


def evaluate_dispatch_failure_gate(
    *,
    test_case: TestCaseSpec,
    step: TestStep,
    attempt: int,
    pre_frame: Mapping[str, Any] | None,
    error: str,
    max_retries: int,
) -> StepGateResult:
    intent = compile_step_execution_intent(step, test_case)
    retryable = _dispatch_failure_is_retryable(error)
    decision = (
        StepGateDecision.RETRY
        if retryable and attempt <= max_retries
        else StepGateDecision.TEST_EXECUTION_FAIL
    )
    return StepGateResult(
        step_id=step.step_id,
        attempt=attempt,
        pre_frame=_frame_id(pre_frame),
        post_frames=(),
        action_ids=(),
        target_evidence=ActionConformance.UNKNOWN,
        action_conformance=ActionConformance.UNKNOWN,
        progress_status=ProgressStatus.UNKNOWN,
        gate_decision=decision,
        reason=(
            "target was not located before dispatch; retrying within budget"
            if decision == StepGateDecision.RETRY
            else error
        ),
        runtime_intent=intent.as_dict(),
    )


def evaluate_micro_action_gate(
    *,
    test_case: TestCaseSpec,
    step: TestStep,
    micro_action_index: int,
    action_record: Mapping[str, Any],
    pre_frame: Mapping[str, Any] | None,
    post_frame: Mapping[str, Any] | None = None,
    post_frames: tuple[Mapping[str, Any], ...] | None = None,
) -> StepGateResult:
    intent = compile_step_execution_intent(step, test_case)
    pre_frame_id = _frame_id(pre_frame)
    post_frames = (
        post_frames
        if post_frames is not None
        else ((post_frame,) if isinstance(post_frame, Mapping) else ())
    )
    post_frame_ids = tuple(
        frame_id for frame in post_frames if (frame_id := _frame_id(frame)) is not None
    )
    action_ids = _action_ids(action_record)
    environment_signal = _environment_signal(post_frames)
    if environment_signal is not None:
        return StepGateResult(
            step_id=step.step_id,
            attempt=micro_action_index,
            pre_frame=pre_frame_id,
            post_frames=post_frame_ids,
            action_ids=action_ids,
            target_evidence=ActionConformance.UNKNOWN,
            action_conformance=ActionConformance.UNKNOWN,
            progress_status=ProgressStatus.UNKNOWN,
            environment_signal=environment_signal,
            gate_decision=StepGateDecision.ENV_BLOCKED,
            reason=f"environment blocker observed after micro-action: {environment_signal}",
            runtime_intent=intent.as_dict(),
        )
    action_conformance, target_evidence, reason = _action_conformance(
        step=step,
        test_case=test_case,
        action_record=action_record,
        intent=intent,
    )
    progress_status = _progress_status(
        step=step,
        test_case=test_case,
        action_record=action_record,
        pre_frame=pre_frame,
        post_frames=post_frames,
        next_step=None,
        allow_goal_stage_result=True,
    )
    if action_conformance == ActionConformance.NON_CONFORMANT:
        decision = StepGateDecision.TEST_EXECUTION_FAIL
    elif action_conformance == ActionConformance.UNKNOWN and not _micro_progress_allows_unknown_target(progress_status):
        decision = StepGateDecision.INCONCLUSIVE
    else:
        decision = StepGateDecision.CONTINUE
    return StepGateResult(
        step_id=step.step_id,
        attempt=micro_action_index,
        pre_frame=pre_frame_id,
        post_frames=post_frame_ids,
        action_ids=action_ids,
        target_evidence=target_evidence,
        action_conformance=action_conformance,
        progress_status=progress_status,
        gate_decision=decision,
        reason=(
            reason
            if decision != StepGateDecision.CONTINUE
            else "micro-action evidence is sufficient to continue the current goal"
        ),
        runtime_intent=intent.as_dict(),
    )


def _action_conformance(
    *,
    step: TestStep,
    test_case: TestCaseSpec,
    action_record: Mapping[str, Any],
    intent: StepExecutionIntent,
) -> tuple[str, str, str]:
    action_type = str(action_record.get("type") or "").lower()
    if action_type in {"info", "call_user", "abort"}:
        if action_record.get("runner_control_continue") is True:
            return (
                ActionConformance.CONFORMANT,
                ActionConformance.CONFORMANT,
                "Runner control action was handled and returned to the current step",
            )
        return (
            ActionConformance.UNKNOWN,
            ActionConformance.UNKNOWN,
            f"Runner control action {action_type!r} requires user intervention or terminated the step",
        )
    allowed = _allowed_runner_action_types(step.action_type)
    if action_type not in allowed:
        return (
            ActionConformance.NON_CONFORMANT,
            ActionConformance.UNKNOWN,
            f"runner action {action_type!r} does not match step action family {intent.action_family}",
        )
    if action_type == "gui_task":
        goal_action_conformance, goal_target_evidence, goal_reason = _goal_micro_conformance(action_record)
        if goal_action_conformance != ActionConformance.CONFORMANT:
            return goal_action_conformance, goal_target_evidence, goal_reason
    expected_value = step.resolved_value(test_case.test_data)
    if expected_value is not None and action_type in {"input", "click_input"}:
        actual = action_record.get("text")
        if action_type == "input":
            actual = action_record.get("text")
        if actual != expected_value:
            return (
                ActionConformance.NON_CONFORMANT,
                ActionConformance.CONFORMANT,
                "input value dispatched by runner does not match test data",
            )
    if action_type == "swipe":
        direction = str(action_record.get("direction") or "").casefold()
        if direction not in {"up", "down", "left", "right"}:
            return (
                ActionConformance.NON_CONFORMANT,
                ActionConformance.UNKNOWN,
                "swipe action is missing a valid direction",
            )
    target_evidence = _target_conformance(
        action_record,
        action_type,
        intent,
        test_case.app_under_test.package,
    )
    if target_evidence == "OVERLAY_BLOCKED":
        return (
            ActionConformance.NON_CONFORMANT,
            target_evidence,
            "runtime target is covered by an external window",
        )
    if target_evidence == ActionConformance.NON_CONFORMANT:
        return (
            ActionConformance.NON_CONFORMANT,
            target_evidence,
            "runtime target evidence proves the action hit the wrong target",
        )
    if target_evidence == ActionConformance.UNKNOWN and action_type in {"click", "click_input"}:
        return (
            ActionConformance.UNKNOWN,
            target_evidence,
            "runtime target evidence is missing or insufficient",
        )
    return (
        ActionConformance.CONFORMANT,
        target_evidence,
        "runner action matches the frozen step execution intent",
    )


def _goal_micro_conformance(action_record: Mapping[str, Any]) -> tuple[str, str, str]:
    micro_gates = action_record.get("micro_gates")
    if not isinstance(micro_gates, list) or not micro_gates:
        return (
            ActionConformance.UNKNOWN,
            ActionConformance.UNKNOWN,
            "goal action is missing independent micro-action gate evidence",
        )
    target_evidence = ActionConformance.CONFORMANT
    for gate in micro_gates:
        if not isinstance(gate, Mapping):
            continue
        gate_decision = gate.get("gate_decision")
        gate_target = gate.get("target_evidence")
        if gate_target == ActionConformance.NON_CONFORMANT:
            return (
                ActionConformance.NON_CONFORMANT,
                ActionConformance.NON_CONFORMANT,
                "a goal micro-action gate proved a wrong target",
            )
        if gate_decision == StepGateDecision.TEST_EXECUTION_FAIL:
            return (
                ActionConformance.NON_CONFORMANT,
                str(gate_target or ActionConformance.UNKNOWN),
                "a goal micro-action gate failed",
            )
        if gate_decision in {StepGateDecision.ENV_BLOCKED, StepGateDecision.INCONCLUSIVE}:
            return (
                ActionConformance.UNKNOWN,
                str(gate_target or ActionConformance.UNKNOWN),
                "a goal micro-action gate was not safe to continue",
            )
        if gate_target == ActionConformance.UNKNOWN:
            target_evidence = ActionConformance.UNKNOWN
    return (
        ActionConformance.CONFORMANT,
        target_evidence,
        "all goal micro-action gates were conformant",
    )


def _goal_micro_blocking_result(
    *,
    step: TestStep,
    action_record: Mapping[str, Any],
    attempt: int,
    pre_frame_id: int | None,
    post_frame_ids: tuple[int, ...],
    action_ids: tuple[int, ...],
    intent: StepExecutionIntent,
) -> StepGateResult | None:
    if step.step_mode != "GOAL" or str(action_record.get("type") or "").lower() != "gui_task":
        return None
    micro_gates = action_record.get("micro_gates")
    if not isinstance(micro_gates, list):
        return None
    for gate in micro_gates:
        if not isinstance(gate, Mapping):
            continue
        decision = gate.get("gate_decision")
        if decision == StepGateDecision.ENV_BLOCKED:
            return StepGateResult(
                step_id=step.step_id,
                attempt=attempt,
                pre_frame=pre_frame_id,
                post_frames=post_frame_ids,
                action_ids=action_ids,
                target_evidence=str(gate.get("target_evidence") or ActionConformance.UNKNOWN),
                action_conformance=str(gate.get("action_conformance") or ActionConformance.UNKNOWN),
                progress_status=str(gate.get("progress_status") or ProgressStatus.UNKNOWN),
                environment_signal=str(gate.get("environment_signal") or "goal_micro_action_env_blocked"),
                gate_decision=StepGateDecision.ENV_BLOCKED,
                reason="a goal micro-action was environment-blocked",
                runtime_intent=intent.as_dict(),
            )
        if decision == StepGateDecision.INCONCLUSIVE:
            return StepGateResult(
                step_id=step.step_id,
                attempt=attempt,
                pre_frame=pre_frame_id,
                post_frames=post_frame_ids,
                action_ids=action_ids,
                target_evidence=str(gate.get("target_evidence") or ActionConformance.UNKNOWN),
                action_conformance=str(gate.get("action_conformance") or ActionConformance.UNKNOWN),
                progress_status=str(gate.get("progress_status") or ProgressStatus.UNKNOWN),
                gate_decision=StepGateDecision.INCONCLUSIVE,
                reason="a goal micro-action was inconclusive and cannot be hidden by stage result evidence",
                runtime_intent=intent.as_dict(),
            )
    return None


def _target_conformance(
    action_record: Mapping[str, Any],
    action_type: str,
    intent: StepExecutionIntent,
    app_package: str | None,
) -> str:
    if action_type not in {"click", "click_input"}:
        return ActionConformance.CONFORMANT
    target_match = action_record.get("target_match")
    if target_match is True:
        return ActionConformance.CONFORMANT
    if target_match is False:
        return ActionConformance.NON_CONFORMANT
    xml_result = _xml_hit_test_conformance(
        action_record.get("xml_hit_test_result"),
        action_type=action_type,
        intent=intent,
        app_package=app_package,
    )
    if xml_result is not None:
        return xml_result
    if action_record.get("selector_clicked") is True:
        return ActionConformance.CONFORMANT
    visual = action_record.get("visual_target_check")
    if isinstance(visual, Mapping):
        if visual.get("matches_target") is True:
            return ActionConformance.CONFORMANT
        if visual.get("matches_target") is False:
            return ActionConformance.NON_CONFORMANT
    bounds_result = _click_bounds_conformance(action_record)
    if bounds_result == ActionConformance.NON_CONFORMANT:
        return bounds_result
    return ActionConformance.UNKNOWN


def _progress_status(
    *,
    step: TestStep,
    test_case: TestCaseSpec,
    action_record: Mapping[str, Any],
    pre_frame: Mapping[str, Any] | None,
    post_frames: tuple[Mapping[str, Any], ...],
    next_step: TestStep | None,
    next_step_target_evidence: str | None = None,
    allow_goal_stage_result: bool = True,
) -> str:
    expected_value = step.resolved_value(test_case.test_data)
    post_texts = _texts(post_frames)
    if allow_goal_stage_result and step.step_mode == "GOAL" and expected_value is not None:
        if any(expected_value in text for text in post_texts):
            return ProgressStatus.GOAL_RESULT_CONFIRMED
    if allow_goal_stage_result and step.step_mode == "GOAL" and _goal_stage_result_available(step, post_texts):
        return ProgressStatus.GOAL_RESULT_CONFIRMED
    action_type = str(action_record.get("type") or "").lower()
    if (step.action_type == "INPUT" or action_type in {"input", "click_input"}) and expected_value is not None:
        if any(expected_value in text for text in post_texts):
            return ProgressStatus.INPUT_VALUE_CONFIRMED
        # A device-level input call is not evidence that the editor had focus.
        # The executor attaches this result after observing the actual UI.  Do
        # not let a visible next-step button turn a failed input into progress.
        input_effect = action_record.get("input_effect")
        if isinstance(input_effect, Mapping) and input_effect.get("status") in {
            ActionConformance.NON_CONFORMANT,
            ActionConformance.UNKNOWN,
        }:
            return ProgressStatus.UNKNOWN
        if action_record.get("text") == expected_value:
            return ProgressStatus.INPUT_DISPATCH_CONFIRMED
    if action_type == "swipe":
        return ProgressStatus.SWIPE_DISPATCH_CONFIRMED
    if next_step is not None:
        if next_step_target_evidence == ActionConformance.CONFORMANT:
            return ProgressStatus.NEXT_STEP_TARGET_AVAILABLE
        if next_step_target_evidence is None and _next_step_target_available(next_step, post_texts):
            return ProgressStatus.NEXT_STEP_TARGET_AVAILABLE
    if _loading_cleared(pre_frame, post_frames):
        return ProgressStatus.LOADING_CLEARED
    if _async_page_changed(pre_frame, post_frames):
        return ProgressStatus.ASYNC_PAGE_CHANGED
    if _texts((pre_frame,)) != post_texts and post_texts:
        return ProgressStatus.PAGE_CHANGED
    if next_step is None:
        return ProgressStatus.ACTION_CONFORMANT_PROGRESS_UNKNOWN
    return ProgressStatus.UNKNOWN


def _strong_progress_for_unknown_target(progress_status: str) -> bool:
    return progress_status in {
        ProgressStatus.NEXT_STEP_TARGET_AVAILABLE,
        ProgressStatus.GOAL_RESULT_CONFIRMED,
        ProgressStatus.INPUT_VALUE_CONFIRMED,
        ProgressStatus.INPUT_DISPATCH_CONFIRMED,
        ProgressStatus.SWIPE_DISPATCH_CONFIRMED,
    }


def _goal_stage_result_available(step: TestStep, post_texts: tuple[str, ...]) -> bool:
    target = step.target if isinstance(step.target, Mapping) else {}
    candidates: list[str] = []
    raw = target.get("stage_result_text_candidates")
    if isinstance(raw, (list, tuple)):
        candidates.extend(str(item) for item in raw)
    for key in ("stage_result_text", "completion_text", "success_text"):
        value = target.get(key)
        if isinstance(value, str):
            candidates.append(value)
    candidates.extend(
        [
            "发布完成",
            "发布成功",
            "已发布",
            "posted",
            "published",
            "sent",
            "success",
        ]
    )
    cleaned = [item.strip().casefold() for item in candidates if item and item.strip()]
    folded_texts = [text.casefold() for text in post_texts]
    return any(candidate in text for candidate in cleaned for text in folded_texts)


def _micro_progress_allows_unknown_target(progress_status: str) -> bool:
    return progress_status in {
        ProgressStatus.INPUT_VALUE_CONFIRMED,
        ProgressStatus.INPUT_DISPATCH_CONFIRMED,
        ProgressStatus.GOAL_RESULT_CONFIRMED,
        ProgressStatus.SWIPE_DISPATCH_CONFIRMED,
    }


def _allowed_runner_action_types(action_family: str) -> set[str]:
    return {
        "OPEN_APP": {"open_app"},
        "CLICK": {"click"},
        "INPUT": {"click_input", "input"},
        "WAIT": {"wait"},
        "BACK": {"press_back"},
        "GUI_TASK": {
            "gui_task",
            "click",
            "click_input",
            "input",
            "swipe",
            "wait",
            "press_back",
            "long_press",
            "press_home",
            "info",
            "call_user",
            "abort",
        },
    }.get(action_family, set())


def _click_bounds_conformance(action_record: Mapping[str, Any]) -> str | None:
    point = action_record.get("click_point")
    bounds = action_record.get("bounds")
    if point is None or bounds is None:
        return None
    if not isinstance(point, (list, tuple)) or len(point) != 2:
        return ActionConformance.NON_CONFORMANT
    if not isinstance(bounds, (list, tuple)) or len(bounds) != 4:
        return ActionConformance.NON_CONFORMANT
    try:
        x, y = int(point[0]), int(point[1])
        x1, y1, x2, y2 = (int(item) for item in bounds)
    except (TypeError, ValueError):
        return ActionConformance.NON_CONFORMANT
    return (
        ActionConformance.CONFORMANT
        if x1 <= x <= x2 and y1 <= y <= y2
        else ActionConformance.NON_CONFORMANT
    )


def _xml_hit_test_conformance(
    value: Any,
    *,
    action_type: str,
    intent: StepExecutionIntent,
    app_package: str | None,
) -> str | None:
    if not isinstance(value, Mapping):
        return None
    if value.get("snapped") is True and value.get("selected_node"):
        nodes = [value.get("selected_node")]
    else:
        nodes = value.get("direct_hits")
        if not isinstance(nodes, list):
            nodes = []
    if _target_role_mismatch(nodes, action_type=action_type, intent=intent):
        return ActionConformance.NON_CONFORMANT
    if _hit_is_external_overlay(nodes, app_package):
        return "OVERLAY_BLOCKED"
    direct_hits = value.get("direct_hits")
    if isinstance(direct_hits, list) and direct_hits:
        return ActionConformance.CONFORMANT
    if value.get("snapped") is True and value.get("alignment_basis"):
        return ActionConformance.CONFORMANT
    if value.get("alignment_basis") == "direct_supported_hit":
        return ActionConformance.CONFORMANT
    if value.get("rejection_reason") in {"wrong_target", "outside_target"}:
        return ActionConformance.NON_CONFORMANT
    return None


def _hit_is_external_overlay(nodes: list[Any], app_package: str | None) -> bool:
    if not app_package:
        return False
    for node in nodes:
        if not isinstance(node, Mapping):
            continue
        attributes = node.get("attributes")
        attributes = attributes if isinstance(attributes, Mapping) else {}
        bundle = str(
            node.get("window_bundle_name")
            or attributes.get("bundleName")
            or attributes.get("bundle_name")
            or ""
        ).strip()
        if not bundle or bundle == app_package:
            continue
        signature = " ".join(
            str(node.get(key) or "")
            for key in ("tag", "text", "semantic_text", "window_page_path")
        ).casefold()
        if any(marker in signature for marker in ("keyboard", "inputmethod", "sceneboard")):
            continue
        return True
    return False


def _target_role_mismatch(
    nodes: list[Any],
    *,
    action_type: str,
    intent: StepExecutionIntent,
) -> bool:
    """Reject a proven input/button role collision before generic hit evidence.

    A coordinate can land inside a clickable parent while still targeting the
    wrong control.  This is especially common when a keyboard is visible and a
    send/submit button is adjacent to a rich text editor.  The rule is based on
    the frozen action family and runtime accessibility roles, so it applies to
    arbitrary Apps rather than named controls or coordinates.
    """
    if action_type not in {"click", "click_input"}:
        return False
    if not nodes:
        return False
    if intent.action_family == "CLICK" and _intent_requests_button(intent):
        has_input = any(_node_is_text_input(node) for node in nodes)
        has_button = any(_node_is_button(node) for node in nodes)
        return has_input and not has_button
    if intent.action_family == "INPUT" and action_type == "click_input":
        has_input = any(
            _node_is_text_input(node) or _node_is_input_container(node)
            for node in nodes
        )
        has_button = any(_node_is_button(node) for node in nodes)
        return has_button and not has_input
    return False


def _intent_requests_button(intent: StepExecutionIntent) -> bool:
    text = intent.semantic_target.casefold()
    return any(
        marker in text
        for marker in (
            "button",
            "按钮",
            "send",
            "发送",
            "submit",
            "提交",
            "confirm",
            "确认",
        )
    )


def _node_is_text_input(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    attributes = value.get("attributes")
    attributes = attributes if isinstance(attributes, Mapping) else {}
    signature = " ".join(
        str(value.get(key) or "")
        for key in ("tag", "text", "semantic_text", "semantic_context")
    ) + " " + " ".join(
        str(attributes.get(key) or "")
        for key in ("id", "key", "type", "class", "resource-id", "resourceId")
    )
    folded = signature.casefold()
    return any(
        marker in folded
        for marker in (
            "richeditor",
            "textinput",
            "edittext",
            "textfield",
            "inputfield",
            "text_input",
            "输入框",
            "输入栏",
            "文本框",
        )
    )


def _node_is_input_container(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    attributes = value.get("attributes")
    attributes = attributes if isinstance(attributes, Mapping) else {}
    signature = " ".join(
        str(value.get(key) or "")
        for key in ("tag", "text", "semantic_text", "semantic_context")
    ) + " " + " ".join(
        str(attributes.get(key) or "")
        for key in ("id", "key", "type", "class", "resource-id", "resourceId")
    )
    folded = signature.casefold()
    return any(
        marker in folded
        for marker in (
            "inputarea",
            "chatinput",
            "messageinput",
            "inputcontainer",
            "输入区域",
            "输入容器",
        )
    )


def _node_is_button(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    attributes = value.get("attributes")
    attributes = attributes if isinstance(attributes, Mapping) else {}
    signature = " ".join(
        str(value.get(key) or "")
        for key in ("tag", "text", "semantic_text", "semantic_context")
    ) + " " + " ".join(
        str(attributes.get(key) or "")
        for key in ("id", "key", "type", "class", "resource-id", "resourceId")
    )
    folded = signature.casefold()
    return any(
        marker in folded
        for marker in ("button", "发送", "send", "提交", "submit", "确认", "confirm")
    )


def _dispatch_failure_is_retryable(error: str) -> bool:
    text = str(error).casefold()
    if "done before dispatching required action" in text:
        return False
    return any(
        marker in text
        for marker in (
            "target was not found",
            "target was not located",
            "not visible",
            "temporarily unavailable",
            "locator returned no target",
        )
    )


def _next_step_target_available(next_step: TestStep, post_texts: tuple[str, ...]) -> bool:
    target = next_step.target
    candidates: list[str] = [next_step.instruction]
    if isinstance(target, Mapping):
        raw = target.get("text_candidates")
        if isinstance(raw, (list, tuple)):
            candidates.extend(str(item) for item in raw)
        for key in ("label", "text", "name", "role"):
            value = target.get(key)
            if isinstance(value, str):
                candidates.append(value)
    cleaned = [item.strip() for item in candidates if item and item.strip()]
    return any(candidate in text for candidate in cleaned for text in post_texts)


def _async_page_changed(
    pre_frame: Mapping[str, Any] | None,
    post_frames: tuple[Mapping[str, Any], ...],
) -> bool:
    if len(post_frames) < 2:
        return False
    baseline = _texts((pre_frame,))
    first = _texts((post_frames[0],))
    later = _texts(post_frames[1:])
    return bool(first == baseline and later and later != baseline)


def _loading_cleared(
    pre_frame: Mapping[str, Any] | None,
    post_frames: tuple[Mapping[str, Any], ...],
) -> bool:
    early_texts = _texts((pre_frame, post_frames[0] if post_frames else None))
    if not _has_loading_signal(early_texts):
        return False
    final_texts = _texts((post_frames[-1],)) if post_frames else ()
    return bool(final_texts and not _has_loading_signal(final_texts))


def _has_loading_signal(texts: tuple[str, ...]) -> bool:
    joined = "\n".join(texts).casefold()
    return any(
        term in joined
        for term in (
            "loading",
            "please wait",
            "submitting",
            "加载中",
            "正在加载",
            "提交中",
            "请稍候",
        )
    )


def _environment_signal(frames: tuple[Mapping[str, Any], ...]) -> str | None:
    texts = "\n".join(_texts(frames)).casefold()
    for term in (
        "crash",
        "isn't responding",
        "not responding",
        "anr",
        "login",
        "log in",
        "sign in",
        "permission",
        "network",
        "offline",
        "retry",
        "崩溃",
        "无响应",
        "请先登录",
        "登录",
        "权限",
        "网络",
        "无网络",
        "未连接",
        "重试",
    ):
        if term.casefold() in texts:
            return term
    return None


def _texts(frames: tuple[Mapping[str, Any] | None, ...]) -> tuple[str, ...]:
    values: list[str] = []
    for frame in frames:
        if not isinstance(frame, Mapping):
            continue
        for key in ("visible_texts", "ocr_texts"):
            raw = frame.get(key)
            if isinstance(raw, list):
                values.extend(str(item) for item in raw if str(item))
    return tuple(dict.fromkeys(values))


def _frame_id(frame: Mapping[str, Any] | None) -> int | None:
    if not isinstance(frame, Mapping):
        return None
    value = frame.get("frame_id")
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _action_ids(action_record: Mapping[str, Any]) -> tuple[int, ...]:
    raw = action_record.get("action_ids")
    if isinstance(raw, (list, tuple)):
        return tuple(int(item) for item in raw if isinstance(item, int))
    index = action_record.get("action_index")
    if isinstance(index, int) and not isinstance(index, bool):
        return (index,)
    return ()
