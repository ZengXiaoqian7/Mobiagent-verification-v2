"""Structured App functional test-case protocol.

This module deliberately stays dependency-free.  The project already has a
large verifier stack, but the App-test layer needs a small stable protocol that
can be used by a mock executor, by the existing MobiAgent runner adapter, and
by future trace-based verifiers.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping


TEST_CASE_SCHEMA_VERSION = "app-test-case-v1"
SUPPORTED_ACTION_TYPES = frozenset({"OPEN_APP", "CLICK", "INPUT", "WAIT", "BACK", "GUI_TASK"})
SUPPORTED_STEP_MODES = frozenset({"ATOMIC", "GOAL"})
GENERATED_RUNTIME_DATA_PREFIX = "__generated_"
DEFAULT_GENERATED_TEXT_REF = "__generated_post_content"
READ_ONLY_VERIFICATION_ACTION_TYPES = frozenset(
    {"OPEN_APP", "NAVIGATE", "WAIT", "REFRESH", "SCROLL", "OBSERVE", "BACK"}
)
SUPPORTED_ASSERTION_TYPES = frozenset(
    {"TEXT_VISIBLE", "TEXT_ABSENT", "STATE_CHANGED", "SUCCESS_SIGNAL"}
)
DEFAULT_OBSERVATION_POLICY = {
    "immediate": True,
    "delays_ms": [500, 1000],
    "max_wait_ms": 5000,
    "stop_when_stable": True,
    "adaptive_capture": False,
}
DEFAULT_VERIFICATION_POLICY = {
    "max_steps": 5,
    "timeout_seconds": 30.0,
    "max_retries": 1,
}
SUPPORTED_VERIFICATION_RUNNER_POLICIES = frozenset(
    {"NEVER", "IF_DIRECT_UNKNOWN", "REQUIRED_FOR_RESULT"}
)
DEFAULT_VERIFICATION_RUNNER_POLICY = "IF_DIRECT_UNKNOWN"
SUPPORTED_FAILURE_CLASSES = frozenset(
    {"ENV_BLOCKED", "TEST_EXECUTION_FAIL", "INCONCLUSIVE", "UNSUPPORTED"}
)


class TestCaseError(ValueError):
    """Raised when an App test case is not accepted."""


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _expect_str(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TestCaseError(f"{context} must be a non-empty string")
    return value.strip()


def _expect_list(value: Any, context: str) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise TestCaseError(f"{context} must be a list")
    return value


def _expect_mapping(value: Any, context: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise TestCaseError(f"{context} must be an object")
    return dict(value)


def _string_list(value: Any, context: str) -> tuple[str, ...]:
    items = _expect_list(value, context)
    result = tuple(_expect_str(item, f"{context}[]") for item in items)
    if len(result) != len(set(result)):
        raise TestCaseError(f"{context} must not contain duplicates")
    return result


def _optional_string_list(value: Any, context: str) -> tuple[str, ...]:
    if value is None:
        return ()
    return _string_list(value, context)


def _expect_supported(value: str, supported: frozenset[str], context: str) -> str:
    normalized = _expect_str(value, context).upper()
    if normalized not in supported:
        raise TestCaseError(
            f"{context} is unsupported: {normalized}; supported={sorted(supported)}"
        )
    return normalized


def _replace_runtime_templates(value: Any, *, run_id: str) -> Any:
    if isinstance(value, str):
        return value.replace("${run_id}", run_id)
    if isinstance(value, list):
        return [_replace_runtime_templates(item, run_id=run_id) for item in value]
    if isinstance(value, tuple):
        return tuple(_replace_runtime_templates(item, run_id=run_id) for item in value)
    if isinstance(value, Mapping):
        return {
            str(key): _replace_runtime_templates(child, run_id=run_id)
            for key, child in value.items()
        }
    return value


def _is_generated_data_ref(value: str | None) -> bool:
    return bool(value and value.startswith(GENERATED_RUNTIME_DATA_PREFIX))


def _default_generated_value_ref(description: str | None = None) -> str:
    text = str(description or "").casefold()
    if any(term in text for term in ("comment", "评论")):
        return "__generated_comment_text"
    if any(term in text for term in ("message", "chat", "私信", "消息")):
        return "__generated_message_text"
    return DEFAULT_GENERATED_TEXT_REF


def _generate_runtime_value(ref: str, *, run_id: str) -> str:
    suffix = ref.removeprefix(GENERATED_RUNTIME_DATA_PREFIX).strip("_") or "text"
    safe_run_id = re.sub(r"[^A-Za-z0-9_-]+", "_", run_id).strip("_") or "run"
    return f"app_test_{safe_run_id}_{suffix}"


def _instruction_is_goal(instruction: Any) -> bool:
    text = str(instruction or "").casefold()
    if any(
        term in text
        for term in (
            "click",
            "tap",
            "input",
            "enter",
            "type",
            "fill",
            "click_input",
            "点击",
            "点按",
            "输入",
            "填写",
            "返回",
            "后退",
            "等待",
            "打开",
            "选择",
        )
    ):
        return False
    return any(
        term in text
        for term in (
            "complete",
            "create",
            "publish",
            "post",
            "find",
            "finish",
            "完成",
            "创建",
            "发布",
            "发帖",
            "发文",
            "找到",
            "确保",
            "一次",
            "流程",
        )
    )


def _goal_needs_generated_text(instruction: Any) -> bool:
    text = str(instruction or "").casefold()
    return any(
        term in text
        for term in (
            "text post",
            "post",
            "note",
            "content",
            "文字",
            "内容",
            "发帖",
            "发文",
            "笔记",
            "帖子",
            "发布",
        )
    )


def _infer_action_type(instruction: Any) -> str:
    text = str(instruction or "").casefold()
    if _instruction_is_goal(instruction):
        return "GUI_TASK"
    if any(term in text for term in ("input", "enter", "type", "填写", "输入", "录入")):
        return "INPUT"
    if any(term in text for term in ("wait", "等待", "稍等")):
        return "WAIT"
    if any(term in text for term in ("go back", "back", "返回", "后退")):
        return "BACK"
    if any(term in text for term in ("launch app", "open app", "启动应用", "打开应用")):
        return "OPEN_APP"
    return "CLICK"


def _infer_step_mode(instruction: Any, action_type: str) -> str:
    if action_type == "GUI_TASK" or _instruction_is_goal(instruction):
        return "GOAL"
    return "ATOMIC"


def _infer_value_ref_for_input(
    test_data: Mapping[str, Any],
    context: str,
) -> str | None:
    del context
    candidates = [
        key
        for key, value in test_data.items()
        if isinstance(key, str) and key.strip() and isinstance(value, str) and value.strip()
    ]
    if len(candidates) == 1:
        return candidates[0]
    preferred = [
        key
        for key in candidates
        if re.search(r"(content|text|body|message|内容|文本|正文)", key, flags=re.I)
    ]
    if len(preferred) == 1:
        return preferred[0]
    return None


def _infer_value_ref_for_expected_text(
    test_data: Mapping[str, Any],
    description: str,
) -> str | None:
    for key, value in test_data.items():
        if isinstance(value, str) and value and value in description:
            return str(key)
    if any(term in description for term in ("本轮", "测试内容", "test content", "configured")):
        return _infer_value_ref_for_input(test_data, "expected_results[]") or _default_generated_value_ref(description)
    if any(term in description for term in ("刚才", "本次", "发布内容", "生成内容", "自动生成")):
        return _infer_value_ref_for_input(test_data, "expected_results[]") or _default_generated_value_ref(description)
    return None


def _observation_policy(value: Any) -> dict[str, Any]:
    policy = {**DEFAULT_OBSERVATION_POLICY, **_expect_mapping(value, "observation_policy")}
    immediate = policy.get("immediate")
    if not isinstance(immediate, bool):
        raise TestCaseError("observation_policy.immediate must be boolean")
    delays = policy.get("delays_ms")
    if not isinstance(delays, list) or any(
        not isinstance(item, int) or isinstance(item, bool) or item < 0
        for item in delays
    ):
        raise TestCaseError("observation_policy.delays_ms must be a list of non-negative integers")
    max_wait = policy.get("max_wait_ms")
    if not isinstance(max_wait, int) or isinstance(max_wait, bool) or max_wait < 0:
        raise TestCaseError("observation_policy.max_wait_ms must be a non-negative integer")
    stop_when_stable = policy.get("stop_when_stable")
    if not isinstance(stop_when_stable, bool):
        raise TestCaseError("observation_policy.stop_when_stable must be boolean")
    adaptive_capture = policy.get("adaptive_capture")
    if not isinstance(adaptive_capture, bool):
        raise TestCaseError("observation_policy.adaptive_capture must be boolean")
    return {
        "immediate": immediate,
        "delays_ms": list(delays),
        "max_wait_ms": max_wait,
        "stop_when_stable": stop_when_stable,
        "adaptive_capture": adaptive_capture,
    }


def _verification_policy(value: Any) -> dict[str, Any]:
    policy = {**DEFAULT_VERIFICATION_POLICY, **_expect_mapping(value, "verification_policy")}
    max_steps = policy.get("max_steps")
    if not isinstance(max_steps, int) or isinstance(max_steps, bool) or max_steps < 0:
        raise TestCaseError("verification_policy.max_steps must be a non-negative integer")
    timeout = policy.get("timeout_seconds")
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout < 0:
        raise TestCaseError("verification_policy.timeout_seconds must be non-negative")
    retries = policy.get("max_retries")
    if not isinstance(retries, int) or isinstance(retries, bool) or retries < 0:
        raise TestCaseError("verification_policy.max_retries must be a non-negative integer")
    return {
        "max_steps": max_steps,
        "timeout_seconds": float(timeout),
        "max_retries": retries,
    }


@dataclass(frozen=True)
class AppUnderTest:
    name: str
    package: str | None = None
    version: str | None = None

    @classmethod
    def from_json(cls, value: Any) -> "AppUnderTest":
        if isinstance(value, str):
            return cls(name=_expect_str(value, "app_under_test"))
        data = _expect_mapping(value, "app_under_test")
        name = _expect_str(data.get("name"), "app_under_test.name")
        package = data.get("package")
        version = data.get("version")
        return cls(
            name=name,
            package=_expect_str(package, "app_under_test.package")
            if package is not None
            else None,
            version=_expect_str(version, "app_under_test.version")
            if version not in (None, "")
            else None,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "package": self.package,
            "version": self.version,
        }


@dataclass(frozen=True)
class TestStep:
    step_id: str
    instruction: str
    action_type: str = "CLICK"
    step_mode: str = "ATOMIC"
    target: Mapping[str, Any] = field(default_factory=dict)
    value: str | None = None
    value_ref: str | None = None
    timeout_seconds: float = 10.0
    max_retries: int = 1

    @classmethod
    def from_json(
        cls,
        value: Mapping[str, Any] | str,
        index: int,
        *,
        test_data: Mapping[str, Any] | None = None,
    ) -> "TestStep":
        if isinstance(value, str):
            return cls.from_instruction(value, index, test_data=test_data or {})
        data = _expect_mapping(value, f"steps[{index}]")
        explicit_action_type = data.get("action_type")
        step_id = _expect_str(data.get("step_id"), f"steps[{index}].step_id")
        action_type = _expect_supported(
            str(explicit_action_type or _infer_action_type(data.get("instruction"))),
            SUPPORTED_ACTION_TYPES,
            f"steps[{index}].action_type",
        )
        step_mode = _expect_supported(
            str(data.get("step_mode") or _infer_step_mode(data.get("instruction"), action_type)),
            SUPPORTED_STEP_MODES,
            f"steps[{index}].step_mode",
        )
        if action_type == "GUI_TASK" and step_mode != "GOAL":
            raise TestCaseError(f"steps[{index}] action_type GUI_TASK requires step_mode GOAL")
        timeout = data.get("timeout_seconds", 10.0)
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
            raise TestCaseError(f"steps[{index}].timeout_seconds must be positive")
        retries = data.get("max_retries", 1)
        if not isinstance(retries, int) or isinstance(retries, bool) or retries < 0:
            raise TestCaseError(f"steps[{index}].max_retries must be non-negative")
        value_literal = data.get("value")
        # Harmony canonicalizes non-input steps with an empty value field.
        # Treat that representation as the protocol's absent optional value.
        if value_literal == "":
            value_literal = None
        value_ref = data.get("value_ref")
        if value_literal is not None and value_ref is not None:
            raise TestCaseError(f"steps[{index}] cannot set both value and value_ref")
        if action_type == "INPUT" and value_literal is None and value_ref is None and explicit_action_type is None:
            inferred_ref = _infer_value_ref_for_input(test_data or {}, f"steps[{index}]")
            value_ref = inferred_ref or _default_generated_value_ref(data.get("instruction"))
        elif action_type == "INPUT" and value_literal is None and value_ref is None:
            value_ref = _default_generated_value_ref(data.get("instruction"))
        elif (
            action_type == "GUI_TASK"
            and value_literal is None
            and value_ref is None
            and _goal_needs_generated_text(data.get("instruction"))
        ):
            value_ref = _infer_value_ref_for_input(test_data or {}, f"steps[{index}]") or _default_generated_value_ref(
                data.get("instruction")
            )
        return cls(
            step_id=step_id,
            instruction=_expect_str(data.get("instruction"), f"steps[{index}].instruction"),
            action_type=action_type,
            step_mode=step_mode,
            target=_expect_mapping(data.get("target"), f"steps[{index}].target"),
            value=_expect_str(value_literal, f"steps[{index}].value")
            if value_literal is not None
            else None,
            value_ref=_expect_str(value_ref, f"steps[{index}].value_ref")
            if value_ref is not None
            else None,
            timeout_seconds=float(timeout),
            max_retries=retries,
        )

    @classmethod
    def from_instruction(
        cls,
        instruction: str,
        index: int,
        *,
        test_data: Mapping[str, Any],
    ) -> "TestStep":
        text = _expect_str(instruction, f"steps[{index}]")
        action_type = _infer_action_type(text)
        step_mode = _infer_step_mode(text, action_type)
        value_ref = None
        if action_type == "INPUT":
            value_ref = _infer_value_ref_for_input(test_data, f"steps[{index}]")
            if value_ref is None:
                value_ref = _default_generated_value_ref(text)
        elif action_type == "GUI_TASK" and _goal_needs_generated_text(text):
            value_ref = _infer_value_ref_for_input(test_data, f"steps[{index}]") or _default_generated_value_ref(text)
        return cls(
            step_id=f"step_{index + 1:03d}",
            instruction=text,
            action_type=action_type,
            step_mode=step_mode,
            value_ref=value_ref,
        )

    def resolved_value(self, test_data: Mapping[str, Any]) -> str | None:
        if self.value is not None:
            return self.value
        if self.value_ref is None:
            return None
        if self.value_ref not in test_data:
            if _is_generated_data_ref(self.value_ref):
                return None
            raise TestCaseError(f"step {self.step_id} references missing test_data key: {self.value_ref}")
        return _expect_str(test_data[self.value_ref], f"test_data.{self.value_ref}")

    def as_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "instruction": self.instruction,
            "action_type": self.action_type,
            "step_mode": self.step_mode,
            "target": dict(self.target),
            "value": self.value,
            "value_ref": self.value_ref,
            "timeout_seconds": self.timeout_seconds,
            "max_retries": self.max_retries,
        }


@dataclass(frozen=True)
class VerificationStep:
    verification_step_id: str
    instruction: str
    action_type: str = "OBSERVE"
    target: Mapping[str, Any] = field(default_factory=dict)
    timeout_seconds: float = 10.0
    max_retries: int = 0

    @classmethod
    def from_json(cls, value: Mapping[str, Any], index: int) -> "VerificationStep":
        data = _expect_mapping(value, f"verification_steps[{index}]")
        step_id = _expect_str(
            data.get("verification_step_id"),
            f"verification_steps[{index}].verification_step_id",
        )
        action_type = _expect_str(
            data.get("action_type") or "OBSERVE",
            f"verification_steps[{index}].action_type",
        ).upper()
        timeout = data.get("timeout_seconds", 10.0)
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
            raise TestCaseError(f"verification_steps[{index}].timeout_seconds must be positive")
        retries = data.get("max_retries", 0)
        if not isinstance(retries, int) or isinstance(retries, bool) or retries < 0:
            raise TestCaseError(f"verification_steps[{index}].max_retries must be non-negative")
        return cls(
            verification_step_id=step_id,
            instruction=_expect_str(
                data.get("instruction"), f"verification_steps[{index}].instruction"
            ),
            action_type=action_type,
            target=_expect_mapping(data.get("target"), f"verification_steps[{index}].target"),
            timeout_seconds=float(timeout),
            max_retries=retries,
        )

    @property
    def is_read_only(self) -> bool:
        return self.action_type in READ_ONLY_VERIFICATION_ACTION_TYPES

    def as_dict(self) -> dict[str, Any]:
        return {
            "verification_step_id": self.verification_step_id,
            "instruction": self.instruction,
            "action_type": self.action_type,
            "target": dict(self.target),
            "timeout_seconds": self.timeout_seconds,
            "max_retries": self.max_retries,
            "read_only_action": self.is_read_only,
        }


@dataclass(frozen=True)
class ExpectedAssertion:
    assertion_id: str
    type: str
    expected_value: str | None = None
    expected_value_ref: str | None = None
    surface: str | None = None
    required: bool = True
    after_step: str | None = None
    historical_match_not_sufficient: bool = False
    requires_verification_runner: bool = False

    @classmethod
    def from_json(
        cls,
        value: Mapping[str, Any] | str,
        index: int,
        *,
        test_data: Mapping[str, Any] | None = None,
    ) -> "ExpectedAssertion":
        if isinstance(value, str):
            text = _expect_str(value, f"expected_results[{index}]")
            ref = _infer_value_ref_for_expected_text(test_data or {}, text)
            return cls(
                assertion_id=f"expected_result_{index + 1:03d}",
                type="TEXT_VISIBLE",
                expected_value_ref=ref,
                expected_value=None if ref is not None else text,
                surface=text,
                historical_match_not_sufficient=False,
            )
        data = _expect_mapping(value, f"expected_results[{index}]")
        expected_value = data.get("expected_value")
        expected_value_ref = data.get("expected_value_ref")
        if expected_value is not None and expected_value_ref is not None:
            raise TestCaseError(
                f"expected_results[{index}] cannot set both expected_value and expected_value_ref"
            )
        required = data.get("required", True)
        if not isinstance(required, bool):
            raise TestCaseError(f"expected_results[{index}].required must be boolean")
        surface = data.get("surface")
        after_step = data.get("after_step")
        if surface == "":
            surface = None
        if after_step == "":
            after_step = None
        historical_match_not_sufficient = data.get(
            "historical_match_not_sufficient", False
        )
        if not isinstance(historical_match_not_sufficient, bool):
            raise TestCaseError(
                f"expected_results[{index}].historical_match_not_sufficient must be boolean"
            )
        requires_verification_runner = data.get("requires_verification_runner", False)
        if not isinstance(requires_verification_runner, bool):
            raise TestCaseError(
                f"expected_results[{index}].requires_verification_runner must be boolean"
            )
        return cls(
            assertion_id=_expect_str(data.get("assertion_id"), f"expected_results[{index}].assertion_id"),
            type=_expect_str(data.get("type"), f"expected_results[{index}].type").upper(),
            expected_value=_expect_str(expected_value, f"expected_results[{index}].expected_value")
            if expected_value is not None
            else None,
            expected_value_ref=_expect_str(expected_value_ref, f"expected_results[{index}].expected_value_ref")
            if expected_value_ref is not None
            else None,
            surface=_expect_str(surface, f"expected_results[{index}].surface")
            if surface is not None
            else None,
            required=required,
            after_step=_expect_str(after_step, f"expected_results[{index}].after_step")
            if after_step is not None
            else None,
            historical_match_not_sufficient=historical_match_not_sufficient,
            requires_verification_runner=requires_verification_runner,
        )

    def resolved_value(self, test_data: Mapping[str, Any]) -> str | None:
        if self.expected_value is not None:
            return self.expected_value
        if self.expected_value_ref is None:
            return None
        if self.expected_value_ref not in test_data:
            if _is_generated_data_ref(self.expected_value_ref):
                return None
            raise TestCaseError(
                f"assertion {self.assertion_id} references missing test_data key: {self.expected_value_ref}"
            )
        return _expect_str(test_data[self.expected_value_ref], f"test_data.{self.expected_value_ref}")

    def as_dict(self) -> dict[str, Any]:
        return {
            "assertion_id": self.assertion_id,
            "type": self.type,
            "expected_value": self.expected_value,
            "expected_value_ref": self.expected_value_ref,
            "surface": self.surface,
            "required": self.required,
            "after_step": self.after_step,
            "historical_match_not_sufficient": self.historical_match_not_sufficient,
            "requires_verification_runner": self.requires_verification_runner,
        }


@dataclass(frozen=True)
class Precondition:
    condition_id: str
    type: str
    value: str | None = None
    value_ref: str | None = None
    value_candidates: tuple[str, ...] = ()
    failure_class: str = "ENV_BLOCKED"
    description: str | None = None

    @classmethod
    def from_json(cls, value: Any, index: int) -> "Precondition":
        if isinstance(value, str):
            return cls(
                condition_id=f"precondition_{index + 1}",
                type="TEXT_VISIBLE",
                value=_expect_str(value, f"preconditions[{index}]"),
                failure_class="TEST_EXECUTION_FAIL",
                description=_expect_str(value, f"preconditions[{index}]"),
            )
        data = _expect_mapping(value, f"preconditions[{index}]")
        value_literal = data.get("value")
        value_ref = data.get("value_ref")
        candidates = _optional_string_list(
            data.get("value_candidates"), f"preconditions[{index}].value_candidates"
        )
        if sum(item is not None for item in (value_literal, value_ref)) + bool(candidates) > 1:
            raise TestCaseError(
                f"preconditions[{index}] can set only one of value, value_ref, value_candidates"
            )
        description = data.get("description")
        return cls(
            condition_id=_expect_str(data.get("condition_id"), f"preconditions[{index}].condition_id"),
            type=_expect_supported(
                data.get("type"),
                SUPPORTED_ASSERTION_TYPES,
                f"preconditions[{index}].type",
            ),
            value=_expect_str(value_literal, f"preconditions[{index}].value")
            if value_literal is not None
            else None,
            value_ref=_expect_str(value_ref, f"preconditions[{index}].value_ref")
            if value_ref is not None
            else None,
            value_candidates=candidates,
            failure_class=_expect_supported(
                data.get("failure_class", "ENV_BLOCKED"),
                SUPPORTED_FAILURE_CLASSES,
                f"preconditions[{index}].failure_class",
            ),
            description=_expect_str(description, f"preconditions[{index}].description")
            if description is not None
            else None,
        )

    def resolved_values(self, test_data: Mapping[str, Any]) -> tuple[str, ...]:
        if self.value is not None:
            return (self.value,)
        if self.value_ref is not None:
            if self.value_ref not in test_data:
                raise TestCaseError(
                    f"precondition {self.condition_id} references missing test_data key: {self.value_ref}"
                )
            return (_expect_str(test_data[self.value_ref], f"test_data.{self.value_ref}"),)
        return self.value_candidates

    def as_dict(self) -> dict[str, Any]:
        return {
            "condition_id": self.condition_id,
            "type": self.type,
            "value": self.value,
            "value_ref": self.value_ref,
            "value_candidates": list(self.value_candidates),
            "failure_class": self.failure_class,
            "description": self.description,
        }


@dataclass(frozen=True)
class ForbiddenEffect:
    assertion_id: str
    type: str = "TEXT_ABSENT"
    value: str | None = None
    value_ref: str | None = None
    value_candidates: tuple[str, ...] = ()

    @classmethod
    def from_json(cls, value: Any, index: int) -> "ForbiddenEffect":
        if isinstance(value, str):
            return cls(
                assertion_id=f"forbidden_effect_{index + 1}",
                value=_expect_str(value, f"forbidden_effects[{index}]"),
            )
        data = _expect_mapping(value, f"forbidden_effects[{index}]")
        value_literal = data.get("value")
        value_ref = data.get("value_ref")
        candidates = _optional_string_list(
            data.get("value_candidates"), f"forbidden_effects[{index}].value_candidates"
        )
        if sum(item is not None for item in (value_literal, value_ref)) + bool(candidates) != 1:
            raise TestCaseError(
                f"forbidden_effects[{index}] must set exactly one of value, value_ref, value_candidates"
            )
        effect_type = _expect_supported(
            data.get("type", "TEXT_ABSENT"),
            SUPPORTED_ASSERTION_TYPES,
            f"forbidden_effects[{index}].type",
        )
        if effect_type != "TEXT_ABSENT":
            raise TestCaseError(
                f"forbidden_effects[{index}].type must be TEXT_ABSENT"
            )
        return cls(
            assertion_id=_expect_str(
                data.get("assertion_id"), f"forbidden_effects[{index}].assertion_id"
            ),
            type=effect_type,
            value=_expect_str(value_literal, f"forbidden_effects[{index}].value")
            if value_literal is not None
            else None,
            value_ref=_expect_str(value_ref, f"forbidden_effects[{index}].value_ref")
            if value_ref is not None
            else None,
            value_candidates=candidates,
        )

    def resolved_values(self, test_data: Mapping[str, Any]) -> tuple[str, ...]:
        if self.value is not None:
            return (self.value,)
        if self.value_ref is not None:
            if self.value_ref not in test_data:
                raise TestCaseError(
                    f"forbidden effect {self.assertion_id} references missing test_data key: {self.value_ref}"
                )
            return (_expect_str(test_data[self.value_ref], f"test_data.{self.value_ref}"),)
        return self.value_candidates

    def as_dict(self) -> dict[str, Any]:
        return {
            "assertion_id": self.assertion_id,
            "type": self.type,
            "value": self.value,
            "value_ref": self.value_ref,
            "value_candidates": list(self.value_candidates),
        }


@dataclass(frozen=True)
class TestCaseSpec:
    test_case_id: str
    app_under_test: AppUnderTest
    feature: str
    steps: tuple[TestStep, ...]
    expected_results: tuple[ExpectedAssertion, ...]
    preconditions: tuple[Precondition, ...] = ()
    test_data: Mapping[str, Any] = field(default_factory=dict)
    observation_policy: Mapping[str, Any] = field(default_factory=lambda: dict(DEFAULT_OBSERVATION_POLICY))
    verification_steps: tuple[VerificationStep, ...] = ()
    verification_policy: Mapping[str, Any] = field(default_factory=lambda: dict(DEFAULT_VERIFICATION_POLICY))
    verification_runner_policy: str = DEFAULT_VERIFICATION_RUNNER_POLICY
    forbidden_effects: tuple[ForbiddenEffect, ...] = ()
    risk_level: str = "LOW"
    metadata: Mapping[str, Any] = field(default_factory=dict)
    source_text: str | None = None
    runtime_generated_data: Mapping[str, Any] = field(default_factory=dict)
    schema_version: str = TEST_CASE_SCHEMA_VERSION

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> "TestCaseSpec":
        data = _expect_mapping(value, "test case")
        schema_version = _expect_str(data.get("schema_version"), "schema_version")
        if schema_version != TEST_CASE_SCHEMA_VERSION:
            raise TestCaseError(f"unsupported schema_version: {schema_version}")
        test_data = _expect_mapping(data.get("test_data"), "test_data")
        steps = tuple(
            TestStep.from_json(item, index, test_data=test_data)
            for index, item in enumerate(_expect_list(data.get("steps"), "steps"))
        )
        if not steps:
            raise TestCaseError("steps must be non-empty")
        step_ids = [step.step_id for step in steps]
        if len(step_ids) != len(set(step_ids)):
            raise TestCaseError("step_id values must be unique")
        assertions = tuple(
            ExpectedAssertion.from_json(item, index, test_data=test_data)
            for index, item in enumerate(
                _expect_list(data.get("expected_results"), "expected_results")
            )
        )
        if not assertions:
            raise TestCaseError("expected_results must be non-empty")
        assertion_ids = [item.assertion_id for item in assertions]
        if len(assertion_ids) != len(set(assertion_ids)):
            raise TestCaseError("assertion_id values must be unique")
        spec = cls(
            test_case_id=_expect_str(data.get("test_case_id"), "test_case_id"),
            app_under_test=AppUnderTest.from_json(data.get("app_under_test")),
            feature=str(data.get("feature") or ""),
            preconditions=tuple(
                Precondition.from_json(item, index)
                for index, item in enumerate(
                    _expect_list(data.get("preconditions"), "preconditions")
                )
            ),
            test_data=test_data,
            observation_policy=_observation_policy(data.get("observation_policy")),
            verification_steps=tuple(
                VerificationStep.from_json(item, index)
                for index, item in enumerate(
                    _expect_list(data.get("verification_steps"), "verification_steps")
                )
            ),
            verification_policy=_verification_policy(data.get("verification_policy")),
            verification_runner_policy=_expect_supported(
                data.get(
                    "verification_runner_policy",
                    DEFAULT_VERIFICATION_RUNNER_POLICY,
                ),
                SUPPORTED_VERIFICATION_RUNNER_POLICIES,
                "verification_runner_policy",
            ),
            steps=steps,
            expected_results=assertions,
            forbidden_effects=tuple(
                ForbiddenEffect.from_json(item, index)
                for index, item in enumerate(
                    _expect_list(data.get("forbidden_effects"), "forbidden_effects")
                )
            ),
            risk_level=str(data.get("risk_level") or "LOW").strip().upper(),
            metadata=_expect_mapping(data.get("metadata"), "metadata"),
            source_text=(
                _expect_str(data.get("source_text"), "source_text")
                if data.get("source_text") is not None
                else None
            ),
            runtime_generated_data=_expect_mapping(
                data.get("runtime_generated_data"), "runtime_generated_data"
            ),
            schema_version=schema_version,
        )
        spec.validate_references()
        return spec

    def validate_references(self) -> None:
        for step in self.steps:
            step.resolved_value(self.test_data)
        for assertion in self.expected_results:
            assertion.resolved_value(self.test_data)
            if assertion.after_step is not None and assertion.after_step not in {
                step.step_id for step in self.steps
            }:
                raise TestCaseError(
                    f"assertion {assertion.assertion_id} references unknown after_step: {assertion.after_step}"
                )
        for precondition in self.preconditions:
            precondition.resolved_values(self.test_data)
        for effect in self.forbidden_effects:
            effect.resolved_values(self.test_data)
        if (
            self.verification_runner_policy == "NEVER"
            and any(assertion.requires_verification_runner for assertion in self.expected_results)
        ):
            raise TestCaseError(
                "verification_runner_policy NEVER conflicts with an assertion "
                "that requires_verification_runner"
            )
        verification_ids = [step.verification_step_id for step in self.verification_steps]
        if len(verification_ids) != len(set(verification_ids)):
            raise TestCaseError("verification_step_id values must be unique")
        max_steps = self.verification_policy.get("max_steps")
        if isinstance(max_steps, int) and len(self.verification_steps) > max_steps:
            raise TestCaseError(
                "verification_steps length exceeds verification_policy.max_steps"
            )

    def with_runtime_context(self, *, run_id: str) -> "TestCaseSpec":
        base_data = _replace_runtime_templates(self.test_data, run_id=run_id)
        existing_generated = _replace_runtime_templates(
            self.runtime_generated_data,
            run_id=run_id,
        )
        generated = {
            ref: _generate_runtime_value(ref, run_id=run_id)
            for ref in sorted(self._required_generated_runtime_refs())
            if ref not in base_data
        }
        runtime_generated_data = {**existing_generated, **generated}
        resolved = replace(
            self,
            test_data={**base_data, **generated},
            runtime_generated_data=runtime_generated_data,
        )
        resolved.validate_references()
        return resolved

    def _required_generated_runtime_refs(self) -> set[str]:
        refs: set[str] = set()
        for step in self.steps:
            if _is_generated_data_ref(step.value_ref):
                refs.add(str(step.value_ref))
        for assertion in self.expected_results:
            if _is_generated_data_ref(assertion.expected_value_ref):
                refs.add(str(assertion.expected_value_ref))
        return refs

    @property
    def sha256(self) -> str:
        return hashlib.sha256(_canonical_bytes(self.as_dict())).hexdigest()

    def strict_runner_instruction(self) -> str:
        lines = [
            f"App under test: {self.app_under_test.name}",
            f"Feature: {self.feature}",
            "Execute the following test steps exactly in order.",
        ]
        if self.preconditions:
            lines.append("Preconditions:")
            for item in self.preconditions:
                values = item.resolved_values(self.test_data)
                detail = ", ".join(values) if values else item.description or item.condition_id
                lines.append(f"- [{item.condition_id}] {item.type}: {detail}")
        lines.append("Steps:")
        for index, step in enumerate(self.steps, 1):
            value = step.resolved_value(self.test_data)
            suffix = f" Value: {value!r}." if value is not None else ""
            mode = "goal" if step.step_mode == "GOAL" else "atomic"
            lines.append(f"{index}. [{step.step_id}] ({mode}) {step.instruction}{suffix}")
        lines.append("Do not skip, reorder, or replace these user steps.")
        lines.append("For goal steps, internal micro-actions are allowed only within the current user step.")
        return "\n".join(lines)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "test_case_id": self.test_case_id,
            "app_under_test": self.app_under_test.as_dict(),
            "feature": self.feature,
            "preconditions": [item.as_dict() for item in self.preconditions],
            "test_data": dict(self.test_data),
            "observation_policy": dict(self.observation_policy),
            "steps": [step.as_dict() for step in self.steps],
            "expected_results": [item.as_dict() for item in self.expected_results],
            "verification_steps": [step.as_dict() for step in self.verification_steps],
            "verification_policy": dict(self.verification_policy),
            "verification_runner_policy": self.verification_runner_policy,
            "forbidden_effects": [item.as_dict() for item in self.forbidden_effects],
            "risk_level": self.risk_level,
            "metadata": dict(self.metadata),
            "source_text": self.source_text,
            "runtime_generated_data": dict(self.runtime_generated_data),
        }


def load_test_case(path: Path) -> TestCaseSpec:
    source = path.resolve(strict=True)
    try:
        payload = json.loads(source.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise TestCaseError(f"invalid JSON in test case: {exc}") from exc
    return TestCaseSpec.from_json(payload)


def dump_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
