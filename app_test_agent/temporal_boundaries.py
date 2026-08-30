"""Unified audit boundaries for business and verification observations."""

from __future__ import annotations

from typing import Any, Mapping

from .executor import ExecutionRecord
from .offline_verifier import OfflineTraceReview
from .schema import TestCaseSpec
from .verification_runner import VerificationRunResult


TEMPORAL_BOUNDARIES_SCHEMA_VERSION = "app-test-temporal-boundaries-v1"


def build_temporal_boundaries(
    *,
    test_case: TestCaseSpec,
    execution: ExecutionRecord,
    verification_result: VerificationRunResult,
    business_offline_review: OfflineTraceReview,
    verification_offline_review: OfflineTraceReview | None,
) -> dict[str, Any]:
    verification_execution = verification_result.observation_record
    return {
        "schema_version": TEMPORAL_BOUNDARIES_SCHEMA_VERSION,
        "business_action_boundaries": _business_action_boundaries(
            test_case,
            execution,
        ),
        "runner_done_frame": _runner_done_frame(execution),
        "verification_runner_surface_reached_frame": _surface_reached_frame(
            verification_result,
            verification_execution,
        ),
        "result_observation_window": _result_observation_windows(
            business_offline_review,
            verification_offline_review,
        ),
    }


def _business_action_boundaries(
    test_case: TestCaseSpec,
    execution: ExecutionRecord,
) -> list[dict[str, Any]]:
    by_id = {item.step_id: item for item in execution.step_results}
    frames = _frame_map(execution)
    boundaries: list[dict[str, Any]] = []
    for step in test_case.steps:
        result = by_id.get(step.step_id)
        if result is None:
            boundaries.append(
                {
                    "step_id": step.step_id,
                    "status": "MISSING",
                    "boundary": None,
                }
            )
            continue
        post_ids = list(result.post_frames)
        boundary = {
            "pre_frame": _frame_ref(frames.get(result.pre_frame)),
            "first_post_frame": _frame_ref(frames.get(post_ids[0])) if post_ids else None,
            "last_post_frame": _frame_ref(frames.get(post_ids[-1])) if post_ids else None,
            "post_frame_ids": post_ids,
            "start_timestamp_ms": _frame_timestamp(frames.get(result.pre_frame)),
            "end_timestamp_ms": _frame_timestamp(
                frames.get(post_ids[-1]) if post_ids else None
            ),
            "source": "step_result.pre_frame_and_post_frames",
        }
        boundaries.append(
            {
                "step_id": step.step_id,
                "action_type": result.action_type,
                "attempts": result.attempts,
                "status": result.status,
                "action_ids": _action_ids(result.evidence),
                "boundary": boundary,
            }
        )
    return boundaries


def _runner_done_frame(execution: ExecutionRecord) -> dict[str, Any]:
    frames = _frame_map(execution)
    inferred_id = _last_execution_frame_id(execution)
    for result in execution.step_results:
        evidence = result.evidence if isinstance(result.evidence, Mapping) else {}
        goal_state = evidence.get("goal_state")
        completion_evidence = (
            goal_state.get("completion_evidence")
            if isinstance(goal_state, Mapping)
            else None
        )
        completion_frame_id = (
            completion_evidence.get("frame_id")
            if isinstance(completion_evidence, Mapping)
            else None
        )
        if (
            isinstance(goal_state, Mapping)
            and goal_state.get("status") == "COMPLETED"
            and isinstance(completion_evidence, Mapping)
            and completion_evidence.get("confirmed") is True
            and isinstance(completion_frame_id, int)
            and not isinstance(completion_frame_id, bool)
        ):
            return {
                "known": True,
                "explicit": True,
                "frame": _frame_ref(frames.get(completion_frame_id))
                or {"frame_id": completion_frame_id},
                "source": "goal_state.completion_evidence.frame_id",
                "inferred_terminal_frame": _frame_ref(frames.get(inferred_id)),
            }
        if not _done_emitted(evidence.get("model_decision")) and not _done_emitted(
            evidence.get("model_decisions")
        ):
            continue
        frame_id = (
            goal_state.get("frame_id")
            if isinstance(goal_state, Mapping)
            else None
        )
        if not isinstance(frame_id, int) or isinstance(frame_id, bool):
            frame_id = result.post_frames[-1] if result.post_frames else result.pre_frame
        return {
            "known": isinstance(frame_id, int) and not isinstance(frame_id, bool),
            "explicit": True,
            "frame": _frame_ref(frames.get(frame_id)),
            "source": "model_done_with_goal_state",
            "inferred_terminal_frame": _frame_ref(frames.get(inferred_id)),
        }
    return {
        "known": False,
        "explicit": False,
        "frame": None,
        "source": "runner_done_frame_not_explicitly_recorded",
        "inferred_terminal_frame": _frame_ref(frames.get(inferred_id)),
    }


