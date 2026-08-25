"""App behavior oracle verifier for App tests."""

from __future__ import annotations

import base64
import os
from pathlib import Path
from typing import Mapping

from .contract import AppTestContract
from .evidence import (
    ExecutionEvidence,
    TextEvidenceSlice,
    assess_negative_observation_sufficiency,
    assess_text_freshness,
    text_contains,
)
from .executor import ExecutionRecord
from .model_client import (
    ModelConfigError,
    extract_json_object,
    model_config_from_env,
    post_chat_completion,
)
from .result_types import (
    AppBehaviorResult,
    AppBehaviorStatus,
    AssertionResult,
    ExecutionConformanceResult,
    ExecutionStatus,
)
from .offline_verifier import (
    OfflineReviewStatus,
    OfflineTraceReview,
    offline_review_from_mapping,
)
from .schema import ExpectedAssertion, ForbiddenEffect, TestCaseSpec


def verify_app_behavior(
    test_case: TestCaseSpec,
    execution: ExecutionRecord,
    conformance: ExecutionConformanceResult,
    contract: AppTestContract,
    verification_execution: ExecutionRecord | None = None,
    verification_context: Mapping[str, object] | None = None,
    offline_review: OfflineTraceReview | Mapping[str, object] | None = None,
) -> AppBehaviorResult:
    if conformance.status != ExecutionStatus.COMPLETED:
        return AppBehaviorResult(
            status=AppBehaviorStatus.NOT_EVALUATED,
            assertion_results=(),
            reason="App behavior is not evaluated when execution conformance fails",
            contract_sha256=contract.sha256,
        )

    expected_results = tuple(
        _evaluate_assertion(
            assertion,
            test_case,
            execution,
            contract,
            verification_execution=verification_execution,
            verification_context=verification_context,
            offline_review=offline_review,
        )
        for assertion in test_case.expected_results
    )
    forbidden_results = tuple(
        _evaluate_forbidden_effect(effect, test_case, execution, contract)
        for effect in test_case.forbidden_effects
    )
    results = expected_results + forbidden_results
    required_ids = {
        assertion.assertion_id for assertion in test_case.expected_results if assertion.required
    } | {effect.assertion_id for effect in test_case.forbidden_effects}
    required = tuple(result for result in results if result.assertion_id in required_ids)
    if any(item.status == AppBehaviorStatus.UNSUPPORTED for item in required):
        return AppBehaviorResult(
            status=AppBehaviorStatus.UNSUPPORTED,
            assertion_results=results,
            reason="at least one required App behavior assertion is unsupported",
            contract_sha256=contract.sha256,
        )
    if any(item.status == AppBehaviorStatus.VIOLATED for item in required):
        return AppBehaviorResult(
            status=AppBehaviorStatus.VIOLATED,
            assertion_results=results,
            reason="one or more required App behavior assertions were violated",
            contract_sha256=contract.sha256,
        )
    if any(item.status == AppBehaviorStatus.UNKNOWN_EVIDENCE for item in required):
        return AppBehaviorResult(
            status=AppBehaviorStatus.UNKNOWN_EVIDENCE,
            assertion_results=results,
            reason="one or more required assertions lacked enough evidence",
            contract_sha256=contract.sha256,
        )
    return AppBehaviorResult(
        status=AppBehaviorStatus.SATISFIED,
        assertion_results=results,
        reason="all required App behavior assertions were satisfied",
        contract_sha256=contract.sha256,
    )


