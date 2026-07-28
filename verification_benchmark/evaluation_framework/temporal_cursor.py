"""Pure, timestamp-first navigation over immutable criterion observations."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Tuple

from .event_log import CriterionObservationEvent, DurableEventTrace, TerminationEvent
from .models import CriterionObservation, ObservationState


class TemporalPhase(str, Enum):
    PROCESS = "PROCESS"
    GRACE = "GRACE"
    OUTSIDE = "OUTSIDE"


class TemporalWindowEnd(str, Enum):
    DEADLINE = "DEADLINE"
    ACTION_BOUNDARY = "ACTION_BOUNDARY"
    DECLARED_DONE = "DECLARED_DONE"
    GRACE_END = "GRACE_END"


class TemporalMatchKind(str, Enum):
    SEMANTIC = "SEMANTIC"
    GRACE_LOADING = "GRACE_LOADING"
    NOT_FOUND = "NOT_FOUND"


@dataclass(frozen=True)
class QualityTemporalWindow:
    phase: TemporalPhase
    start_timestamp: float
    end_timestamp: float
    end_reason: TemporalWindowEnd
    end_inclusive: bool

    def __post_init__(self) -> None:
        _finite_non_negative("window start_timestamp", self.start_timestamp)
        _finite_non_negative("window end_timestamp", self.end_timestamp)
        if self.end_timestamp < self.start_timestamp:
            raise ValueError("temporal window end cannot precede its start")
        if self.phase not in {TemporalPhase.PROCESS, TemporalPhase.GRACE}:
            raise ValueError("temporal window phase must be PROCESS or GRACE")
        if not isinstance(self.end_reason, TemporalWindowEnd):
            raise ValueError("temporal window end_reason must be a TemporalWindowEnd")
        if not isinstance(self.end_inclusive, bool):
            raise ValueError("temporal window end_inclusive must be boolean")


@dataclass(frozen=True)
class TemporalSeekResult:
    match_kind: TemporalMatchKind
    phase: TemporalPhase
    observation: Optional[CriterionObservation]
    skipped: Tuple[CriterionObservation, ...]
    window: QualityTemporalWindow


def _finite_non_negative(name: str, value: float) -> None:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        or value < 0
    ):
        raise ValueError(f"{name} must be a finite non-negative physical timestamp")


def _observation_timestamp(observation: CriterionObservation) -> float:
    if observation.evidence is None or observation.evidence.timestamp is None:
        raise ValueError(
            "quality-aware temporal cursor requires timestamps for every observation; "
            "frame_index fallback is forbidden"
        )
    timestamp = observation.evidence.timestamp
    _finite_non_negative("observation timestamp", timestamp)
    return float(timestamp)


@dataclass(frozen=True)
class QualityAwareTemporalCursor:
    """Immutable navigation surface; seek operations never retain a position."""

    observations: Tuple[CriterionObservation, ...]
    declared_done_timestamp: float
    grace_end_timestamp: Optional[float] = None

    def __post_init__(self) -> None:
        if not isinstance(self.observations, tuple):
            raise ValueError("observations must be an immutable tuple")
        if any(not isinstance(item, CriterionObservation) for item in self.observations):
            raise ValueError("observations must contain CriterionObservation values")
        _finite_non_negative("declared_done_timestamp", self.declared_done_timestamp)
        if self.grace_end_timestamp is not None:
            _finite_non_negative("grace_end_timestamp", self.grace_end_timestamp)
            if self.grace_end_timestamp < self.declared_done_timestamp:
                raise ValueError("grace_end_timestamp cannot precede declared_done_timestamp")
        timestamps = tuple(_observation_timestamp(item) for item in self.observations)
        if timestamps != tuple(sorted(timestamps)):
            raise ValueError("observation timestamps must be non-decreasing in event order")

    @classmethod
    def from_event_trace(cls, trace: DurableEventTrace) -> "QualityAwareTemporalCursor":
        trace.validate()
        termination = next(
            event for event in trace.events if isinstance(event, TerminationEvent)
        )
        if termination.declared_done_timestamp is None:
            raise ValueError(
                "timestamp-aware termination boundary is unavailable; frame fallback is forbidden"
            )
        observations = tuple(
            event.observation
            for event in trace.events
            if isinstance(event, CriterionObservationEvent)
        )
        return cls(
            observations=observations,
            declared_done_timestamp=termination.declared_done_timestamp,
            grace_end_timestamp=termination.grace_end_timestamp,
        )

    def phase_at(self, timestamp: float) -> TemporalPhase:
        _finite_non_negative("timestamp", timestamp)
        if timestamp <= self.declared_done_timestamp:
            return TemporalPhase.PROCESS
        if self.grace_end_timestamp is not None and timestamp <= self.grace_end_timestamp:
            return TemporalPhase.GRACE
        return TemporalPhase.OUTSIDE

    def make_window(
        self,
        phase: TemporalPhase,
        *,
        start_timestamp: float,
        duration_seconds: float,
        action_boundary_timestamp: Optional[float] = None,
    ) -> QualityTemporalWindow:
        _finite_non_negative("start_timestamp", start_timestamp)
        _finite_non_negative("duration_seconds", duration_seconds)
        if phase is TemporalPhase.PROCESS:
            if start_timestamp > self.declared_done_timestamp:
                raise ValueError("process window cannot start after declared-done")
            phase_end = self.declared_done_timestamp
            phase_reason = TemporalWindowEnd.DECLARED_DONE
        elif phase is TemporalPhase.GRACE:
            if self.grace_end_timestamp is None:
                raise ValueError("grace window requires a physical grace-end timestamp")
            if not self.declared_done_timestamp <= start_timestamp <= self.grace_end_timestamp:
                raise ValueError("grace window must start within declared-done/grace-end")
            phase_end = self.grace_end_timestamp
            phase_reason = TemporalWindowEnd.GRACE_END
        else:
            raise ValueError("cursor windows must be PROCESS or GRACE")

        candidates = [
            (start_timestamp + duration_seconds, TemporalWindowEnd.DEADLINE, 2),
            (phase_end, phase_reason, 1),
        ]
        if action_boundary_timestamp is not None:
            _finite_non_negative("action_boundary_timestamp", action_boundary_timestamp)
            if action_boundary_timestamp < start_timestamp:
                raise ValueError("action boundary cannot precede window start")
            candidates.append(
                (action_boundary_timestamp, TemporalWindowEnd.ACTION_BOUNDARY, 0)
            )
        end_timestamp, end_reason, _ = min(candidates, key=lambda item: (item[0], item[2]))
        return QualityTemporalWindow(
            phase=phase,
            start_timestamp=float(start_timestamp),
            end_timestamp=float(end_timestamp),
            end_reason=end_reason,
            end_inclusive=end_reason is not TemporalWindowEnd.ACTION_BOUNDARY,
        )

    def _validate_window(self, window: QualityTemporalWindow) -> None:
        if not isinstance(window, QualityTemporalWindow):
            raise ValueError("window must be a QualityTemporalWindow")
        if window.phase is TemporalPhase.PROCESS:
            if window.end_timestamp > self.declared_done_timestamp:
                raise ValueError("process window cannot cross declared-done")
            return
        if self.grace_end_timestamp is None:
            raise ValueError("grace window requires a physical grace-end timestamp")
        if (
            window.start_timestamp < self.declared_done_timestamp
            or window.end_timestamp > self.grace_end_timestamp
        ):
            raise ValueError("grace window cannot cross process/grace boundaries")

    def _window_observations(
        self,
        criterion_id: str,
        window: QualityTemporalWindow,
    ) -> Tuple[CriterionObservation, ...]:
        self._validate_window(window)
        if not isinstance(criterion_id, str) or not criterion_id.strip():
            raise ValueError("criterion_id must be non-empty")
        selected = []
        for observation in self.observations:
            if observation.criterion_id != criterion_id:
                continue
            timestamp = _observation_timestamp(observation)
            if timestamp < window.start_timestamp:
                continue
            if window.phase is TemporalPhase.GRACE and timestamp <= self.declared_done_timestamp:
                continue
            if window.end_inclusive:
                within_end = timestamp <= window.end_timestamp
            else:
                within_end = timestamp < window.end_timestamp
            if within_end:
                selected.append(observation)
        return tuple(selected)

    @staticmethod
    def _match_kind(
        observation: CriterionObservation,
        phase: TemporalPhase,
    ) -> Optional[TemporalMatchKind]:
        if observation.observation_state in {
            ObservationState.STABLE_SEMANTIC,
            ObservationState.OBSCURED_BUT_PERSISTENT,
        }:
            return TemporalMatchKind.SEMANTIC
        if (
            phase is TemporalPhase.GRACE
            and observation.observation_state is ObservationState.STABLE_LOADING
        ):
            return TemporalMatchKind.GRACE_LOADING
        return None

    def seek_next(
        self,
        criterion_id: str,
        window: QualityTemporalWindow,
        *,
        after_timestamp: Optional[float] = None,
    ) -> TemporalSeekResult:
        if after_timestamp is not None:
            _finite_non_negative("after_timestamp", after_timestamp)
        skipped = []
        for observation in self._window_observations(criterion_id, window):
            timestamp = _observation_timestamp(observation)
            if after_timestamp is not None and timestamp <= after_timestamp:
                continue
            match_kind = self._match_kind(observation, window.phase)
            if match_kind is not None:
                return TemporalSeekResult(
                    match_kind, window.phase, observation, tuple(skipped), window
                )
            skipped.append(observation)
        return TemporalSeekResult(
            TemporalMatchKind.NOT_FOUND, window.phase, None, tuple(skipped), window
        )

    def seek_previous(
        self,
        criterion_id: str,
        window: QualityTemporalWindow,
        *,
        before_timestamp: Optional[float] = None,
    ) -> TemporalSeekResult:
        if before_timestamp is not None:
            _finite_non_negative("before_timestamp", before_timestamp)
        skipped = []
        for observation in reversed(self._window_observations(criterion_id, window)):
            timestamp = _observation_timestamp(observation)
            if before_timestamp is not None and timestamp >= before_timestamp:
                continue
            match_kind = self._match_kind(observation, window.phase)
            if match_kind is not None:
                return TemporalSeekResult(
                    match_kind, window.phase, observation, tuple(skipped), window
                )
            skipped.append(observation)
        return TemporalSeekResult(
            TemporalMatchKind.NOT_FOUND, window.phase, None, tuple(skipped), window
        )
