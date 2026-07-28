"""Small adapter from App-test assertions to the legacy visual checker.

The App-test oracle owns freshness and surface safety.  This module only asks
the existing benchmark checker registry for additional visual semantic
evidence when a raw trace is available; it never creates an App verdict by
itself.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping

from .model_client import model_config_from_env
from .executor import ExecutionRecord
from .schema import ExpectedAssertion, TestCaseSpec

from verification_benchmark.evaluation_framework.checker_registry import (
    CHECKER_REGISTRY_VERSION,
    CriterionCheckerRegistry,
)
from verification_benchmark.evaluation_framework.models import (
    ContractIR,
    CriterionIR,
    TemporalSemantics,
)
from verification_benchmark.evaluation_framework.phase5_full_verifier_comparison import (
    ProviderConfig,
    VisionCallRecorder,
)
from verification_benchmark.evaluation_framework.phase5_trace_case import CasePaths
from verification_benchmark.evaluation_framework.task_spec import TaskSpec
from verification_benchmark.evaluation_framework.trace_adapter import load_trace_directory


LEGACY_CHECKER_ADAPTER_VERSION = "app-test-legacy-checker-adapter-v1"


def review_with_legacy_checker(
    *,
    test_case: TestCaseSpec,
    assertion: ExpectedAssertion,
    execution: ExecutionRecord,
) -> Mapping[str, Any] | None:
    """Return one legacy checker record, or ``None`` when it is not applicable."""

    if assertion.type != "TEXT_VISIBLE" or not execution.raw_trace_dir:
        if assertion.type != "STATE_CHANGED" or not assertion.surface or not execution.raw_trace_dir:
            return None
    expected = assertion.resolved_value(test_case.test_data)
    if assertion.type == "TEXT_VISIBLE" and not expected:
        return None
    task_expected = expected or f"state changed on {assertion.surface}"
    trace_root = Path(execution.raw_trace_dir)
    if not trace_root.is_dir() or not (trace_root / "actions.json").is_file():
        return None

    try:
        bundle = load_trace_directory(trace_root, trace_ref="app-test")
        task = _task_for_assertion(test_case, assertion, task_expected)
        content_id = None
        criteria = []
        if assertion.type == "TEXT_VISIBLE":
            content_id = f"app_test.content.{assertion.assertion_id}"
            criteria.append(
                CriterionIR(
                    content_id,
                    TemporalSemantics.EVENTUAL_STATE,
                    required=True,
                    description=(
                        f"The exact runtime result text {expected!r} is visible on "
                        f"the declared result surface {assertion.surface or 'the result surface'}."
                    ),
                )
            )
        state_id = None
        state_metadata: dict[str, Any] = {}
        if assertion.type == "STATE_CHANGED" and assertion.surface:
            state_id = f"state.{assertion.assertion_id}"
            criteria.append(
                CriterionIR(
                    state_id,
                    TemporalSemantics.EVENTUAL_STATE,
                    required=False,
                    description=(
                        "Advisory legacy state evidence for the App-test state-change assertion."
                    ),
                )
            )
            state_metadata[state_id] = {
                "desired_state": "activated",
                "anchor_source": "explicit",
                "anchors": list(_surface_markers(assertion.surface)),
                "frame_scope": "terminal",
                "allow_vlm": False,
            }
        page_id = None
        if assertion.surface:
            page_id = "outcome.page_domain_semantics"
            criteria.append(
                CriterionIR(
                    page_id,
                    TemporalSemantics.EVENTUAL_STATE,
                    required=True,
                    description="The declared App result surface is visibly reached.",
                )
            )
        contract = ContractIR.from_criteria(
            contract_id=f"app-test-legacy:{test_case.test_case_id}:{assertion.assertion_id}",
            criteria=criteria,
            source="app-test-adapter",
            task_family=task.task_family,
            metadata=(
                {
                    **(
                        {
                            "page_domain_semantics": {
                                page_id: {
                                    "expected": [
                                        {
                                            "domain_id": assertion.surface,
                                            "description": assertion.surface,
                                            "markers_any": list(_surface_markers(assertion.surface)),
                                        }
                                    ]
                                }
                            }
                        }
                        if page_id is not None
                        else {}
                    ),
                    **({"state_evidence": state_metadata} if state_metadata else {}),
                }
            ),
        )
        checker_result = CriterionCheckerRegistry().evaluate(
            CasePaths(trace_root, trace_root / "app-test-intake.json"),
            contract,
            task,
            bundle,
            trace_root,
            _legacy_recorder(),
        )
        records = checker_result.get("criteria")
        if not isinstance(records, Mapping):
            return _unknown("legacy checker returned no criteria", checker_result)
        content = records.get(content_id) if content_id is not None else None
        page = records.get(page_id) if page_id is not None else None
        state = records.get(state_id) if state_id is not None else None
        statuses = [
            str(item.get("status") or "UNKNOWN_EVIDENCE").upper()
            for item in (content, page)
            if isinstance(item, Mapping)
        ]
        if "VIOLATED" in statuses:
            status = "VIOLATED"
        elif statuses and all(item == "SATISFIED" for item in statuses):
            status = "SATISFIED"
        else:
            status = "UNKNOWN_EVIDENCE"
        reasons = [
            str(item.get("reason") or "")
            for item in (content, page)
            if isinstance(item, Mapping) and str(item.get("reason") or "")
        ]
        return {
            "status": status,
            "reason": "; ".join(reasons) or "legacy checker returned no reason",
            "evidence": {
                "adapter_version": LEGACY_CHECKER_ADAPTER_VERSION,
                "checker_registry_version": CHECKER_REGISTRY_VERSION,
                "task": task.payload(),
                "checker_result": dict(checker_result),
                "trace_integrity": bundle.capability_profile.integrity.value,
                "state_evidence": dict(state) if isinstance(state, Mapping) else None,
            },
        }
    except Exception as exc:  # noqa: BLE001 - legacy evidence is advisory here.
        return _unknown(
            f"legacy checker adapter failed: {type(exc).__name__}: {exc}",
            {"adapter_version": LEGACY_CHECKER_ADAPTER_VERSION},
        )


def _task_for_assertion(
    test_case: TestCaseSpec,
    assertion: ExpectedAssertion,
    expected: str,
) -> TaskSpec:
    surface = assertion.surface or "result surface"
    task_text = (
        f"Verify the App feature {test_case.feature}: the result content "
        f'"{expected}" must be visible on the {surface} result surface. '
        "This is a completed result observation, not an input or editor state."
    )
    family = "creator_homepage" if _is_profile_surface(surface) else "composite_workflow"
    return TaskSpec(
        task_id=f"app-test-{test_case.test_case_id}-{assertion.assertion_id}",
        task_text=task_text,
        task_family=family,
        initial_app=test_case.app_under_test.name,
        target_apps=(test_case.app_under_test.name,),
        risk_level="read_only",
        parsed_intent={"entities": {"quoted": [expected]}},
    )


def _surface_markers(surface: str | None) -> tuple[str, ...]:
    text = str(surface or "").casefold()
    values: list[str] = []
    if any(term in text for term in ("profile", "personal", "mine", "own", "主页", "我的")):
        values.extend(("我", "我的", "个人主页", "Profile", "Me"))
    if any(term in text for term in ("note", "post", "content", "帖子", "笔记", "内容")):
        values.extend(("笔记", "帖子", "内容", "Notes", "Posts"))
    if any(term in text for term in ("conversation", "chat", "message", "消息", "聊天", "私信")):
        values.extend(("消息", "聊天", "私信", "Messages", "Chat"))
    if surface:
        values.append(surface)
    return tuple(dict.fromkeys(values))


def _is_profile_surface(surface: str) -> bool:
    text = surface.casefold()
    return any(term in text for term in ("profile", "personal", "mine", "own", "主页", "我的"))


def _legacy_recorder() -> VisionCallRecorder | None:
    enabled = os.getenv("APP_TEST_ENABLE_LEGACY_CHECKER_VLM", "").strip().lower()
    if enabled not in {"1", "true", "yes", "on"}:
        return None
    config = model_config_from_env(
        base_url_names=(
            "APP_TEST_VERIFIER_BASE_URL",
            "MOBIAGENT_VERIFIER_BASE_URL",
            "MOBIAGENT_BASE_URL",
        ),
        model_names=(
            "APP_TEST_VERIFIER_MODEL",
            "MOBIAGENT_VERIFIER_MODEL",
            "MOBIAGENT_MODEL",
        ),
    )
    return VisionCallRecorder(
        ProviderConfig(
            base_url=config.base_url,
            model=config.model,
            api_key_env="MOBIAGENT_API_KEY",
            api_key=config.api_key,
        )
    )


def _unknown(reason: str, evidence: Mapping[str, Any]) -> Mapping[str, Any]:
    return {
        "status": "UNKNOWN_EVIDENCE",
        "reason": reason,
        "evidence": dict(evidence),
    }


__all__ = ["LEGACY_CHECKER_ADAPTER_VERSION", "review_with_legacy_checker"]