def _evaluate_forbidden_effect(
    effect: ForbiddenEffect,
    test_case: TestCaseSpec,
    execution: ExecutionRecord,
    contract: AppTestContract,
) -> AssertionResult:
    values = effect.resolved_values(test_case.test_data)
    evidence = ExecutionEvidence(execution)
    text_slice = evidence.observed_text_slice()
    negative_sufficiency = assess_negative_observation_sufficiency(
        text_slice,
        contract.observation_policy,
    )
    evidence_payload = {
        **text_slice.as_dict(),
        "negative_observation_sufficiency": negative_sufficiency.as_dict(),
        "required": True,
        "forbidden_effect": effect.as_dict(),
        "contract_sha256": contract.sha256,
    }
    expected_value = "|".join(values)
    if not values:
        return AssertionResult(
            effect.assertion_id,
            AppBehaviorStatus.UNSUPPORTED,
            "forbidden effect requires at least one resolved value",
            expected_value,
            evidence_payload,
        )
    present = any(text_contains(text_slice.texts, value) for value in values)
    if (
        not present
        and (not text_slice.evidence_sufficient or not negative_sufficiency.sufficient)
    ):
        return AssertionResult(
            effect.assertion_id,
            AppBehaviorStatus.UNKNOWN_EVIDENCE,
            "selected evidence cannot confirm forbidden effect absence",
            expected_value,
            evidence_payload,
        )
    return AssertionResult(
        effect.assertion_id,
        AppBehaviorStatus.VIOLATED if present else AppBehaviorStatus.SATISFIED,
        "forbidden effect text is visible" if present else "forbidden effect text is absent",
        expected_value,
        evidence_payload,
    )


def _evaluate_assertion(
    assertion: ExpectedAssertion,
    test_case: TestCaseSpec,
    execution: ExecutionRecord,
    contract: AppTestContract,
    *,
    verification_execution: ExecutionRecord | None = None,
    verification_context: Mapping[str, object] | None = None,
    offline_review: OfflineTraceReview | Mapping[str, object] | None = None,
) -> AssertionResult:
    expected_value = assertion.resolved_value(test_case.test_data)
    evidence = ExecutionEvidence(execution)
    verification_evidence = (
        ExecutionEvidence(verification_execution)
        if verification_execution is not None
        else None
    )
    text_slice = _text_slice(
        assertion,
        evidence,
        contract.observation_policy,
        verification_evidence=verification_evidence,
    )
    evidence_payload = {
        **text_slice.as_dict(),
        "initial_visible_texts": list(evidence.initial_texts()),
        "final_state": execution.final_state.as_dict(),
        "observation_policy": dict(contract.observation_policy),
        "required": assertion.required,
        "after_step": assertion.after_step,
        "historical_match_not_sufficient": assertion.historical_match_not_sufficient,
        "requires_verification_runner": assertion.requires_verification_runner,
    }
    negative_sufficiency = assess_negative_observation_sufficiency(
        text_slice,
        contract.observation_policy,
    )
    evidence_payload["negative_observation_sufficiency"] = negative_sufficiency.as_dict()
    if assertion.type == "TEXT_VISIBLE" and expected_value is not None:
        freshness = assess_text_freshness(
            evidence,
            text_slice,
            expected_value,
            required=assertion.historical_match_not_sufficient,
        )
        evidence_payload["freshness"] = freshness.as_dict()
    if verification_context is not None:
        evidence_payload["verification_runner"] = dict(verification_context)
    review = (
        offline_review
        if isinstance(offline_review, OfflineTraceReview)
        else offline_review_from_mapping(offline_review)
    )
    if review is not None:
        evidence_payload["offline_trace_review"] = review.as_dict()
        if review.status == OfflineReviewStatus.INVALID_TRACE:
            return AssertionResult(
                assertion.assertion_id,
                AppBehaviorStatus.UNKNOWN_EVIDENCE,
                "offline trace verifier rejected invalid trace evidence",
                expected_value,
                evidence_payload,
            )
        offline_assertion = review.assertion(assertion.assertion_id)
        if offline_assertion is not None and review.authoritative:
            if (
                not _offline_allows_visual_fallback(assertion, offline_assertion)
            ):
                return AssertionResult(
                    assertion.assertion_id,
                    offline_assertion.status,
                    f"offline trace verifier: {offline_assertion.reason}",
                    expected_value,
                    {
                        **dict(evidence_payload),
                        "offline_assertion_review": offline_assertion.as_dict(),
                    },
                )
            evidence_payload["offline_assertion_review"] = offline_assertion.as_dict()

    if assertion.type == "TEXT_VISIBLE":
        return _evaluate_text_visible(
            assertion,
            expected_value,
            execution,
            verification_execution or execution,
            evidence,
            text_slice,
            evidence_payload,
        )
    if assertion.type == "TEXT_ABSENT":
        return _evaluate_text_absent(
            assertion,
            expected_value,
            text_slice,
            evidence_payload,
        )
    if assertion.type == "STATE_CHANGED":
        changed = execution.final_state.state_changed
        if changed is None or not execution.final_state.evidence_sufficient:
            return AssertionResult(
                assertion.assertion_id,
                AppBehaviorStatus.UNKNOWN_EVIDENCE,
                "state change evidence is insufficient",
                expected_value,
                evidence_payload,
            )
        return AssertionResult(
            assertion.assertion_id,
            AppBehaviorStatus.SATISFIED if changed is True else AppBehaviorStatus.VIOLATED,
            "state changed" if changed is True else "state change was not observed",
            expected_value,
            evidence_payload,
        )
    if assertion.type == "SUCCESS_SIGNAL":
        if not execution.final_state.evidence_sufficient:
            return AssertionResult(
                assertion.assertion_id,
                AppBehaviorStatus.UNKNOWN_EVIDENCE,
                "success signal evidence is insufficient",
                expected_value,
                evidence_payload,
            )
        if expected_value is None:
            observed = bool(execution.final_state.success_signals)
            return AssertionResult(
                assertion.assertion_id,
                AppBehaviorStatus.SATISFIED if observed else AppBehaviorStatus.VIOLATED,
                "success signal observed" if observed else "success signal missing",
                expected_value,
                evidence_payload,
            )
        matched = text_contains(execution.final_state.success_signals, expected_value)
        return AssertionResult(
            assertion.assertion_id,
            AppBehaviorStatus.SATISFIED if matched else AppBehaviorStatus.VIOLATED,
            "success signal matched" if matched else "success signal missing",
            expected_value,
            evidence_payload,
        )
    return AssertionResult(
        assertion.assertion_id,
        AppBehaviorStatus.UNSUPPORTED,
        f"unsupported assertion type: {assertion.type}",
        expected_value,
        evidence_payload,
    )


