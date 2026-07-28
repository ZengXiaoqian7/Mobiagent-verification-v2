"""Runtime-only verification intent compiled from final expected results.

This module keeps verification navigation out of the user-authored test case.
The intent is generated after the business execution when direct evidence is
insufficient, then consumed by constrained read-only verification runners.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping

from .schema import TestCaseSpec, VerificationStep


VERIFICATION_INTENT_SCHEMA_VERSION = "app-test-verification-intent-v1"


@dataclass(frozen=True)
class VerificationIntent:
    test_case_id: str
    target_surface: str | None
    expected_texts: tuple[str, ...]
    read_only_strategies: tuple[str, ...]
    generated_steps: tuple[VerificationStep, ...]
    source: str = "expected_results"
    schema_version: str = VERIFICATION_INTENT_SCHEMA_VERSION

    @property
    def sha256(self) -> str:
        payload = json.dumps(
            self.as_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    @property
    def has_observable_goal(self) -> bool:
        return bool(self.expected_texts or self.target_surface)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "test_case_id": self.test_case_id,
            "target_surface": self.target_surface,
            "expected_texts": list(self.expected_texts),
            "read_only_strategies": list(self.read_only_strategies),
            "generated_steps": [
                {
                    "ordinal": index + 1,
                    **step.as_dict(),
                }
                for index, step in enumerate(self.generated_steps)
            ],
            "source": self.source,
        }


def compile_verification_intent(test_case: TestCaseSpec) -> VerificationIntent:
    surface = _target_surface(test_case)
    expected_texts = tuple(
        dict.fromkeys(
            value
            for value in (
                assertion.resolved_value(test_case.test_data)
                for assertion in test_case.expected_results
                if assertion.required and assertion.type == "TEXT_VISIBLE"
            )
            if value is not None
        )
    )
    steps = tuple(_generated_steps(surface, expected_texts)) if (surface or expected_texts) else ()
    strategies = tuple(
        step.action_type
        for step in steps
    )
    return VerificationIntent(
        test_case_id=test_case.test_case_id,
        target_surface=surface,
        expected_texts=expected_texts,
        read_only_strategies=strategies,
        generated_steps=steps,
    )


def effective_verification_steps(test_case: TestCaseSpec) -> tuple[VerificationStep, ...]:
    if test_case.verification_steps:
        return test_case.verification_steps
    return compile_verification_intent(test_case).generated_steps


def _target_surface(test_case: TestCaseSpec) -> str | None:
    for assertion in test_case.expected_results:
        if assertion.required and assertion.surface:
            return assertion.surface
    for assertion in test_case.expected_results:
        if assertion.surface:
            return assertion.surface
    return None


def _generated_steps(surface: str | None, expected_texts: tuple[str, ...]) -> list[VerificationStep]:
    steps: list[VerificationStep] = []
    target = _surface_target(surface)
    if target is not None:
        steps.append(
            VerificationStep(
                verification_step_id="auto_navigate_observation_surface",
                instruction=(
                    "Navigate only if a safe read-only entry to the expected "
                    "observation surface is visible"
                ),
                action_type="NAVIGATE",
                target=target,
                timeout_seconds=5.0,
                max_retries=1,
            )
        )
    steps.append(
        VerificationStep(
            verification_step_id="auto_wait_for_result_observation",
            instruction="Wait briefly for the expected result surface to stabilize",
            action_type="WAIT",
            target={},
            timeout_seconds=2.0,
            max_retries=0,
        )
    )
    steps.append(
        VerificationStep(
            verification_step_id="auto_observe_result_surface",
            instruction="Observe the current read-only surface for the expected result",
            action_type="OBSERVE",
            target={
                "surface": surface or "current_surface",
                "expected_texts": list(expected_texts),
                "surface_text_candidates": _surface_text_candidates(surface),
                "surface_shape_required": bool(_surface_shape_text_groups(surface)),
                "surface_shape_text_groups": _surface_shape_text_groups(surface),
            },
            timeout_seconds=5.0,
            max_retries=0,
        )
    )
    steps.append(
        VerificationStep(
            verification_step_id="auto_scroll_result_surface_once",
            instruction="Scroll once within the read-only result surface if the result is not immediately visible",
            action_type="SCROLL",
            target={
                "direction": "up",
                "surface": surface or "current_surface",
                "expected_texts": list(expected_texts),
                "surface_text_candidates": _surface_text_candidates(surface),
                "surface_shape_required": bool(_surface_shape_text_groups(surface)),
                "surface_shape_text_groups": _surface_shape_text_groups(surface),
            },
            timeout_seconds=5.0,
            max_retries=0,
        )
    )
    return steps


def _surface_target(surface: str | None) -> Mapping[str, Any] | None:
    text = str(surface or "").casefold()
    if not text.strip():
        return None
    candidates = _surface_text_candidates(surface)
    if not candidates:
        return None
    return {
        "surface": surface,
        "role": "navigation",
        "text_candidates": candidates,
        "surface_text_candidates": candidates,
        "surface_shape_required": bool(_surface_shape_text_groups(surface)),
        "surface_shape_text_groups": _surface_shape_text_groups(surface),
    }


def _surface_text_candidates(surface: str | None) -> list[str]:
    text = str(surface or "")
    folded = text.casefold()
    candidates: list[str] = []
    if any(term in folded for term in ("profile", "personal", "mine", "my ", "own", "个人主页", "主页", "我的", "本人")):
        candidates.extend(["我", "我的", "个人主页", "Profile", "Me", "Mine"])
    if any(term in folded for term in ("feed", "timeline", "动态", "列表")):
        candidates.extend(["Feed", "动态", "列表"])
    if any(term in folded for term in ("post", "note", "content", "笔记", "帖子", "内容")):
        candidates.extend(["Post", "Notes", "笔记", "帖子", "内容"])
    if any(term in folded for term in ("conversation", "chat", "message", "私信", "消息", "聊天")):
        candidates.extend(["Messages", "Chat", "消息", "聊天", "私信"])
    if text.strip():
        candidates.append(text.strip())
    return list(dict.fromkeys(candidates))


def _surface_shape_text_groups(surface: str | None) -> list[list[str]]:
    text = str(surface or "")
    folded = text.casefold()
    groups: list[list[str]] = []
    if any(term in folded for term in ("own_note", "profile_note", "profile_notes", "个人主页", "我的笔记")):
        groups.append(["我", "我的", "个人主页", "Profile", "Me", "Mine"])
        groups.append(["笔记", "帖子", "内容", "Notes", "Posts"])
    elif any(term in folded for term in ("conversation", "chat", "message", "私信", "消息", "聊天")):
        groups.append(["Messages", "Chat", "消息", "聊天", "私信"])
    elif any(term in folded for term in ("profile", "personal", "mine", "my ", "own", "主页", "我的")):
        groups.append(["我", "我的", "个人主页", "Profile", "Me", "Mine"])
    return groups
