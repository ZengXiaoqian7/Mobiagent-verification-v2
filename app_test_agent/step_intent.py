"""Runtime-only execution intent for business test steps.

The intent is compiled from the user-facing TestStep before dispatch.  It is
not part of the test-case contract that authors must fill in, but it gives the
runner and future Step Gate a shared, frozen interpretation of the step.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping

from .schema import TestCaseSpec, TestStep


STEP_EXECUTION_INTENT_SCHEMA_VERSION = "app-test-step-execution-intent-v1"


@dataclass(frozen=True)
class StepExecutionIntent:
    step_id: str
    original_instruction: str
    action_family: str
    step_mode: str
    semantic_target: str
    value: str | None = None
    value_ref: str | None = None
    allow_micro_actions: bool = False
    allowed_micro_action_families: tuple[str, ...] = ()
    allowed_recovery: tuple[str, ...] = ("WAIT",)
    max_attempts: int = 1
    compiled_before_execution: bool = True
    schema_version: str = STEP_EXECUTION_INTENT_SCHEMA_VERSION

    @property
    def sha256(self) -> str:
        payload = json.dumps(
            self.as_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "step_id": self.step_id,
            "original_instruction": self.original_instruction,
            "execution_intent": {
                "action_family": self.action_family,
                "step_mode": self.step_mode,
                "semantic_target": self.semantic_target,
                "value": self.value,
                "value_ref": self.value_ref,
                "allow_micro_actions": self.allow_micro_actions,
                "allowed_micro_action_families": list(self.allowed_micro_action_families),
                "allowed_recovery": list(self.allowed_recovery),
                "max_attempts": self.max_attempts,
            },
            "compiled_before_execution": self.compiled_before_execution,
        }


def compile_step_execution_intent(
    step: TestStep,
    test_case: TestCaseSpec,
) -> StepExecutionIntent:
    """Compile a frozen runtime intent from a user-facing step."""

    target = _semantic_target(step)
    value = step.resolved_value(test_case.test_data)
    recovery = ["WAIT"]
    if step.action_type in {"CLICK", "INPUT"}:
        recovery.append("BACK")
    if step.step_mode == "GOAL":
        recovery.extend(
            [
                "BACK",
                "OBSERVE",
                "LONG_PRESS",
                "PRESS_HOME",
                "INFO",
                "CALL_USER",
                "ABORT",
            ]
        )
    allowed_micro_actions = _allowed_goal_micro_actions(step) if step.step_mode == "GOAL" else ()
    return StepExecutionIntent(
        step_id=step.step_id,
        original_instruction=step.instruction,
        action_family=step.action_type,
        step_mode=step.step_mode,
        semantic_target=target,
        value=value,
        value_ref=step.value_ref,
        allow_micro_actions=step.step_mode == "GOAL",
        allowed_micro_action_families=allowed_micro_actions,
        allowed_recovery=tuple(dict.fromkeys(recovery)),
        max_attempts=step.max_retries + 1,
    )


def _allowed_goal_micro_actions(step: TestStep) -> tuple[str, ...]:
    default = (
        "click", "click_input", "input", "swipe", "wait", "press_back",
        "long_press", "press_home", "info", "call_user", "abort", "done",
    )
    target = step.target if isinstance(step.target, Mapping) else {}
    raw = target.get("allowed_micro_actions")
    if not isinstance(raw, (list, tuple)):
        return default
    aliases = {"back": "press_back", "home": "press_home", "scroll": "swipe"}
    allowed = []
    for item in raw:
        action = aliases.get(str(item).strip().lower(), str(item).strip().lower())
        if action not in default:
            raise ValueError(f"unsupported GOAL micro-action {item!r} for step {step.step_id}")
        allowed.append(action)
    if not allowed:
        raise ValueError(f"GOAL step {step.step_id} must allow at least one micro-action")
    return tuple(dict.fromkeys((*allowed, "done")))


def _semantic_target(step: TestStep) -> str:
    target = step.target if isinstance(step.target, Mapping) else {}
    labels = []
    for key in ("label", "text", "name", "role", "surface"):
        value = target.get(key)
        if isinstance(value, str) and value.strip():
            labels.append(value.strip())
    candidates = target.get("text_candidates")
    if isinstance(candidates, (list, tuple)):
        labels.extend(str(item).strip() for item in candidates if str(item).strip())
    if labels:
        return "; ".join(dict.fromkeys(labels))
    return step.instruction