def _evaluate_text_visible(
    assertion: ExpectedAssertion,
    expected_value: str | None,
    execution: ExecutionRecord,
    source_execution: ExecutionRecord,
    evidence: ExecutionEvidence,
    text_slice: TextEvidenceSlice,
    evidence_payload: Mapping[str, object],
) -> AssertionResult:
    if expected_value is None:
        return AssertionResult(
            assertion.assertion_id,
            AppBehaviorStatus.UNSUPPORTED,
            "TEXT_VISIBLE requires expected_value or expected_value_ref",
            expected_value,
            evidence_payload,
        )
    if assertion.historical_match_not_sufficient and assertion.after_step is None:
        return AssertionResult(
            assertion.assertion_id,
            AppBehaviorStatus.UNKNOWN_EVIDENCE,
            "freshness requires after_step evidence, but no after_step was declared",
            expected_value,
            evidence_payload,
        )
    if assertion.requires_verification_runner and "verification_runner" not in evidence_payload:
        return AssertionResult(
            assertion.assertion_id,
            AppBehaviorStatus.UNKNOWN_EVIDENCE,
            "assertion requires verification runner evidence from the declared observation surface",
            expected_value,
            evidence_payload,
        )
    if text_contains(text_slice.texts, expected_value):
        freshness = evidence_payload.get("freshness")
        if (
            assertion.historical_match_not_sufficient
            and (
                not isinstance(freshness, Mapping)
                or freshness.get("proven") is not True
            )
        ):
            reason = (
                freshness.get("reason")
                if isinstance(freshness, Mapping)
                else "post-action evidence does not prove a fresh occurrence"
            )
            return AssertionResult(
                assertion.assertion_id,
                AppBehaviorStatus.UNKNOWN_EVIDENCE,
                str(reason),
                expected_value,
                evidence_payload,
            )
        return AssertionResult(
            assertion.assertion_id,
            AppBehaviorStatus.SATISFIED,
            "expected text is visible in selected post-action evidence",
            expected_value,
            evidence_payload,
        )
    if (
        assertion.historical_match_not_sufficient
        and text_contains(evidence.initial_texts(), expected_value)
    ):
        return AssertionResult(
            assertion.assertion_id,
            AppBehaviorStatus.UNKNOWN_EVIDENCE,
            "matching text existed before the tested action and no fresh post-action match was observed",
            expected_value,
            evidence_payload,
        )
    visual_result = _evaluate_text_visible_with_visual_model(
        assertion,
        expected_value,
        source_execution,
        text_slice,
        evidence_payload,
    )
    if visual_result is not None:
        negative_sufficiency = evidence_payload.get("negative_observation_sufficiency")
        if (
            visual_result.status == AppBehaviorStatus.VIOLATED
            and isinstance(negative_sufficiency, Mapping)
            and negative_sufficiency.get("sufficient") is not True
        ):
            return AssertionResult(
                assertion.assertion_id,
                AppBehaviorStatus.UNKNOWN_EVIDENCE,
                "negative visual evidence does not cover the configured observation window",
                expected_value,
                visual_result.evidence,
            )
        return visual_result
    negative_sufficiency = evidence_payload.get("negative_observation_sufficiency")
    if (
        not text_slice.evidence_sufficient
        or not source_execution.final_state.evidence_sufficient
        or not isinstance(negative_sufficiency, Mapping)
        or negative_sufficiency.get("sufficient") is not True
    ):
        return AssertionResult(
            assertion.assertion_id,
            AppBehaviorStatus.UNKNOWN_EVIDENCE,
            "selected evidence cannot confirm the expected text appeared after this run",
            expected_value,
            evidence_payload,
        )
    return AssertionResult(
        assertion.assertion_id,
        AppBehaviorStatus.VIOLATED,
        "expected text is not visible in selected evidence",
        expected_value,
        evidence_payload,
    )