def _surface_reached_frame(
    verification_result: VerificationRunResult,
    execution: ExecutionRecord | None,
) -> dict[str, Any]:
    if execution is None:
        return {
            "known": False,
            "frame": None,
            "source": "verification_observation_trace_unavailable",
        }
    frames = _frame_map(execution)
    for step in verification_result.step_results:
        if step.reached_surface is not True or not step.observation_frames:
            continue
        audited_frame = step.evidence.get("surface_reached_frame")
        frame_id = (
            audited_frame
            if isinstance(audited_frame, int) and not isinstance(audited_frame, bool)
            else step.observation_frames[0]
        )
        return {
            "known": True,
            "frame": _frame_ref(frames.get(frame_id))
            or {"frame_id": frame_id},
            "verification_step_id": step.verification_step_id,
            "source": "verification_step.reached_surface",
        }
    return {
        "known": False,
        "frame": None,
        "source": "verification_runner_did_not_report_surface_reached",
    }


def _result_observation_windows(
    business_review: OfflineTraceReview,
    verification_review: OfflineTraceReview | None,
) -> dict[str, Any]:
    return {
        "business_execution": _review_windows(business_review),
        "verification_observation": (
            _review_windows(verification_review)
            if verification_review is not None
            else []
        ),
    }


def _review_windows(review: OfflineTraceReview) -> list[dict[str, Any]]:
    windows: list[dict[str, Any]] = []
    for assertion in review.assertion_reviews:
        evidence = assertion.evidence
        raw_frames = evidence.get("frames") if isinstance(evidence, Mapping) else None
        frame_refs = [
            _frame_ref(frame) for frame in raw_frames if isinstance(frame, Mapping)
        ] if isinstance(raw_frames, list) else []
        frame_refs = [item for item in frame_refs if item is not None]
        windows.append(
            {
                "assertion_id": assertion.assertion_id,
                "source": evidence.get("source") if isinstance(evidence, Mapping) else None,
                "frame_ids": [item["frame_id"] for item in frame_refs if "frame_id" in item],
                "start_frame": frame_refs[0] if frame_refs else None,
                "end_frame": frame_refs[-1] if frame_refs else None,
                "start_timestamp_ms": (
                    frame_refs[0].get("timestamp_ms") if frame_refs else None
                ),
                "end_timestamp_ms": (
                    frame_refs[-1].get("timestamp_ms") if frame_refs else None
                ),
                "evidence_sufficient": (
                    evidence.get("evidence_sufficient")
                    if isinstance(evidence, Mapping)
                    else None
                ),
            }
        )
    return windows


def _frame_map(execution: ExecutionRecord) -> dict[int, Mapping[str, Any]]:
    frames = execution.metadata.get("frames")
    if not isinstance(frames, list):
        return {}
    return {
        int(frame["frame_id"]): frame
        for frame in frames
        if isinstance(frame, Mapping)
        and isinstance(frame.get("frame_id"), int)
        and not isinstance(frame.get("frame_id"), bool)
    }


def _last_execution_frame_id(execution: ExecutionRecord) -> int | None:
    values = [
        frame_id
        for result in execution.step_results
        for frame_id in result.post_frames
    ]
    return values[-1] if values else None


def _frame_ref(frame: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(frame, Mapping):
        return None
    frame_id = frame.get("frame_id")
    if not isinstance(frame_id, int) or isinstance(frame_id, bool):
        return None
    return {
        "frame_id": frame_id,
        "timestamp_ms": frame.get("timestamp_ms"),
        "relative_to_action_ms": frame.get("relative_to_action_ms"),
        "screenshot": frame.get("screenshot"),
        "hierarchy": frame.get("hierarchy"),
        "screenshot_sha256": frame.get("screenshot_sha256"),
        "hierarchy_sha256": frame.get("hierarchy_sha256"),
    }


def _frame_timestamp(frame: Mapping[str, Any] | None) -> int | None:
    value = frame.get("timestamp_ms") if isinstance(frame, Mapping) else None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _done_emitted(value: object) -> bool:
    if isinstance(value, Mapping):
        return str(value.get("action") or "").casefold() == "done"
    if isinstance(value, (list, tuple)):
        return any(_done_emitted(item) for item in value)
    return False


def _action_ids(value: object) -> list[int]:
    if not isinstance(value, Mapping):
        return []
    raw = value.get("action_ids")
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, int) and not isinstance(item, bool)]


__all__ = ["TEMPORAL_BOUNDARIES_SCHEMA_VERSION", "build_temporal_boundaries"]
