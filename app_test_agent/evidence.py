"""Deterministic evidence access helpers for App-test verification."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .executor import ExecutionRecord


@dataclass(frozen=True)
class TextEvidenceSlice:
    source: str
    texts: tuple[str, ...]
    frames: tuple[Mapping[str, Any], ...] = ()
    evidence_sufficient: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "texts": list(self.texts),
            "frames": [dict(frame) for frame in self.frames],
            "evidence_sufficient": self.evidence_sufficient,
        }


@dataclass(frozen=True)
class ObservationSufficiency:
    sufficient: bool
    reason: str
    expected_offsets_ms: tuple[int, ...]
    observed_offsets_ms: tuple[int, ...]
    missing_offsets_ms: tuple[int, ...]
    terminal_frame_id: int | None
    terminal_stability: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "sufficient": self.sufficient,
            "reason": self.reason,
            "expected_offsets_ms": list(self.expected_offsets_ms),
            "observed_offsets_ms": list(self.observed_offsets_ms),
            "missing_offsets_ms": list(self.missing_offsets_ms),
            "terminal_frame_id": self.terminal_frame_id,
            "terminal_stability": self.terminal_stability,
        }


@dataclass(frozen=True)
class FreshnessAssessment:
    required: bool
    proven: bool
    reason: str
    initial_count: int
    max_post_count: int
    proof_frame_ids: tuple[int, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "required": self.required,
            "proven": self.proven,
            "reason": self.reason,
            "initial_count": self.initial_count,
            "max_post_count": self.max_post_count,
            "proof_frame_ids": list(self.proof_frame_ids),
        }


def assess_negative_observation_sufficiency(
    text_slice: TextEvidenceSlice,
    observation_policy: Mapping[str, Any],
) -> ObservationSufficiency:
    """Require the configured delayed window before accepting absence.

    Positive presence remains decisive as soon as it is observed.  This check
    applies only when a verdict depends on text continuing to be absent.
    """

    max_wait = observation_policy.get("max_wait_ms")
    max_wait_ms = (
        max_wait
        if isinstance(max_wait, int) and not isinstance(max_wait, bool) and max_wait >= 0
        else None
    )
    raw_delays = observation_policy.get("delays_ms", ())
    delays = tuple(
        sorted(
            {
                value
                for value in raw_delays
                if isinstance(value, int)
                and not isinstance(value, bool)
                and value >= 0
                and (max_wait_ms is None or value <= max_wait_ms)
            }
        )
    ) if isinstance(raw_delays, (list, tuple)) else ()
    expected = delays or ((0,) if observation_policy.get("immediate") is True else ())
    observed_pairs = tuple(
        (int(frame["relative_to_action_ms"]), frame)
        for frame in text_slice.frames
        if isinstance(frame.get("relative_to_action_ms"), int)
        and not isinstance(frame.get("relative_to_action_ms"), bool)
        and int(frame["relative_to_action_ms"]) >= 0
        and (
            max_wait_ms is None
            or int(frame["relative_to_action_ms"]) <= max_wait_ms
        )
    )
    observed = tuple(sorted({offset for offset, _frame in observed_pairs}))
    missing = tuple(
        expected_offset
        for expected_offset in expected
        if not any(
            abs(observed_offset - expected_offset) <= _observation_offset_tolerance(expected_offset)
            for observed_offset in observed
        )
    )
    terminal_pair = max(observed_pairs, key=lambda item: item[0]) if observed_pairs else None
    terminal_frame = terminal_pair[1] if terminal_pair is not None else None
    terminal_frame_id = (
        int(terminal_frame["frame_id"])
        if isinstance(terminal_frame, Mapping)
        and isinstance(terminal_frame.get("frame_id"), int)
        and not isinstance(terminal_frame.get("frame_id"), bool)
        else None
    )
    terminal_stability = (
        str(terminal_frame.get("stability") or "UNKNOWN")
        if isinstance(terminal_frame, Mapping)
        else None
    )
    terminal_is_stable = _stable_terminal_value(terminal_stability)
    if not text_slice.frames:
        sufficient = False
        reason = "selected evidence contains no observation frames"
    elif not text_slice.evidence_sufficient:
        sufficient = False
        reason = "selected observation frames lack usable text evidence"
    elif not expected:
        sufficient = False
        reason = "observation policy declares no usable observation offset"
    elif not observed_pairs:
        sufficient = False
        reason = "observation frames lack relative_to_action_ms timing evidence"
    elif missing:
        sufficient = False
        reason = "configured delayed observation window is incomplete"
    elif not terminal_is_stable:
        sufficient = False
        reason = "terminal observation frame is not stably observable"
    else:
        sufficient = True
        reason = "configured delayed observation window is complete and terminal evidence is stable"
    return ObservationSufficiency(
        sufficient=sufficient,
        reason=reason,
        expected_offsets_ms=expected,
        observed_offsets_ms=observed,
        missing_offsets_ms=missing,
        terminal_frame_id=terminal_frame_id,
        terminal_stability=terminal_stability,
    )


def _observation_offset_tolerance(expected_offset_ms: int) -> int:
    return max(100, min(500, round(max(1, expected_offset_ms) * 0.2)))


def _stable_terminal_value(value: str | None) -> bool:
    normalized = str(value or "").strip().casefold()
    blockers = (
        "blocked",
        "changed",
        "degraded",
        "loading",
        "obscured",
        "transition",
        "unknown",
        "unstable",
    )
    return (
        bool(normalized)
        and "stable" in normalized
        and not any(blocker in normalized for blocker in blockers)
    )


class ExecutionEvidence:
    def __init__(self, execution: ExecutionRecord):
        self.execution = execution
        self.frames = tuple(
            frame
            for frame in execution.metadata.get("frames", ())
            if isinstance(frame, Mapping)
        )
        raw_frame_texts = execution.metadata.get("frame_visible_texts", {})
        self.frame_texts = (
            {
                int(frame_id): tuple(str(item) for item in texts if str(item))
                for frame_id, texts in raw_frame_texts.items()
                if str(frame_id).isdigit() and isinstance(texts, list)
            }
            if isinstance(raw_frame_texts, Mapping)
            else {}
        )

    def initial_texts(self) -> tuple[str, ...]:
        raw = self.execution.metadata.get("initial_visible_texts")
        if isinstance(raw, list):
            return tuple(str(item) for item in raw if str(item))
        first_pre_frame = next(
            (
                result.pre_frame
                for result in self.execution.step_results
                if result.pre_frame is not None
            ),
            None,
        )
        if first_pre_frame is not None:
            return self.texts_for_frame(first_pre_frame)
        return ()

    def texts_for_frame(self, frame_id: int) -> tuple[str, ...]:
        if frame_id in self.frame_texts:
            return self.frame_texts[frame_id]
        for frame in self.frames:
            if frame.get("frame_id") != frame_id:
                continue
            return _frame_texts(frame)
        return ()

    def after_step_text_slice(
        self,
        step_id: str,
        observation_policy: Mapping[str, Any],
    ) -> TextEvidenceSlice:
        result = next(
            (item for item in self.execution.step_results if item.step_id == step_id),
            None,
        )
        if result is None:
            return TextEvidenceSlice(
                source="missing_after_step",
                texts=(),
                evidence_sufficient=False,
            )
        max_wait_ms = observation_policy.get("max_wait_ms")
        selected_frames: list[Mapping[str, Any]] = []
        selected_texts: list[str] = []
        for frame_id in result.post_frames:
            frame = self._frame_for_id(frame_id)
            if frame is not None and isinstance(max_wait_ms, int):
                relative = frame.get("relative_to_action_ms")
                if isinstance(relative, int) and relative > max_wait_ms:
                    continue
            frame_texts = self.texts_for_frame(frame_id)
            if frame is not None:
                selected_frames.append(frame)
            elif frame_texts:
                selected_frames.append(
                    {
                        "frame_id": frame_id,
                        "visible_texts": list(frame_texts),
                        "stability": "UNKNOWN",
                    }
                )
            selected_texts.extend(frame_texts)
        return TextEvidenceSlice(
            source=f"after_step:{step_id}",
            texts=tuple(dict.fromkeys(selected_texts)),
            frames=tuple(selected_frames),
            evidence_sufficient=bool(selected_frames and selected_texts),
        )

    def observed_text_slice(self) -> TextEvidenceSlice:
        texts: list[str] = []
        frames = list(self.frames)
        for frame in self.frames:
            texts.extend(_frame_texts(frame))
        if texts:
            return TextEvidenceSlice(
                source="observation_frames",
                texts=tuple(dict.fromkeys(texts)),
                frames=tuple(frames),
                evidence_sufficient=True,
            )
        return TextEvidenceSlice(
            source="final_state_fallback",
            texts=self.execution.final_state.visible_texts,
            evidence_sufficient=self.execution.final_state.evidence_sufficient,
        )

    def _frame_for_id(self, frame_id: int) -> Mapping[str, Any] | None:
        for frame in self.frames:
            if frame.get("frame_id") == frame_id:
                return frame
        return None


def assess_text_freshness(
    evidence: ExecutionEvidence,
    text_slice: TextEvidenceSlice,
    expected_value: str,
    *,
    required: bool,
) -> FreshnessAssessment:
    """Prove that selected text belongs to the current action window.

    A post-action match is fresh when it was absent before the action, or when
    the selected frames contain more occurrences than the initial evidence.
    Persistent historical text without either proof must fail closed.
    """

    initial_count = _text_occurrence_count(evidence.initial_texts(), expected_value)
    post_counts: list[tuple[int, int]] = []
    for frame in text_slice.frames:
        frame_id = frame.get("frame_id")
        if not isinstance(frame_id, int) or isinstance(frame_id, bool):
            frame_id = frame.get("frame_index")
        if not isinstance(frame_id, int) or isinstance(frame_id, bool):
            continue
        frame_texts = evidence.texts_for_frame(frame_id) or _frame_texts(frame)
        post_counts.append(
            (frame_id, _text_occurrence_count(frame_texts, expected_value))
        )
    max_post_count = max((count for _frame_id, count in post_counts), default=0)
    matching_frame_ids = tuple(
        frame_id for frame_id, count in post_counts if count > 0
    )
    increased_frame_ids = tuple(
        frame_id for frame_id, count in post_counts if count > initial_count
    )
    if not required:
        return FreshnessAssessment(
            required=False,
            proven=bool(matching_frame_ids),
            reason="freshness proof is not required by the assertion",
            initial_count=initial_count,
            max_post_count=max_post_count,
            proof_frame_ids=matching_frame_ids,
        )
    if not matching_frame_ids:
        return FreshnessAssessment(
            required=True,
            proven=False,
            reason="selected post-action evidence contains no frame-bounded match",
            initial_count=initial_count,
            max_post_count=max_post_count,
            proof_frame_ids=(),
        )
    if initial_count == 0:
        return FreshnessAssessment(
            required=True,
            proven=True,
            reason="expected text was absent initially and appeared after the action",
            initial_count=0,
            max_post_count=max_post_count,
            proof_frame_ids=matching_frame_ids,
        )
    if increased_frame_ids:
        return FreshnessAssessment(
            required=True,
            proven=True,
            reason="post-action evidence contains an additional text occurrence",
            initial_count=initial_count,
            max_post_count=max_post_count,
            proof_frame_ids=increased_frame_ids,
        )
    return FreshnessAssessment(
        required=True,
        proven=False,
        reason=(
            "matching text already existed before the action and no additional "
            "post-action occurrence was proven"
        ),
        initial_count=initial_count,
        max_post_count=max_post_count,
        proof_frame_ids=(),
    )


def _text_occurrence_count(texts: tuple[str, ...], expected_value: str) -> int:
    if not expected_value:
        return 0
    return sum(str(item).count(expected_value) for item in texts)


def text_contains(haystack: tuple[str, ...], needle: str) -> bool:
    return any(needle in item for item in haystack)


def _frame_texts(frame: Mapping[str, Any]) -> tuple[str, ...]:
    values: list[str] = []
    for key in ("visible_texts", "ocr_texts"):
        raw = frame.get(key)
        if isinstance(raw, list):
            values.extend(str(item) for item in raw if str(item))
    return tuple(values)