def _evaluate_text_visible_with_visual_model(
    assertion: ExpectedAssertion,
    expected_value: str,
    execution: ExecutionRecord,
    text_slice: TextEvidenceSlice,
    evidence_payload: Mapping[str, object],
) -> AssertionResult | None:
    if not _visual_verifier_enabled():
        return None
    screenshot_paths = tuple(_screenshot_paths(execution, text_slice))
    if not screenshot_paths:
        return AssertionResult(
            assertion.assertion_id,
            AppBehaviorStatus.UNKNOWN_EVIDENCE,
            "visual verifier is enabled but selected post-action screenshots are unavailable",
            expected_value,
            {
                **dict(evidence_payload),
                "visual_verifier": {
                    "enabled": True,
                    "status": "NO_SCREENSHOT",
                },
            },
        )
    try:
        decision = _model_visual_assertion(expected_value, screenshot_paths)
    except Exception as exc:  # noqa: BLE001
        return AssertionResult(
            assertion.assertion_id,
            AppBehaviorStatus.UNKNOWN_EVIDENCE,
            f"visual verifier could not evaluate selected screenshots: {type(exc).__name__}: {exc}",
            expected_value,
            {
                **dict(evidence_payload),
                "visual_verifier": {
                    "enabled": True,
                    "status": "ERROR",
                    "error": f"{type(exc).__name__}: {exc}",
                    "screenshots": [str(path) for path in screenshot_paths],
                },
            },
        )
    visible = decision.get("visible")
    confidence = _confidence(decision)
    min_confidence = _visual_verifier_min_confidence()
    if visible is True and confidence >= min_confidence:
        return AssertionResult(
            assertion.assertion_id,
            AppBehaviorStatus.SATISFIED,
            "expected text is visible in selected post-action screenshot evidence",
            expected_value,
            {
                **dict(evidence_payload),
                "visual_verifier": {
                    "enabled": True,
                    "status": "VISIBLE",
                    "decision": dict(decision),
                    "screenshots": [str(path) for path in screenshot_paths],
                },
            },
        )
    if visible is False and confidence >= min_confidence:
        return AssertionResult(
            assertion.assertion_id,
            AppBehaviorStatus.VIOLATED,
            "expected text is not visible in selected post-action screenshot evidence",
            expected_value,
            {
                **dict(evidence_payload),
                "visual_verifier": {
                    "enabled": True,
                    "status": "NOT_VISIBLE",
                    "decision": dict(decision),
                    "screenshots": [str(path) for path in screenshot_paths],
                },
            },
        )
    return AssertionResult(
        assertion.assertion_id,
        AppBehaviorStatus.UNKNOWN_EVIDENCE,
        "visual verifier returned an inconclusive or low-confidence decision",
        expected_value,
        {
            **dict(evidence_payload),
            "visual_verifier": {
                "enabled": True,
                "status": "INCONCLUSIVE",
                "decision": dict(decision),
                "screenshots": [str(path) for path in screenshot_paths],
            },
        },
    )


