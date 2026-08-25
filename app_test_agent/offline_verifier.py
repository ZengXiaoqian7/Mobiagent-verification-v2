"""App-test adapter for offline trace evidence review.

This module is the boundary between the App-test runner/oracle and the older
``verification_benchmark`` evidence infrastructure.  It produces evidence
findings only; it does not emit APP_PASS or APP_FAIL.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping, Sequence

from .evidence import (
    ExecutionEvidence,
    TextEvidenceSlice,
    assess_negative_observation_sufficiency,
    text_contains,
)
from .executor import ExecutionRecord
from .legacy_checker_adapter import review_with_legacy_checker
from .result_types import AppBehaviorStatus
from .schema import ExpectedAssertion, TestCaseSpec

from verification_benchmark.evaluation_framework.models import (
    ContractIR,
    CriterionIR,
    CriterionObservation,
    CriterionResult,
    CriterionStatus,
    EvidenceCapabilityProfile,
    EvidencePointer,
    ObservationState,
    OverlayKind,
    TerminationQuality,
    TemporalSemantics,
    TraceIntegrity,
)
from verification_benchmark.evaluation_framework.event_log import (
    CriterionObservationEvent,
    DurableEventTrace,
    TerminationEvent,
    contract_sha256 as verification_contract_sha256,
    event_trace_sha256,
)
from verification_benchmark.evaluation_framework.g1_observer import (
    G1ObservationPolicy,
    describe_g1_frame,
)
from verification_benchmark.evaluation_framework.replay import (
    REPLAY_ENGINE_VERSION,
    replay_event_trace,
)
from verification_benchmark.evaluation_framework.runner_source import G1FrameContext
from verification_benchmark.evaluation_framework.temporal import aggregate_criterion


OFFLINE_REVIEW_SCHEMA_VERSION = "app-test-offline-trace-review-v1"


class OfflineReviewStatus:
    COMPLETED = "COMPLETED"
    DEGRADED = "DEGRADED"
    INVALID_TRACE = "INVALID_TRACE"
    NOT_AVAILABLE = "NOT_AVAILABLE"


class OfflineTraceRole:
    BUSINESS_EXECUTION = "BUSINESS_EXECUTION"
    VERIFICATION_OBSERVATION = "VERIFICATION_OBSERVATION"


@dataclass(frozen=True)
class SurfaceEvidenceSpec:
    marker_candidates: tuple[str, ...] = ()
    required_shape_groups: tuple[tuple[str, ...], ...] = ()
    forbidden_context_candidates: tuple[str, ...] = ()
    context_candidates: tuple[str, ...] = ()

    @property
    def requires_page_shape(self) -> bool:
        return bool(self.required_shape_groups or self.context_candidates)

    def as_dict(self) -> dict[str, Any]:
        return {
            "marker_candidates": list(self.marker_candidates),
            "required_shape_groups": [
                list(group) for group in self.required_shape_groups
            ],
            "forbidden_context_candidates": list(self.forbidden_context_candidates),
            "context_candidates": list(self.context_candidates),
            "requires_page_shape": self.requires_page_shape,
        }


@dataclass(frozen=True)
class PageStateEvidence:
    frame_index: int
    observation_state: ObservationState
    overlay_kind: OverlayKind
    source: str
    evidence_mode: str
    surface_candidates: tuple[str, ...] = ()
    surface_marker_hits: tuple[str, ...] = ()
    surface_shape_requirements: tuple[tuple[str, ...], ...] = ()
    surface_shape_hits: tuple[tuple[str, ...], ...] = ()
    context_candidates: tuple[str, ...] = ()
    context_hits: tuple[str, ...] = ()
    forbidden_context_hits: tuple[str, ...] = ()
    loading_markers: tuple[str, ...] = ()
    overlay_markers: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def surface_shape_matched(self) -> bool:
        if not self.surface_shape_requirements:
            return True
        return len(self.surface_shape_hits) == len(self.surface_shape_requirements)

    @property
    def context_matched(self) -> bool:
        return not self.context_candidates or bool(self.context_hits)

    @property
    def surface_matched(self) -> bool:
        return (
            bool(self.surface_marker_hits)
            and self.surface_shape_matched
            and self.context_matched
            and not self.forbidden_context_hits
        )

    @property
    def decisive_for_result(self) -> bool:
        return self.observation_state is ObservationState.STABLE_SEMANTIC

    def as_dict(self) -> dict[str, Any]:
        return {
            "frame_index": self.frame_index,
            "observation_state": self.observation_state.value,
            "overlay_kind": self.overlay_kind.value,
            "source": self.source,
            "evidence_mode": self.evidence_mode,
            "surface_candidates": list(self.surface_candidates),
            "surface_marker_hits": list(self.surface_marker_hits),
            "surface_shape_requirements": [
                list(group) for group in self.surface_shape_requirements
            ],
            "surface_shape_hits": [list(group) for group in self.surface_shape_hits],
            "surface_shape_matched": self.surface_shape_matched,
            "context_candidates": list(self.context_candidates),
            "context_hits": list(self.context_hits),
            "context_matched": self.context_matched,
            "forbidden_context_hits": list(self.forbidden_context_hits),
            "loading_markers": list(self.loading_markers),
            "overlay_markers": list(self.overlay_markers),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class OfflineAssertionReview:
    assertion_id: str
    status: str
    reason: str
    expected_value: str | None = None
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "assertion_id": self.assertion_id,
            "status": self.status,
            "reason": self.reason,
            "expected_value": self.expected_value,
            "evidence": dict(self.evidence),
        }


@dataclass(frozen=True)
class OfflineTraceReview:
    role: str
    status: str
    reason: str
    assertion_reviews: tuple[OfflineAssertionReview, ...]
    contract_sha256: str | None = None
    trace_source: str | None = None
    trace_integrity: str | None = None
    capability_profile: Mapping[str, Any] = field(default_factory=dict)
    process_actions: tuple[Mapping[str, Any], ...] = ()
    outcome_frames: tuple[Mapping[str, Any], ...] = ()
    diagnostics: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    schema_version: str = OFFLINE_REVIEW_SCHEMA_VERSION

    @property
    def authoritative(self) -> bool:
        return self.status in {
            OfflineReviewStatus.COMPLETED,
            OfflineReviewStatus.DEGRADED,
        }

    def assertion(self, assertion_id: str) -> OfflineAssertionReview | None:
        return next(
            (item for item in self.assertion_reviews if item.assertion_id == assertion_id),
            None,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "role": self.role,
            "status": self.status,
            "reason": self.reason,
            "contract_sha256": self.contract_sha256,
            "trace_source": self.trace_source,
            "trace_integrity": self.trace_integrity,
            "authoritative": self.authoritative,
            "capability_profile": dict(self.capability_profile),
            "process_actions": [dict(item) for item in self.process_actions],
            "outcome_frames": [dict(item) for item in self.outcome_frames],
            "diagnostics": dict(self.diagnostics),
            "assertion_reviews": [item.as_dict() for item in self.assertion_reviews],
            "metadata": dict(self.metadata),
        }


def compile_offline_oracle_contract(test_case: TestCaseSpec) -> tuple[dict[str, Any], ...]:
    """Compile final expected results into offline evidence targets.

    The compiled contract is derived only from the user test case and runtime
    data.  It is not a runner route and does not add step-level expectations.
    """

    items: list[dict[str, Any]] = []
    for assertion in test_case.expected_results:
        items.append(
            {
                "assertion_id": assertion.assertion_id,
                "type": assertion.type,
                "target_surface": assertion.surface,
                "expected_text": assertion.resolved_value(test_case.test_data),
                "freshness_required": bool(assertion.historical_match_not_sufficient),
                "after_step": assertion.after_step,
                "requires_verification_runner": assertion.requires_verification_runner,
                "required": assertion.required,
                "allowed_evidence_sources": [
                    "business_execution_trace",
                    "verification_observation_trace",
                ],
            }
        )
    return tuple(items)


def review_app_test_trace(
    *,
    test_case: TestCaseSpec,
    execution: ExecutionRecord,
    contract: Any,
    role: str,
    verification_context: Mapping[str, Any] | None = None,
) -> OfflineTraceReview:
    """Review one business or verification observation trace.

    The review intentionally avoids runner diagnostic self-reports such as
    ``done`` as outcome evidence.  It uses frame text, hierarchy/screenshot
    availability, and final evidence sufficiency to decide whether the oracle
    has enough evidence to consume.
    """

    trace_payload = _trace_adapter_payload(execution.raw_trace_dir, role=role)
    evidence = ExecutionEvidence(execution)
    status, reason = _review_status(trace_payload, execution)
    reviews = tuple(
        _review_assertion(
            assertion,
            test_case,
            execution,
            evidence,
            contract,
            role=role,
            review_status=status,
            trace_payload=trace_payload,
            verification_context=verification_context,
        )
        for assertion in test_case.expected_results
    )
    return OfflineTraceReview(
        role=role,
        status=status,
        reason=reason,
        assertion_reviews=reviews,
        contract_sha256=contract.sha256,
        trace_source=trace_payload.get("trace_source"),
        trace_integrity=trace_payload.get("trace_integrity"),
        capability_profile=dict(trace_payload.get("capability_profile", {})),
        process_actions=tuple(
            item for item in trace_payload.get("process_actions", ()) if isinstance(item, Mapping)
        ),
        outcome_frames=tuple(
            item for item in trace_payload.get("outcome_frames", ()) if isinstance(item, Mapping)
        ),
        diagnostics=dict(trace_payload.get("diagnostics", {})),
        metadata={
            "execution_record_executor": execution.executor,
            "execution_record_has_frame_metadata": bool(evidence.frames),
            "execution_final_state_evidence_sufficient": execution.final_state.evidence_sufficient,
            "verification_context": dict(verification_context or {}),
            "pipeline_components": [
                "VERIFICATION_BENCHMARK_TRACE_ADAPTER",
                "APP_TEST_ASSERTION_EVIDENCE_ADAPTER",
                "VERIFICATION_BENCHMARK_TEMPORAL_AGGREGATION",
                "VERIFICATION_BENCHMARK_LEGACY_CHECKER_ADAPTER",
                "APP_TEST_ASSERTION_RESULT_ADAPTER",
            ],
        },
    )


def offline_review_from_mapping(value: Mapping[str, Any] | None) -> OfflineTraceReview | None:
    if not isinstance(value, Mapping):
        return None
    reviews = tuple(
        OfflineAssertionReview(
            assertion_id=str(item.get("assertion_id") or ""),
            status=str(item.get("status") or AppBehaviorStatus.UNKNOWN_EVIDENCE),
            reason=str(item.get("reason") or ""),
            expected_value=(
                str(item.get("expected_value"))
                if item.get("expected_value") is not None
                else None
            ),
            evidence=dict(item.get("evidence", {}))
            if isinstance(item.get("evidence"), Mapping)
            else {},
        )
        for item in value.get("assertion_reviews", ())
        if isinstance(item, Mapping)
    )
    return OfflineTraceReview(
        role=str(value.get("role") or ""),
        status=str(value.get("status") or OfflineReviewStatus.NOT_AVAILABLE),
        reason=str(value.get("reason") or ""),
        assertion_reviews=reviews,
        contract_sha256=(
            str(value.get("contract_sha256"))
            if value.get("contract_sha256") is not None
            else None
        ),
        trace_source=(
            str(value.get("trace_source"))
            if value.get("trace_source") is not None
            else None
        ),
        trace_integrity=(
            str(value.get("trace_integrity"))
            if value.get("trace_integrity") is not None
            else None
        ),
        capability_profile=dict(value.get("capability_profile", {}))
        if isinstance(value.get("capability_profile"), Mapping)
        else {},
        process_actions=tuple(
            dict(item) for item in value.get("process_actions", ()) if isinstance(item, Mapping)
        ),
        outcome_frames=tuple(
            dict(item) for item in value.get("outcome_frames", ()) if isinstance(item, Mapping)
        ),
        diagnostics=dict(value.get("diagnostics", {}))
        if isinstance(value.get("diagnostics"), Mapping)
        else {},
        metadata=dict(value.get("metadata", {}))
        if isinstance(value.get("metadata"), Mapping)
        else {},
    )


def _review_assertion(
    assertion: ExpectedAssertion,
    test_case: TestCaseSpec,
    execution: ExecutionRecord,
    evidence: ExecutionEvidence,
    contract: AppTestContract,
    *,
    role: str,
    review_status: str,
    trace_payload: Mapping[str, Any],
    verification_context: Mapping[str, Any] | None,
) -> OfflineAssertionReview:
    expected_value = assertion.resolved_value(test_case.test_data)
    text_slice = _text_slice(
        assertion,
        evidence,
        contract.observation_policy,
        role=role,
        verification_context=verification_context,
    )
    negative_sufficiency = assess_negative_observation_sufficiency(
        text_slice,
        contract.observation_policy,
    )
    surface_evidence = _surface_review_evidence(
        assertion,
        evidence,
        execution,
        role=role,
        verification_context=verification_context,
    )
    base_evidence = {
        **text_slice.as_dict(),
        "negative_observation_sufficiency": negative_sufficiency.as_dict(),
        "role": role,
        "review_status": review_status,
        "surface": assertion.surface,
        "after_step": assertion.after_step,
        "historical_match_not_sufficient": assertion.historical_match_not_sufficient,
        "requires_verification_runner": assertion.requires_verification_runner,
        "execution_final_state_evidence_sufficient": execution.final_state.evidence_sufficient,
        "verification_context": dict(verification_context or {}),
        **surface_evidence,
    }
    if review_status == OfflineReviewStatus.INVALID_TRACE:
        return OfflineAssertionReview(
            assertion.assertion_id,
            AppBehaviorStatus.UNKNOWN_EVIDENCE,
            "offline verifier could not trust the trace artifacts",
            expected_value,
            base_evidence,
        )
    legacy_review = review_with_legacy_checker(
        test_case=test_case,
        assertion=assertion,
        execution=execution,
    )
    if legacy_review is not None:
        base_evidence["verification_benchmark_legacy_checker"] = dict(legacy_review)
    criterion = _criterion_for_assertion(assertion)
    if criterion is not None:
        observations = _observations_for_assertion(
            assertion,
            expected_value,
            execution,
            evidence,
            text_slice,
            negative_observation_sufficient=negative_sufficiency.sufficient,
            negative_observation_reason=negative_sufficiency.reason,
            role=role,
        )
        result = aggregate_criterion(criterion, observations)
        replay_mirror, replay_result = _replay_mirror_for_assertion(
            assertion,
            criterion,
            observations,
            result,
            execution,
            evidence,
            role=role,
            review_status=review_status,
            trace_payload=trace_payload,
        )
        final_result = replay_result if replay_result is not None else result
        current_review = _assertion_review_from_criterion_result(
            assertion,
            expected_value,
            final_result,
            observations,
            base_evidence,
            replay_mirror=replay_mirror,
        )
        return _merge_legacy_checker_review(
            assertion,
            current_review,
            legacy_review,
            role=role,
        )
    return OfflineAssertionReview(
        assertion.assertion_id,
        AppBehaviorStatus.UNSUPPORTED,
        f"offline verifier does not support assertion type: {assertion.type}",
        expected_value,
        base_evidence,
    )


def _merge_legacy_checker_review(
    assertion: ExpectedAssertion,
    current: OfflineAssertionReview,
    legacy: Mapping[str, Any] | None,
    *,
    role: str,
) -> OfflineAssertionReview:
    """Use legacy visual evidence only when App-test safety gates allow it."""

    if legacy is None:
        return current
    evidence = {
        **dict(current.evidence),
        "verification_benchmark_legacy_checker": dict(legacy),
    }
    if not _legacy_checker_can_adjudicate(assertion, current, role=role):
        return replace(current, evidence=evidence)
    legacy_status = str(legacy.get("status") or "UNKNOWN_EVIDENCE").upper()
    if legacy_status not in {AppBehaviorStatus.SATISFIED, AppBehaviorStatus.VIOLATED}:
        return replace(current, evidence=evidence)
    current_evidence_insufficient = (
        current.evidence.get("evidence_sufficient") is False
        or current.evidence.get("execution_final_state_evidence_sufficient") is False
    )
    if current.status == AppBehaviorStatus.UNKNOWN_EVIDENCE or current_evidence_insufficient:
        return replace(
            current,
            status=legacy_status,
            reason=f"legacy visual checker: {legacy.get('reason') or 'decisive evidence'}",
            evidence=evidence,
        )
    if current.status in {AppBehaviorStatus.SATISFIED, AppBehaviorStatus.VIOLATED}:
        if current.status == legacy_status:
            return replace(current, evidence=evidence)
        return replace(
            current,
            status=AppBehaviorStatus.UNKNOWN_EVIDENCE,
            reason="App-test evidence conflicts with the legacy visual checker",
            evidence=evidence,
        )
    return replace(current, evidence=evidence)


def _legacy_checker_can_adjudicate(
    assertion: ExpectedAssertion,
    current: OfflineAssertionReview,
    *,
    role: str,
) -> bool:
    if assertion.requires_verification_runner and role != OfflineTraceRole.VERIFICATION_OBSERVATION:
        return False
    source = str(current.evidence.get("source") or "")
    if source.startswith("surface_not_reached") or source in {
        "missing_after_step",
        "verification_runner_required",
    }:
        return False
    return True


def _criterion_for_assertion(assertion: ExpectedAssertion) -> CriterionIR | None:
    if assertion.type == "TEXT_VISIBLE":
        return CriterionIR(
            assertion.assertion_id,
            TemporalSemantics.EVENTUAL_STATE,
            required=assertion.required,
            description="AppTest TEXT_VISIBLE evidence compiled for temporal aggregation",
        )
    if assertion.type == "TEXT_ABSENT":
        return CriterionIR(
            assertion.assertion_id,
            TemporalSemantics.PERSISTENT_STATE,
            required=assertion.required,
            description="AppTest TEXT_ABSENT evidence compiled for temporal aggregation",
        )
    if assertion.type in {"STATE_CHANGED", "SUCCESS_SIGNAL"}:
        return CriterionIR(
            assertion.assertion_id,
            TemporalSemantics.PROCESS_OBLIGATION,
            required=assertion.required,
            description=f"AppTest {assertion.type} evidence compiled for temporal aggregation",
        )
    return None


def _observations_for_assertion(
    assertion: ExpectedAssertion,
    expected_value: str | None,
    execution: ExecutionRecord,
    evidence: ExecutionEvidence,
    text_slice: TextEvidenceSlice,
    negative_observation_sufficient: bool,
    negative_observation_reason: str,
    *,
    role: str,
) -> tuple[CriterionObservation, ...]:
    if assertion.type in {"TEXT_VISIBLE", "TEXT_ABSENT"}:
        return _text_observations(
            assertion,
            expected_value,
            execution,
            evidence,
            text_slice,
            negative_observation_sufficient=negative_observation_sufficient,
            negative_observation_reason=negative_observation_reason,
            role=role,
        )
    if assertion.type == "STATE_CHANGED":
        return (
            _single_observation(
                assertion.assertion_id,
                frame_index=_terminal_frame_index(evidence, execution),
                status=_criterion_status_from_bool(execution.final_state.state_changed),
                evidence_sufficient=execution.final_state.evidence_sufficient,
                detail="execution final_state.state_changed",
            ),
        )
    if assertion.type == "SUCCESS_SIGNAL":
        if expected_value is None:
            observed = bool(execution.final_state.success_signals)
        else:
            observed = text_contains(execution.final_state.success_signals, expected_value)
        return (
            _single_observation(
                assertion.assertion_id,
                frame_index=_terminal_frame_index(evidence, execution),
                status=CriterionStatus.SATISFIED if observed else CriterionStatus.VIOLATED,
                evidence_sufficient=execution.final_state.evidence_sufficient,
                detail="execution final_state.success_signals",
            ),
        )
    return ()


def _text_observations(
    assertion: ExpectedAssertion,
    expected_value: str | None,
    execution: ExecutionRecord,
    evidence: ExecutionEvidence,
    text_slice: TextEvidenceSlice,
    negative_observation_sufficient: bool,
    negative_observation_reason: str,
    *,
    role: str,
) -> tuple[CriterionObservation, ...]:
    if expected_value is None:
        return (
            _single_observation(
                assertion.assertion_id,
                frame_index=_terminal_frame_index(evidence, execution),
                status=CriterionStatus.UNSUPPORTED_CAPABILITY,
                evidence_sufficient=False,
                detail=f"{assertion.type} requires expected_value or expected_value_ref",
            ),
        )
    if assertion.requires_verification_runner and role != OfflineTraceRole.VERIFICATION_OBSERVATION:
        return (
            _single_observation(
            assertion.assertion_id,
                frame_index=_terminal_frame_index(evidence, execution),
                status=CriterionStatus.UNKNOWN_EVIDENCE,
                evidence_sufficient=False,
                detail="assertion requires verification runner evidence from an independent observation trace",
            ),
        )
    if assertion.historical_match_not_sufficient and assertion.after_step is None:
        return (
            _single_observation(
            assertion.assertion_id,
                frame_index=_terminal_frame_index(evidence, execution),
                status=CriterionStatus.SOURCE_EVIDENCE_MISSING,
                evidence_sufficient=False,
                detail="freshness requires an after_step evidence boundary",
            ),
        )
    if (
        assertion.historical_match_not_sufficient
        and text_contains(evidence.initial_texts(), expected_value)
        and not text_contains(text_slice.texts, expected_value)
    ):
        return (
            _single_observation(
                assertion.assertion_id,
                frame_index=_terminal_frame_index(evidence, execution),
                status=CriterionStatus.UNKNOWN_EVIDENCE,
                evidence_sufficient=False,
                detail="matching text existed before the tested action and no fresh match was observed",
            ),
        )

    frames = text_slice.frames
    if not frames:
        return (
            _single_observation(
                assertion.assertion_id,
                frame_index=_terminal_frame_index(evidence, execution),
                status=CriterionStatus.SOURCE_EVIDENCE_MISSING,
                evidence_sufficient=False,
                detail="selected text evidence has no frame boundary",
            ),
        )
    observations: list[CriterionObservation] = []
    for frame in frames:
        frame_id = _frame_index(frame)
        page_state = _page_state_for_frame(
            frame,
            execution,
        )
        frame_texts = tuple(
            str(item)
            for item in (
                evidence.texts_for_frame(frame_id)
                if frame_id is not None
                else _frame_texts(frame)
            )
            if str(item)
        )
        if not frame_texts:
            frame_texts = _frame_texts(frame)
        present = text_contains(frame_texts, expected_value)
        if assertion.type == "TEXT_VISIBLE":
            overlay_only = present and _text_is_only_in_input_or_clipboard_overlay(
                frame,
                expected_value,
            )
            if overlay_only:
                status = CriterionStatus.UNKNOWN_EVIDENCE
                detail = (
                    "expected text is only present in an input or clipboard overlay, "
                    "not a proven result surface"
                )
            else:
                status = CriterionStatus.SATISFIED if present else CriterionStatus.VIOLATED
                detail = "expected text present in frame" if present else "expected text absent from frame"
            needs_negative_sufficiency = not present
        else:
            status = CriterionStatus.VIOLATED if present else CriterionStatus.SATISFIED
            detail = "forbidden text present in frame" if present else "forbidden text absent from frame"
            needs_negative_sufficiency = not present
        if not page_state.decisive_for_result:
            status = CriterionStatus.UNKNOWN_EVIDENCE
            detail = f"{detail}; page state is {page_state.observation_state.value}"
        elif needs_negative_sufficiency and not negative_observation_sufficient:
            detail = f"{detail}; {negative_observation_reason}"
        observations.append(
            _single_observation(
                assertion.assertion_id,
                frame_index=frame_id if frame_id is not None else len(observations),
                status=status,
                evidence_sufficient=(
                    text_slice.evidence_sufficient
                    and execution.final_state.evidence_sufficient
                    and (
                        not needs_negative_sufficiency
                        or negative_observation_sufficient
                    )
                ),
                detail=detail,
                observation_state=page_state.observation_state,
                overlay_kind=page_state.overlay_kind,
            )
        )
    return tuple(observations)


def _text_is_only_in_input_or_clipboard_overlay(
    frame: Mapping[str, Any],
    expected_value: str,
) -> bool:
    """Keep clipboard suggestions from masquerading as App result content."""
    raw_nodes = frame.get("xml_nodes")
    if not isinstance(raw_nodes, list):
        return False
    matching: list[Mapping[str, Any]] = []
    clipboard_nodes: list[Mapping[str, Any]] = []
    for node in raw_nodes:
        if not isinstance(node, Mapping):
            continue
        attributes = node.get("attributes")
        attributes = attributes if isinstance(attributes, Mapping) else {}
        values = [
            node.get("text"),
            node.get("semantic_text"),
            attributes.get("text"),
            attributes.get("originalText"),
            attributes.get("description"),
            attributes.get("hint"),
        ]
        joined = " ".join(str(value or "") for value in values)
        if expected_value in joined:
            matching.append(node)
        signature = " ".join(
            str(value or "")
            for value in (
                joined,
                attributes.get("id"),
                attributes.get("key"),
                attributes.get("type"),
            )
        ).casefold()
        if "clipboard" in signature or "来自剪贴板" in signature:
            clipboard_nodes.append(node)
    if not matching or not clipboard_nodes:
        return False
    overlay_bounds = [
        _bounds(node.get("bounds"))
        for node in clipboard_nodes
        if _bounds(node.get("bounds")) is not None
    ]
    overlay_bounds = [item for item in overlay_bounds if item is not None]
    if not overlay_bounds:
        return False
    return all(
        any(_nearby_bounds(_bounds(node.get("bounds")), overlay) for overlay in overlay_bounds)
        for node in matching
    )


def _bounds(value: Any) -> tuple[int, int, int, int] | None:
    if isinstance(value, (list, tuple)) and len(value) == 4:
        try:
            x1, y1, x2, y2 = (int(item) for item in value)
        except (TypeError, ValueError):
            return None
        return (x1, y1, x2, y2) if x2 >= x1 and y2 >= y1 else None
    return None


def _nearby_bounds(
    candidate: tuple[int, int, int, int] | None,
    overlay: tuple[int, int, int, int],
) -> bool:
    if candidate is None:
        return False
    x1, y1, x2, y2 = candidate
    ox1, oy1, ox2, oy2 = overlay
    horizontal_overlap = min(x2, ox2) >= max(x1, ox1)
    vertical_distance = max(0, max(oy1 - y2, y1 - oy2))
    return horizontal_overlap and vertical_distance <= 24


def _assertion_review_from_criterion_result(
    assertion: ExpectedAssertion,
    expected_value: str | None,
    result: CriterionResult,
    observations: tuple[CriterionObservation, ...],
    evidence_payload: Mapping[str, Any],
    *,
    replay_mirror: Mapping[str, Any] | None = None,
) -> OfflineAssertionReview:
    replay_diverged = (
        isinstance(replay_mirror, Mapping) and replay_mirror.get("matches") is False
    )
    status = (
        AppBehaviorStatus.UNKNOWN_EVIDENCE
        if replay_diverged
        else _app_status_from_criterion_status(result.status)
    )
    reason = (
        "offline replay consistency check diverged from direct temporal aggregation"
        if replay_diverged
        else _assertion_reason(assertion, result, observations)
    )
    full_evidence = {
        **dict(evidence_payload),
        "verification_benchmark_criterion": _criterion_result_payload(result),
        "verification_benchmark_observations": [
            _criterion_observation_payload(item) for item in observations
        ],
    }
    if replay_mirror is not None:
        full_evidence["verification_benchmark_replay_mirror"] = dict(replay_mirror)
    return OfflineAssertionReview(
        assertion.assertion_id,
        status,
        reason,
        expected_value,
        full_evidence,
    )


def _replay_mirror_for_assertion(
    assertion: ExpectedAssertion,
    criterion: CriterionIR,
    observations: tuple[CriterionObservation, ...],
    direct_result: CriterionResult,
    execution: ExecutionRecord,
    evidence: ExecutionEvidence,
    *,
    role: str,
    review_status: str,
    trace_payload: Mapping[str, Any],
) -> tuple[Mapping[str, Any], CriterionResult | None]:
    replay_criterion = (
        criterion if criterion.required else replace(criterion, required=True)
    )
    benchmark_contract = ContractIR.from_criteria(
        contract_id=f"app-test-offline-replay:{execution.test_case_id}:{assertion.assertion_id}",
        criteria=(replay_criterion,),
        source="app-test-offline-adapter",
        metadata={
            "adapter_schema_version": OFFLINE_REVIEW_SCHEMA_VERSION,
            "test_case_id": execution.test_case_id,
            "assertion_id": assertion.assertion_id,
            "app_test_assertion_required": assertion.required,
            "criterion_required_overridden_for_replay": not criterion.required,
            "role": role,
            "review_status": review_status,
        },
    )
    try:
        trace = _criterion_observation_replay_trace(
            execution,
            evidence,
            benchmark_contract,
            observations,
            role=role,
            review_status=review_status,
            trace_payload=trace_payload,
        )
        replay_report = replay_event_trace(benchmark_contract, trace)
        replay_result = next(
            (
                item
                for item in replay_report.criterion_results
                if item.criterion_id == criterion.criterion_id
            ),
            None,
        )
        if replay_result is None:
            return (
                {
                    "engine": REPLAY_ENGINE_VERSION,
                    "mode": "CRITERION_OBSERVATION_REPLAY",
                    "matches": False,
                    "error": "replay report did not contain the assertion criterion",
                    "contract_sha256": _safe_verification_contract_sha256(
                        benchmark_contract
                    ),
                    "event_trace_sha256": event_trace_sha256(trace),
                },
                None,
            )
        comparisons = _criterion_result_comparisons(direct_result, replay_result)
        return (
            {
                "engine": REPLAY_ENGINE_VERSION,
                "mode": "CRITERION_OBSERVATION_REPLAY",
                "matches": all(comparisons.values()),
                "comparisons": comparisons,
                "contract_id": benchmark_contract.contract_id,
                "contract_sha256": _safe_verification_contract_sha256(
                    benchmark_contract
                ),
                "event_trace_sha256": event_trace_sha256(trace),
                "trace_integrity": trace.capability_profile.integrity.value,
                "outcome_verdict": replay_report.outcome_verdict.value,
                "declared_done_frame": replay_report.declared_done_frame,
                "criterion": _criterion_result_payload(replay_result),
            },
            replay_result,
        )
    except Exception as exc:  # noqa: BLE001 - replay is a fail-closed consistency guard.
        return (
            {
                "engine": REPLAY_ENGINE_VERSION,
                "mode": "CRITERION_OBSERVATION_REPLAY",
                "matches": False,
                "error": f"{type(exc).__name__}: {exc}",
                "contract_sha256": _safe_verification_contract_sha256(
                    benchmark_contract
                ),
            },
            None,
        )


def _criterion_observation_replay_trace(
    execution: ExecutionRecord,
    evidence: ExecutionEvidence,
    contract: ContractIR,
    observations: tuple[CriterionObservation, ...],
    *,
    role: str,
    review_status: str,
    trace_payload: Mapping[str, Any],
) -> DurableEventTrace:
    events = [
        CriterionObservationEvent(sequence_index=index, observation=observation)
        for index, observation in enumerate(observations)
    ]
    terminal_frame = _terminal_frame_index(evidence, execution)
    events.append(
        TerminationEvent(
            sequence_index=len(events),
            quality=TerminationQuality.UNKNOWN,
            declared_done_frame=terminal_frame,
        )
    )
    trace = DurableEventTrace(
        trace_id=f"app-test-offline-replay:{execution.test_case_id}:{role}",
        contract_sha256=verification_contract_sha256(contract),
        capability_profile=_replay_capability_profile(
            execution,
            review_status=review_status,
            trace_payload=trace_payload,
        ),
        events=tuple(events),
    )
    trace.validate()
    return trace


def _replay_capability_profile(
    execution: ExecutionRecord,
    *,
    review_status: str,
    trace_payload: Mapping[str, Any],
) -> EvidenceCapabilityProfile:
    profile = trace_payload.get("capability_profile")
    profile_map = profile if isinstance(profile, Mapping) else {}
    integrity = _replay_trace_integrity(review_status, trace_payload)
    warnings = tuple(
        dict.fromkeys(
            [
                *(
                    str(item)
                    for item in profile_map.get("warnings", ())
                    if isinstance(profile_map.get("warnings"), list) and str(item)
                ),
                "app_test_offline_replay_uses_criterion_observation_events",
            ]
        )
    )
    return EvidenceCapabilityProfile(
        screenshot_frames=_sorted_int_tuple(profile_map.get("screenshot_frames", ())),
        hierarchy_raw_json_frames=_sorted_int_tuple(
            profile_map.get("hierarchy_raw_json_frames", ())
        ),
        hierarchy_xml_frames=_sorted_int_tuple(
            profile_map.get("hierarchy_xml_frames", ())
        ),
        action_count=_non_negative_int(
            profile_map.get("action_count"),
            default=len(execution.step_results),
        ),
        react_count=_non_negative_int(profile_map.get("react_count"), default=0),
        timestamp_sources=tuple(
            str(item)
            for item in profile_map.get("timestamp_sources", ())
            if isinstance(profile_map.get("timestamp_sources"), list) and str(item)
        ),
        integrity=integrity,
        corrupt_artifacts=tuple(
            str(item)
            for item in profile_map.get("corrupt_artifacts", ())
            if isinstance(profile_map.get("corrupt_artifacts"), list) and str(item)
        ),
        warnings=warnings,
    )


def _replay_trace_integrity(
    review_status: str,
    trace_payload: Mapping[str, Any],
) -> TraceIntegrity:
    raw = trace_payload.get("trace_integrity")
    if review_status == OfflineReviewStatus.INVALID_TRACE:
        return TraceIntegrity.INVALID
    if review_status == OfflineReviewStatus.DEGRADED:
        return TraceIntegrity.DEGRADED
    if raw == TraceIntegrity.VALID.value:
        return TraceIntegrity.VALID
    if raw == TraceIntegrity.DEGRADED.value:
        return TraceIntegrity.DEGRADED
    return TraceIntegrity.VALID


def _criterion_result_comparisons(
    direct: CriterionResult,
    replayed: CriterionResult,
) -> dict[str, bool]:
    return {
        "criterion_status": direct.status is replayed.status,
        "temporal_semantics": direct.temporal_semantics is replayed.temporal_semantics,
        "first_satisfied_frame": direct.first_satisfied_frame
        == replayed.first_satisfied_frame,
        "last_evaluated_frame": direct.last_evaluated_frame
        == replayed.last_evaluated_frame,
        "evidence_pointers": [
            _evidence_pointer_payload(item) for item in direct.evidence
        ]
        == [_evidence_pointer_payload(item) for item in replayed.evidence],
    }


def _criterion_result_payload(result: CriterionResult) -> dict[str, Any]:
    return {
        "criterion_id": result.criterion_id,
        "temporal_semantics": result.temporal_semantics.value,
        "status": result.status.value,
        "reason": result.reason,
        "first_satisfied_frame": result.first_satisfied_frame,
        "last_evaluated_frame": result.last_evaluated_frame,
        "obscured_but_persistent": result.obscured_but_persistent,
        "evidence": [_evidence_pointer_payload(pointer) for pointer in result.evidence],
    }


def _criterion_observation_payload(
    observation: CriterionObservation,
) -> dict[str, Any]:
    return {
        "criterion_id": observation.criterion_id,
        "status": observation.status.value,
        "frame_index": observation.frame_index,
        "observation_state": observation.observation_state.value,
        "overlay_kind": observation.overlay_kind.value,
        "explicit_revocation": observation.explicit_revocation,
        "evidence": (
            _evidence_pointer_payload(observation.evidence)
            if observation.evidence is not None
            else None
        ),
    }


def _evidence_pointer_payload(pointer: EvidencePointer) -> dict[str, Any]:
    return {
        "frame_index": pointer.frame_index,
        "source": pointer.source,
        "timestamp": pointer.timestamp,
        "detail": pointer.detail,
    }


def _safe_verification_contract_sha256(contract: ContractIR) -> str | None:
    try:
        return verification_contract_sha256(contract)
    except Exception:  # noqa: BLE001 - error details are recorded by the caller.
        return None


def _sorted_int_tuple(value: Any) -> tuple[int, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(
        sorted(
            {
                item
                for item in value
                if isinstance(item, int) and not isinstance(item, bool) and item >= 0
            }
        )
    )


def _non_negative_int(value: Any, *, default: int) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return default


def _single_observation(
    criterion_id: str,
    *,
    frame_index: int,
    status: CriterionStatus,
    evidence_sufficient: bool,
    detail: str,
    observation_state: ObservationState | None = None,
    overlay_kind: OverlayKind = OverlayKind.NONE,
) -> CriterionObservation:
    state = (
        observation_state
        if observation_state is not None
        else (
            ObservationState.STABLE_SEMANTIC
            if evidence_sufficient and status not in {
                CriterionStatus.UNKNOWN_EVIDENCE,
                CriterionStatus.SOURCE_EVIDENCE_MISSING,
                CriterionStatus.UNSUPPORTED_CAPABILITY,
            }
            else ObservationState.DEGRADED
        )
    )
    return CriterionObservation(
        criterion_id=criterion_id,
        status=(
            status
            if evidence_sufficient
            else (
                status
                if status in {
                    CriterionStatus.SOURCE_EVIDENCE_MISSING,
                    CriterionStatus.UNSUPPORTED_CAPABILITY,
                }
                else CriterionStatus.UNKNOWN_EVIDENCE
            )
        ),
        frame_index=frame_index,
        observation_state=state,
        overlay_kind=overlay_kind,
        evidence=EvidencePointer(
            frame_index=frame_index,
            source=f"frame:{frame_index}",
            detail=detail,
        ),
        explicit_revocation=status is CriterionStatus.VIOLATED,
    )


def _criterion_status_from_bool(value: bool | None) -> CriterionStatus:
    if value is True:
        return CriterionStatus.SATISFIED
    if value is False:
        return CriterionStatus.VIOLATED
    return CriterionStatus.UNKNOWN_EVIDENCE


def _app_status_from_criterion_status(status: CriterionStatus) -> str:
    if status is CriterionStatus.SATISFIED:
        return AppBehaviorStatus.SATISFIED
    if status is CriterionStatus.VIOLATED:
        return AppBehaviorStatus.VIOLATED
    if status is CriterionStatus.UNSUPPORTED_CAPABILITY:
        return AppBehaviorStatus.UNSUPPORTED
    return AppBehaviorStatus.UNKNOWN_EVIDENCE


def _assertion_reason(
    assertion: ExpectedAssertion,
    result: CriterionResult,
    observations: tuple[CriterionObservation, ...],
) -> str:
    if observations and all(
        item.status is CriterionStatus.UNSUPPORTED_CAPABILITY for item in observations
    ):
        if assertion.type in {"TEXT_VISIBLE", "TEXT_ABSENT"}:
            return f"{assertion.type} requires expected_value or expected_value_ref"
        return result.reason
    details = tuple(
        item.evidence.detail
        for item in observations
        if item.evidence is not None and item.evidence.detail
    )
    if any(
        detail == "assertion requires verification runner evidence from an independent observation trace"
        for detail in details
    ):
        return "assertion requires verification runner evidence from an independent observation trace"
    if any(
        detail == "freshness requires an after_step evidence boundary"
        for detail in details
    ):
        return "freshness requires an after_step evidence boundary"
    if any(
        detail == "matching text existed before the tested action and no fresh match was observed"
        for detail in details
    ):
        return "matching text existed before the tested action and no fresh match was observed"
    if result.status is CriterionStatus.SATISFIED:
        return {
            "TEXT_VISIBLE": "offline temporal evidence found expected text in the selected observation window",
            "TEXT_ABSENT": "offline temporal evidence confirmed text absence in the selected observation window",
            "STATE_CHANGED": "offline temporal evidence supports state change",
            "SUCCESS_SIGNAL": "offline temporal evidence contains success signal",
        }.get(assertion.type, result.reason)
    if result.status is CriterionStatus.VIOLATED:
        return {
            "TEXT_VISIBLE": "offline temporal evidence found sufficient selected evidence and expected text is absent",
            "TEXT_ABSENT": "offline temporal evidence found forbidden text",
            "STATE_CHANGED": "offline temporal evidence contradicts state change",
            "SUCCESS_SIGNAL": "offline temporal evidence lacks success signal",
        }.get(assertion.type, result.reason)
    if any(
        item.status is CriterionStatus.SOURCE_EVIDENCE_MISSING for item in observations
    ):
        return "offline verifier lacks selected source evidence"
    return result.reason or "offline temporal evidence is insufficient"


def _terminal_frame_index(
    evidence: ExecutionEvidence,
    execution: ExecutionRecord,
) -> int:
    for result in reversed(execution.step_results):
        if result.post_frames:
            return result.post_frames[-1]
        if result.pre_frame is not None:
            return result.pre_frame
    if evidence.frames:
        value = evidence.frames[-1].get("frame_id")
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return 0


def _frame_index(frame: Mapping[str, Any]) -> int | None:
    value = frame.get("frame_id")
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    value = frame.get("frame_index")
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _frame_texts(frame: Mapping[str, Any]) -> tuple[str, ...]:
    values: list[str] = []
    for key in ("visible_texts", "ocr_texts"):
        raw = frame.get(key)
        if isinstance(raw, list):
            values.extend(str(item) for item in raw if str(item))
    return tuple(values)


def _text_slice(
    assertion: ExpectedAssertion,
    evidence: ExecutionEvidence,
    observation_policy: Mapping[str, Any],
    *,
    role: str,
    verification_context: Mapping[str, Any] | None,
) -> TextEvidenceSlice:
    if role == OfflineTraceRole.VERIFICATION_OBSERVATION:
        result = evidence.observed_text_slice()
        scoped = TextEvidenceSlice(
            source=f"verification_observation:{result.source}",
            texts=result.texts,
            frames=result.frames,
            evidence_sufficient=result.evidence_sufficient,
        )
        if assertion.surface:
            return _surface_scoped_text_slice(
                assertion,
                evidence,
                scoped,
                execution=evidence.execution,
                verification_context=verification_context,
            )
        return scoped
    if assertion.after_step is not None or assertion.historical_match_not_sufficient:
        if assertion.after_step is None:
            return TextEvidenceSlice(
                source="missing_after_step",
                texts=(),
                evidence_sufficient=False,
            )
        scoped = evidence.after_step_text_slice(assertion.after_step, observation_policy)
        if assertion.surface:
            return _business_surface_scoped_text_slice(
                assertion,
                evidence,
                scoped,
                execution=evidence.execution,
            )
        return scoped
    return evidence.observed_text_slice()


def _business_surface_scoped_text_slice(
    assertion: ExpectedAssertion,
    evidence: ExecutionEvidence,
    text_slice: TextEvidenceSlice,
    *,
    execution: ExecutionRecord,
) -> TextEvidenceSlice:
    """Select only stable business frames that prove the declared surface."""

    surface_spec = _surface_evidence_spec(assertion, None)
    selected_frames = tuple(
        frame
        for frame in text_slice.frames
        if _page_state_for_frame(
            frame,
            execution,
            surface_spec=surface_spec,
        ).surface_matched
        and _page_state_for_frame(
            frame,
            execution,
            surface_spec=surface_spec,
        ).decisive_for_result
    )
    if not selected_frames:
        return TextEvidenceSlice(
            source=f"surface_not_reached:{assertion.surface}",
            texts=(),
            frames=(),
            evidence_sufficient=False,
        )
    selected_texts: list[str] = []
    for frame in selected_frames:
        frame_id = _frame_index(frame)
        frame_texts = (
            evidence.texts_for_frame(frame_id)
            if frame_id is not None
            else _frame_texts(frame)
        )
        if not frame_texts:
            frame_texts = _frame_texts(frame)
        selected_texts.extend(frame_texts)
    first_frame = _frame_index(selected_frames[0])
    return TextEvidenceSlice(
        source=f"business_surface:{assertion.surface}:from_frame:{first_frame}",
        texts=tuple(dict.fromkeys(selected_texts)),
        frames=selected_frames,
        evidence_sufficient=bool(
            text_slice.evidence_sufficient and selected_frames and selected_texts
        ),
    )


def _surface_scoped_text_slice(
    assertion: ExpectedAssertion,
    evidence: ExecutionEvidence,
    text_slice: TextEvidenceSlice,
    *,
    execution: ExecutionRecord,
    verification_context: Mapping[str, Any] | None,
) -> TextEvidenceSlice:
    surface_spec = _surface_evidence_spec(assertion, verification_context)
    reached_frame = _surface_reached_frame(
        verification_context,
        text_slice,
        execution,
        surface_spec=surface_spec,
    )
    if reached_frame is None:
        return TextEvidenceSlice(
            source=f"surface_not_reached:{assertion.surface}",
            texts=(),
            frames=(),
            evidence_sufficient=False,
        )
    selected_frames = tuple(
        frame
        for frame in text_slice.frames
        if (frame_id := _frame_index(frame)) is not None
        and frame_id >= reached_frame
        and _page_state_for_frame(
            frame,
            execution,
            surface_spec=surface_spec,
        ).decisive_for_result
    )
    selected_texts: list[str] = []
    for frame in selected_frames:
        frame_id = _frame_index(frame)
        frame_texts = (
            evidence.texts_for_frame(frame_id)
            if frame_id is not None
            else _frame_texts(frame)
        )
        if not frame_texts:
            frame_texts = _frame_texts(frame)
        selected_texts.extend(frame_texts)
    return TextEvidenceSlice(
        source=f"surface:{assertion.surface}:from_frame:{reached_frame}",
        texts=tuple(dict.fromkeys(selected_texts)),
        frames=selected_frames,
        evidence_sufficient=bool(
            text_slice.evidence_sufficient and selected_frames and selected_texts
        ),
    )


def _surface_reached_frame(
    verification_context: Mapping[str, Any] | None,
    text_slice: TextEvidenceSlice,
    execution: ExecutionRecord,
    *,
    surface_spec: SurfaceEvidenceSpec,
) -> int | None:
    if not isinstance(verification_context, Mapping) or not surface_spec.marker_candidates:
        return None
    step_results = verification_context.get("step_results")
    if not isinstance(step_results, list):
        return None
    frames_by_id = {
        _frame_index(frame): frame
        for frame in text_slice.frames
        if _frame_index(frame) is not None
    }
    for step in step_results:
        if not isinstance(step, Mapping) or step.get("reached_surface") is not True:
            continue
        frames = step.get("observation_frames")
        if not isinstance(frames, list):
            continue
        for frame in frames:
            if isinstance(frame, int) and not isinstance(frame, bool):
                candidate_frame = frames_by_id.get(frame)
                if candidate_frame is None:
                    continue
                page_state = _page_state_for_frame(
                    candidate_frame,
                    execution,
                    surface_spec=surface_spec,
                )
                if page_state.surface_matched and page_state.decisive_for_result:
                    return frame
    return None


def _surface_review_evidence(
    assertion: ExpectedAssertion,
    evidence: ExecutionEvidence,
    execution: ExecutionRecord,
    *,
    role: str,
    verification_context: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if not assertion.surface:
        return {}
    surface_spec = _surface_evidence_spec(assertion, verification_context)
    page_states = [
        _page_state_for_frame(frame, execution, surface_spec=surface_spec).as_dict()
        for frame in evidence.frames
    ]
    reached_frames = [
        item["frame_index"]
        for item in page_states
        if item["surface_marker_hits"]
        and item["surface_shape_matched"]
        and item["context_matched"]
        and not item["forbidden_context_hits"]
        and item["observation_state"] == ObservationState.STABLE_SEMANTIC.value
    ]
    return {
        "surface_evidence_role": role,
        "surface_evidence_spec": surface_spec.as_dict(),
        "surface_page_states": page_states,
        "surface_reached_frames": reached_frames,
    }


def _surface_evidence_spec(
    assertion: ExpectedAssertion,
    verification_context: Mapping[str, Any] | None,
) -> SurfaceEvidenceSpec:
    values: list[str] = []
    required_shape_groups: list[tuple[str, ...]] = []
    forbidden_context: list[str] = []
    context_candidates: list[str] = []
    if isinstance(verification_context, Mapping):
        for step in verification_context.get("step_results", ()):
            if not isinstance(step, Mapping):
                continue
            target = step.get("target")
            if not isinstance(target, Mapping):
                continue
            for key in ("surface_text_candidates", "text_candidates"):
                raw = target.get(key)
                if isinstance(raw, list):
                    values.extend(str(item).strip() for item in raw if str(item).strip())
            required_shape_groups.extend(_target_text_groups(target))
            forbidden_context.extend(
                _target_text_list(
                    target,
                    "forbidden_context_text_candidates",
                    "forbidden_surface_text_candidates",
                )
            )
            context_candidates.extend(_target_context_candidates(target))
            surface = target.get("surface")
            if isinstance(surface, str) and surface == assertion.surface:
                values.extend(_surface_name_candidates(surface))
                if _surface_shape_required(target):
                    required_shape_groups.extend(_surface_shape_groups(surface))
    values.extend(_surface_name_candidates(assertion.surface))
    surface_name = str(assertion.surface or "").casefold()
    if any(
        marker in surface_name
        for marker in (
            "result",
            "published",
            "feed",
            "timeline",
            "list",
            "detail",
            "post",
            "note",
            "结果",
            "发布后",
            "列表",
            "详情",
            "笔记",
            "帖子",
        )
    ) and not any(marker in surface_name for marker in ("editor", "compose", "编辑")):
        forbidden_context.extend(
            (
                "EditText",
                "RichEditor",
                "TextArea",
                "TextField",
                "编辑器",
                "编辑页",
                "请输入标题",
                "填写标题",
                "写点什么",
            )
        )
    required_shape_groups.extend(
        (candidate,)
        for candidate in context_candidates
        if candidate
    )
    return SurfaceEvidenceSpec(
        marker_candidates=tuple(dict.fromkeys(item for item in values if item)),
        required_shape_groups=_dedupe_groups(required_shape_groups),
        forbidden_context_candidates=tuple(
            dict.fromkeys(item for item in forbidden_context if item)
        ),
        context_candidates=tuple(dict.fromkeys(item for item in context_candidates if item)),
    )


def _surface_name_candidates(surface: str | None) -> tuple[str, ...]:
    text = str(surface or "").strip()
    folded = text.casefold()
    values: list[str] = []
    if any(term in folded for term in ("conversation", "chat", "message", "私信", "消息", "聊天")):
        values.extend(["Conversation", "Messages", "Chat", "消息", "聊天", "私信", "发消息"])
    if any(term in folded for term in ("feed", "timeline", "列表", "动态")):
        values.extend(["Feed", "列表", "动态"])
    if any(term in folded for term in ("post", "note", "笔记", "帖子", "内容")):
        values.extend(["Post", "Notes", "笔记", "帖子", "内容"])
    if any(term in folded for term in ("profile", "personal", "mine", "my", "own", "主页", "我的", "个人")):
        values.extend(["Profile", "Me", "Mine", "我", "我的", "个人主页"])
    if text and "_" not in text and len(text) <= 24:
        values.append(text)
    return tuple(dict.fromkeys(values))


def _surface_shape_groups(surface: str | None) -> tuple[tuple[str, ...], ...]:
    text = str(surface or "").strip()
    folded = text.casefold()
    groups: list[tuple[str, ...]] = []
    if any(
        term in folded
        for term in ("own_note", "profile_note", "profile_notes", "个人主页", "我的笔记")
    ):
        groups.append(("Profile", "Me", "Mine", "我", "我的", "个人主页"))
        groups.append(("Notes", "Posts", "笔记", "帖子", "内容"))
    elif any(term in folded for term in ("conversation", "chat", "message", "私信", "消息", "聊天")):
        groups.append(("Messages", "Chat", "消息", "聊天", "私信"))
    elif any(term in folded for term in ("profile", "personal", "mine", "my", "主页", "我的")):
        groups.append(("Profile", "Me", "Mine", "我", "我的", "个人主页"))
    return tuple(groups)


def _surface_shape_required(target: Mapping[str, Any]) -> bool:
    value = target.get("surface_shape_required")
    if isinstance(value, bool):
        return value
    value = target.get("require_surface_shape")
    return bool(value) if isinstance(value, bool) else False


def _target_text_groups(target: Mapping[str, Any]) -> list[tuple[str, ...]]:
    groups: list[tuple[str, ...]] = []
    for key in (
        "required_surface_text_groups",
        "surface_text_groups",
        "surface_shape_text_groups",
        "required_text_groups",
    ):
        raw = target.get(key)
        if not isinstance(raw, list):
            continue
        for group in raw:
            if isinstance(group, list):
                values = tuple(str(item).strip() for item in group if str(item).strip())
                if values:
                    groups.append(values)
            elif isinstance(group, str) and group.strip():
                groups.append((group.strip(),))
    return groups


def _target_text_list(target: Mapping[str, Any], *keys: str) -> list[str]:
    values: list[str] = []
    for key in keys:
        raw = target.get(key)
        if isinstance(raw, list):
            values.extend(str(item).strip() for item in raw if str(item).strip())
        elif isinstance(raw, str) and raw.strip():
            values.append(raw.strip())
    return values


def _target_context_candidates(target: Mapping[str, Any]) -> list[str]:
    values = _target_text_list(
        target,
        "contact_text_candidates",
        "conversation_text_candidates",
        "surface_context_text_candidates",
    )
    for key in ("contact_name", "conversation_with", "target_contact"):
        raw = target.get(key)
        if isinstance(raw, str) and raw.strip():
            values.append(raw.strip())
    context = target.get("surface_context")
    if isinstance(context, Mapping):
        for key in (
            "contact_name",
            "conversation_with",
            "target_contact",
            "display_name",
        ):
            raw = context.get(key)
            if isinstance(raw, str) and raw.strip():
                values.append(raw.strip())
        raw_candidates = context.get("text_candidates")
        if isinstance(raw_candidates, list):
            values.extend(
                str(item).strip() for item in raw_candidates if str(item).strip()
            )
    return values


def _dedupe_groups(groups: Sequence[Sequence[str]]) -> tuple[tuple[str, ...], ...]:
    deduped: list[tuple[str, ...]] = []
    seen: set[tuple[str, ...]] = set()
    for group in groups:
        normalized = tuple(dict.fromkeys(str(item).strip() for item in group if str(item).strip()))
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(normalized)
    return tuple(deduped)


def _page_state_for_frame(
    frame: Mapping[str, Any],
    execution: ExecutionRecord,
    *,
    surface_spec: SurfaceEvidenceSpec | None = None,
) -> PageStateEvidence:
    frame_index = _frame_index(frame)
    if frame_index is None:
        frame_index = 0
    if surface_spec is None:
        surface_spec = SurfaceEvidenceSpec()
    g1 = _g1_page_state_for_frame(frame, execution, frame_index, surface_spec)
    if g1 is not None:
        return g1
    return _metadata_page_state_for_frame(frame, frame_index, surface_spec)


def _g1_page_state_for_frame(
    frame: Mapping[str, Any],
    execution: ExecutionRecord,
    frame_index: int,
    surface_spec: SurfaceEvidenceSpec,
) -> PageStateEvidence | None:
    if not execution.raw_trace_dir:
        return None
    screenshot_ref = _artifact_ref(frame.get("screenshot"))
    hierarchy_ref = _artifact_ref(frame.get("hierarchy"))
    if screenshot_ref is None and hierarchy_ref is None:
        return None
    raw_json_ref = hierarchy_ref if hierarchy_ref and hierarchy_ref.endswith(".json") else None
    xml_ref = hierarchy_ref if hierarchy_ref and hierarchy_ref.endswith(".xml") else None
    try:
        descriptor = describe_g1_frame(
            execution.raw_trace_dir,
            G1FrameContext(
                frame_index=frame_index,
                previous_frame_index=None,
                pre_action_index=0,
                screenshot_ref=screenshot_ref,
                hierarchy_raw_json_ref=raw_json_ref,
                hierarchy_xml_ref=xml_ref,
                screenshot_size=None,
                artifacts=(),
                raw_context_complete=bool(screenshot_ref or hierarchy_ref),
                missing_context=(),
                observation_timestamp=_frame_timestamp(frame),
                timestamp_source="frame.timestamp_ms" if _frame_timestamp(frame) is not None else None,
            ),
        )
    except Exception:  # noqa: BLE001 - page-state checker is advisory.
        return None
    tokens = tuple(
        dict.fromkeys(
            (*descriptor.hierarchy.semantic_tokens, *_frame_semantic_tokens(frame))
        )
    )
    surface_hits = _marker_hits(surface_spec.marker_candidates, tokens)
    shape_hits = _shape_group_hits(surface_spec.required_shape_groups, tokens)
    context_hits = _marker_hits(surface_spec.context_candidates, tokens)
    forbidden_context_hits = _marker_hits(
        surface_spec.forbidden_context_candidates,
        tokens,
    )
    overlay_markers = tuple(
        dict.fromkeys(
            list(descriptor.hierarchy.app_overlay_markers)
            + list(descriptor.hierarchy.system_overlay_markers)
        )
    )
    observation_state = descriptor.observation_state
    if observation_state is ObservationState.UNKNOWN and tokens:
        observation_state = ObservationState.STABLE_SEMANTIC
    return PageStateEvidence(
        frame_index=frame_index,
        observation_state=observation_state,
        overlay_kind=descriptor.overlay_kind,
        source="verification_benchmark.g1_observer",
        evidence_mode=descriptor.evidence_mode.value,
        surface_candidates=surface_spec.marker_candidates,
        surface_marker_hits=surface_hits,
        surface_shape_requirements=surface_spec.required_shape_groups,
        surface_shape_hits=shape_hits,
        context_candidates=surface_spec.context_candidates,
        context_hits=context_hits,
        forbidden_context_hits=forbidden_context_hits,
        loading_markers=descriptor.hierarchy.loading_markers,
        overlay_markers=overlay_markers,
        warnings=descriptor.warnings,
    )


def _metadata_page_state_for_frame(
    frame: Mapping[str, Any],
    frame_index: int,
    surface_spec: SurfaceEvidenceSpec,
) -> PageStateEvidence:
    policy = G1ObservationPolicy()
    tokens = _frame_semantic_tokens(frame)
    loading = _marker_hits(policy.loading_markers, tokens)
    app_overlay = _marker_hits(policy.app_overlay_markers, tokens)
    system_overlay = _marker_hits(policy.system_overlay_markers, tokens)
    shape_hits = _shape_group_hits(surface_spec.required_shape_groups, tokens)
    context_hits = _marker_hits(surface_spec.context_candidates, tokens)
    forbidden_context_hits = _marker_hits(
        surface_spec.forbidden_context_candidates,
        tokens,
    )
    if system_overlay:
        overlay = OverlayKind.SYSTEM_DIALOG
        state = ObservationState.DEGRADED
    elif app_overlay:
        overlay = OverlayKind.APP_MODAL
        state = ObservationState.DEGRADED
    elif loading:
        overlay = OverlayKind.NONE
        state = ObservationState.STABLE_LOADING
    elif tokens:
        overlay = OverlayKind.NONE
        state = ObservationState.STABLE_SEMANTIC
    else:
        overlay = OverlayKind.NONE
        state = ObservationState.UNKNOWN
    return PageStateEvidence(
        frame_index=frame_index,
        observation_state=state,
        overlay_kind=overlay,
        source="execution_record.frame_metadata",
        evidence_mode="METADATA_TEXT",
        surface_candidates=surface_spec.marker_candidates,
        surface_marker_hits=_marker_hits(surface_spec.marker_candidates, tokens),
        surface_shape_requirements=surface_spec.required_shape_groups,
        surface_shape_hits=shape_hits,
        context_candidates=surface_spec.context_candidates,
        context_hits=context_hits,
        forbidden_context_hits=forbidden_context_hits,
        loading_markers=loading,
        overlay_markers=tuple(dict.fromkeys(list(app_overlay) + list(system_overlay))),
    )


def _marker_hits(candidates: Sequence[str], tokens: Sequence[str]) -> tuple[str, ...]:
    folded_tokens = "\n".join(str(item) for item in tokens).casefold()
    return tuple(
        dict.fromkeys(
            str(candidate)
            for candidate in candidates
            if str(candidate).strip()
            and str(candidate).casefold() in folded_tokens
        )
    )


def _frame_semantic_tokens(frame: Mapping[str, Any]) -> tuple[str, ...]:
    tokens = list(_frame_texts(frame))
    raw_nodes = frame.get("xml_nodes")
    if isinstance(raw_nodes, list):
        for node in raw_nodes:
            if not isinstance(node, Mapping):
                continue
            attributes = node.get("attributes")
            attributes = attributes if isinstance(attributes, Mapping) else {}
            tokens.extend(
                str(value)
                for value in (
                    node.get("text"),
                    node.get("semantic_text"),
                    attributes.get("text"),
                    attributes.get("originalText"),
                    attributes.get("description"),
                    attributes.get("hint"),
                    attributes.get("id"),
                    attributes.get("key"),
                    attributes.get("type"),
                    attributes.get("class"),
                    attributes.get("resource-id"),
                )
                if value not in (None, "")
            )
    return tuple(dict.fromkeys(tokens))


def _shape_group_hits(
    groups: Sequence[Sequence[str]],
    tokens: Sequence[str],
) -> tuple[tuple[str, ...], ...]:
    hits: list[tuple[str, ...]] = []
    for group in groups:
        group_hits = _marker_hits(tuple(group), tokens)
        if group_hits:
            hits.append(group_hits)
    return tuple(hits)


def _artifact_ref(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip() or "://" in value:
        return None
    return value


def _frame_timestamp(frame: Mapping[str, Any]) -> float | None:
    value = frame.get("timestamp_ms")
    if isinstance(value, int) and not isinstance(value, bool):
        return value / 1000.0
    return None


def _review_status(
    trace_payload: Mapping[str, Any],
    execution: ExecutionRecord,
) -> tuple[str, str]:
    if trace_payload.get("adapter_error"):
        if execution.metadata.get("frames") or execution.final_state.evidence_sufficient:
            return (
                OfflineReviewStatus.DEGRADED,
                f"raw trace adapter failed; using execution record metadata: {trace_payload['adapter_error']}",
            )
        return (
            OfflineReviewStatus.INVALID_TRACE,
            f"verification_benchmark trace adapter rejected raw trace: {trace_payload['adapter_error']}",
        )
    integrity = trace_payload.get("trace_integrity")
    if integrity == "INVALID":
        if execution.metadata.get("frames") or execution.final_state.evidence_sufficient:
            return (
                OfflineReviewStatus.DEGRADED,
                "raw trace integrity is INVALID; using execution record metadata",
            )
        return OfflineReviewStatus.INVALID_TRACE, "raw trace integrity is INVALID"
    if integrity == "DEGRADED":
        return OfflineReviewStatus.DEGRADED, "raw trace was loaded with degraded evidence capability"
    if integrity == "VALID":
        return OfflineReviewStatus.COMPLETED, "raw trace was loaded by verification_benchmark trace adapter"
    if execution.metadata.get("frames") or execution.final_state.evidence_sufficient:
        return OfflineReviewStatus.COMPLETED, "execution record metadata provided offline evidence"
    return OfflineReviewStatus.NOT_AVAILABLE, "no raw trace or frame evidence is available for offline review"


def _trace_adapter_payload(raw_trace_dir: str | None, *, role: str) -> dict[str, Any]:
    if not raw_trace_dir:
        return {}
    trace_dir = Path(raw_trace_dir)
    try:
        from verification_benchmark.evaluation_framework.trace_adapter import (
            load_trace_directory,
        )

        bundle = load_trace_directory(trace_dir, trace_ref=role)
    except Exception as exc:  # noqa: BLE001 - fail closed into review metadata.
        return {
            "trace_source": str(trace_dir),
            "adapter_error": f"{type(exc).__name__}: {exc}",
        }
    profile = bundle.capability_profile
    return {
        "trace_source": str(trace_dir),
        "trace_integrity": profile.integrity.value,
        "capability_profile": {
            "screenshot_frames": list(profile.screenshot_frames),
            "hierarchy_raw_json_frames": list(profile.hierarchy_raw_json_frames),
            "hierarchy_xml_frames": list(profile.hierarchy_xml_frames),
            "action_count": profile.action_count,
            "react_count": profile.react_count,
            "timestamp_sources": list(profile.timestamp_sources),
            "available_capabilities": sorted(item.value for item in profile.available),
            "corrupt_artifacts": list(profile.corrupt_artifacts),
            "warnings": list(profile.warnings),
        },
        "process_actions": [
            {
                "action_index": item.action_index,
                "action_type": item.action_type,
                "screenshot_size": list(item.screenshot_size)
                if item.screenshot_size is not None
                else None,
                "click_coordinate_size": list(item.click_coordinate_size)
                if item.click_coordinate_size is not None
                else None,
            }
            for item in bundle.process_actions
        ],
        "outcome_frames": [
            {
                "frame_index": item.frame_index,
                "screenshot_ref": item.screenshot_ref,
                "hierarchy_raw_json_ref": item.hierarchy_raw_json_ref,
                "hierarchy_xml_ref": item.hierarchy_xml_ref,
            }
            for item in bundle.outcome_frames
        ],
        "diagnostics": {
            "react_ref": bundle.diagnostics.react_ref,
            "react_count": bundle.diagnostics.react_count,
            "excluded_field_names": list(bundle.diagnostics.excluded_field_names),
            "declared_done_action_index": bundle.diagnostics.declared_done_action_index,
            "diagnostic_fields_excluded_from_outcome": True,
        },
    }