def _evaluate_text_absent(
    assertion: ExpectedAssertion,
    expected_value: str | None,
    text_slice: TextEvidenceSlice,
    evidence_payload: Mapping[str, object],
) -> AssertionResult:
    if expected_value is None:
        return AssertionResult(
            assertion.assertion_id,
            AppBehaviorStatus.UNSUPPORTED,
            "TEXT_ABSENT requires expected_value or expected_value_ref",
            expected_value,
            evidence_payload,
        )
    negative_sufficiency = evidence_payload.get("negative_observation_sufficiency")
    present = text_contains(text_slice.texts, expected_value)
    if (
        not present
        and (
            not text_slice.evidence_sufficient
            or not isinstance(negative_sufficiency, Mapping)
            or negative_sufficiency.get("sufficient") is not True
        )
    ):
        return AssertionResult(
            assertion.assertion_id,
            AppBehaviorStatus.UNKNOWN_EVIDENCE,
            "selected evidence cannot confirm text absence",
            expected_value,
            evidence_payload,
        )
    return AssertionResult(
        assertion.assertion_id,
        AppBehaviorStatus.VIOLATED if present else AppBehaviorStatus.SATISFIED,
        "forbidden text is absent" if not present else "forbidden text is visible",
        expected_value,
        evidence_payload,
    )


def _offline_allows_visual_fallback(
    assertion: ExpectedAssertion,
    offline_assertion,
) -> bool:
    if assertion.type != "TEXT_VISIBLE" or not _visual_verifier_enabled():
        return False
    if offline_assertion.status not in {
        AppBehaviorStatus.VIOLATED,
        AppBehaviorStatus.UNKNOWN_EVIDENCE,
    }:
        return False
    evidence = (
        dict(offline_assertion.evidence)
        if isinstance(offline_assertion.evidence, Mapping)
        else {}
    )
    source = str(evidence.get("source") or "")
    if source.startswith("surface_not_reached") or source == "missing_after_step":
        return False
    reason = str(offline_assertion.reason or "").casefold()
    blocking_reason_markers = (
        "requires verification runner",
        "freshness requires",
        "matching text existed before",
        "invalid trace",
        "lacks selected source evidence",
        "input or clipboard overlay",
        "not a proven result surface",
    )
    return not any(marker in reason for marker in blocking_reason_markers)


def _text_slice(
    assertion: ExpectedAssertion,
    evidence: ExecutionEvidence,
    observation_policy: Mapping[str, object],
    *,
    verification_evidence: ExecutionEvidence | None = None,
) -> TextEvidenceSlice:
    if verification_evidence is not None:
        result = verification_evidence.observed_text_slice()
        return TextEvidenceSlice(
            source=f"verification_runner:{result.source}",
            texts=result.texts,
            frames=result.frames,
            evidence_sufficient=result.evidence_sufficient,
        )
    if assertion.after_step is not None or assertion.historical_match_not_sufficient:
        if assertion.after_step is None:
            return TextEvidenceSlice(
                source="missing_after_step",
                texts=(),
                evidence_sufficient=False,
            )
        return evidence.after_step_text_slice(assertion.after_step, observation_policy)
    return evidence.observed_text_slice()


def _visual_verifier_enabled() -> bool:
    value = os.getenv("APP_TEST_ENABLE_VLM_VERIFIER", "").strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    return bool(
        os.getenv("APP_TEST_VERIFIER_BASE_URL")
        or os.getenv("MOBIAGENT_VERIFIER_BASE_URL")
    )


def _visual_verifier_min_confidence() -> float:
    try:
        value = float(os.getenv("APP_TEST_VLM_VERIFIER_MIN_CONFIDENCE", "0.7"))
    except ValueError:
        return 0.7
    return max(0.0, min(1.0, value))


def _confidence(decision: Mapping[str, object]) -> float:
    try:
        return float(decision.get("confidence") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _screenshot_paths(
    execution: ExecutionRecord,
    text_slice: TextEvidenceSlice,
) -> tuple[Path, ...]:
    paths: list[Path] = []
    raw_trace_dir = Path(execution.raw_trace_dir) if execution.raw_trace_dir else None
    for frame in text_slice.frames:
        absolute = frame.get("screenshot_abs")
        candidate: Path | None = None
        if isinstance(absolute, str) and absolute:
            candidate = Path(absolute)
        else:
            relative = frame.get("screenshot")
            if raw_trace_dir is not None and isinstance(relative, str) and relative:
                candidate = raw_trace_dir / relative
        if candidate is not None and candidate.is_file():
            paths.append(candidate)
    return tuple(dict.fromkeys(paths))


def _model_visual_assertion(
    expected_value: str,
    screenshot_paths: tuple[Path, ...],
) -> Mapping[str, object]:
    config = model_config_from_env(
        base_url_names=(
            "APP_TEST_VERIFIER_BASE_URL",
            "MOBIAGENT_VERIFIER_BASE_URL",
            "MOBIAGENT_GROUNDER_BASE_URL",
            "MOBIAGENT_BASE_URL",
        ),
        model_names=(
            "APP_TEST_VERIFIER_MODEL",
            "MOBIAGENT_VERIFIER_MODEL",
            "MOBIAGENT_GROUNDER_MODEL",
            "MOBIAGENT_MODEL",
        ),
    )
    content: list[Mapping[str, object]] = [
        {
            "type": "text",
            "text": (
                "You are verifying an App test from post-action screenshots. "
                "Return JSON only. Decide whether the exact expected text is visibly present. "
                "Do not infer success from buttons, navigation, or runner status.\n"
                f"Expected exact text: {expected_value}\n"
                "Return exactly: {\"visible\": true|false|null, "
                "\"confidence\": number 0..1, \"reason\": short string, "
                "\"matched_text\": string}."
            ),
        }
    ]
    for path in screenshot_paths:
        suffix = path.suffix.lower()
        mime = "image/png" if suffix == ".png" else "image/jpeg"
        image_b64 = base64.b64encode(path.read_bytes()).decode("ascii")
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{image_b64}"},
            }
        )
    try:
        body = post_chat_completion(
            config,
            messages=[{"role": "user", "content": content}],
            max_tokens=256,
        )
    except ModelConfigError:
        raise
    parsed = extract_json_object(body)
    visible = parsed.get("visible")
    if visible not in (True, False, None):
        parsed = {**dict(parsed), "visible": None}
    return parsed
