from __future__ import annotations

from typing import Iterable, List, Optional, Sequence

from .models import (
    CriterionIR,
    CriterionObservation,
    CriterionResult,
    CriterionStatus,
    EvidencePointer,
    ObservationState,
    TemporalSemantics,
)


_NON_DECISIVE_STATES = {
    ObservationState.UNSTABLE_TRANSITION,
    ObservationState.DEGRADED,
    ObservationState.UNKNOWN,
}


def _ordered_observations(
    criterion: CriterionIR,
    observations: Iterable[CriterionObservation],
    window_start_frame: Optional[int],
    window_end_frame: Optional[int],
    window_start_timestamp: Optional[float],
    window_end_timestamp: Optional[float],
) -> List[CriterionObservation]:
    timestamp_window = (
        window_start_timestamp is not None or window_end_timestamp is not None
    )
    selected = []
    for observation in observations:
        if observation.criterion_id != criterion.criterion_id:
            continue
        if (
            window_start_frame is not None
            and observation.frame_index < window_start_frame
        ):
            continue
        if window_end_frame is not None and observation.frame_index > window_end_frame:
            continue
        if timestamp_window:
            timestamp = observation.evidence.timestamp if observation.evidence else None
            if timestamp is None:
                continue
            if (
                window_start_timestamp is not None
                and timestamp < window_start_timestamp
            ):
                continue
            if window_end_timestamp is not None and timestamp > window_end_timestamp:
                continue
        selected.append(observation)
    if timestamp_window:
        return sorted(
            selected,
            key=lambda item: (
                item.evidence.timestamp if item.evidence else float("inf")
            ),
        )
    return sorted(selected, key=lambda item: item.frame_index)


def _evidence(
    observations: Sequence[CriterionObservation],
) -> tuple[EvidencePointer, ...]:
    return tuple(
        observation.evidence
        for observation in observations
        if observation.evidence is not None
    )


def _unknown_result(
    criterion: CriterionIR,
    observations: Sequence[CriterionObservation],
    reason: str,
) -> CriterionResult:
    unsupported_only = bool(observations) and all(
        observation.status is CriterionStatus.UNSUPPORTED_CAPABILITY
        for observation in observations
    )
    source_missing = bool(observations) and any(
        observation.status is CriterionStatus.SOURCE_EVIDENCE_MISSING
        for observation in observations
    )
    return CriterionResult(
        criterion_id=criterion.criterion_id,
        temporal_semantics=criterion.temporal_semantics,
        status=(
            CriterionStatus.SOURCE_EVIDENCE_MISSING
            if source_missing
            else (
                CriterionStatus.UNSUPPORTED_CAPABILITY
                if unsupported_only
                else CriterionStatus.UNKNOWN_EVIDENCE
            )
        ),
        evidence=_evidence(observations),
        reason=reason,
        last_evaluated_frame=observations[-1].frame_index if observations else None,
    )


def aggregate_criterion(
    criterion: CriterionIR,
    observations: Iterable[CriterionObservation],
    *,
    window_start_frame: Optional[int] = None,
    window_end_frame: Optional[int] = None,
    window_start_timestamp: Optional[float] = None,
    window_end_timestamp: Optional[float] = None,
) -> CriterionResult:
    """Aggregate criterion evidence without deleting unstable/loading observations.

    Callers define the evaluation window (for example, declared-done through the
    frozen grace deadline). Unstable/degraded observations remain auditable but do
    not become success or violation evidence by themselves.
    """

    criterion.validate()
    ordered = _ordered_observations(
        criterion,
        observations,
        window_start_frame,
        window_end_frame,
        window_start_timestamp,
        window_end_timestamp,
    )
    if not ordered:
        return _unknown_result(
            criterion, ordered, "no observations in the evaluation window"
        )

    if criterion.temporal_semantics is TemporalSemantics.LATCHED_EVENT:
        return _aggregate_latched(criterion, ordered)
    if criterion.temporal_semantics is TemporalSemantics.PERSISTENT_STATE:
        return _aggregate_persistent(criterion, ordered)
    if criterion.temporal_semantics is TemporalSemantics.EVENTUAL_STATE:
        return _aggregate_eventual(criterion, ordered)
    return _aggregate_process(criterion, ordered)


def _aggregate_latched(
    criterion: CriterionIR,
    observations: Sequence[CriterionObservation],
) -> CriterionResult:
    latched: Optional[CriterionObservation] = None
    revocation: Optional[CriterionObservation] = None
    for observation in observations:
        if observation.observation_state in _NON_DECISIVE_STATES:
            continue
        if (
            observation.status is CriterionStatus.SATISFIED
            and observation.observation_state is not ObservationState.STABLE_LOADING
        ):
            latched = observation
        elif (
            observation.status is CriterionStatus.VIOLATED
            and observation.explicit_revocation
        ):
            revocation = observation

    if revocation is not None and (
        latched is None or revocation.frame_index >= latched.frame_index
    ):
        return CriterionResult(
            criterion_id=criterion.criterion_id,
            temporal_semantics=criterion.temporal_semantics,
            status=CriterionStatus.VIOLATED,
            evidence=_evidence((revocation,)),
            reason="latched event was explicitly revoked",
            first_satisfied_frame=latched.frame_index if latched else None,
            last_evaluated_frame=observations[-1].frame_index,
        )
    if latched is not None:
        return CriterionResult(
            criterion_id=criterion.criterion_id,
            temporal_semantics=criterion.temporal_semantics,
            status=CriterionStatus.SATISFIED,
            evidence=_evidence((latched,)),
            reason="strong event evidence latched",
            first_satisfied_frame=latched.frame_index,
            last_evaluated_frame=observations[-1].frame_index,
        )
    return _unknown_result(criterion, observations, "no decisive event evidence")


def _aggregate_persistent(
    criterion: CriterionIR,
    observations: Sequence[CriterionObservation],
) -> CriterionResult:
    current: Optional[CriterionObservation] = None
    first_satisfied: Optional[int] = None
    obscured = False
    decisive: List[CriterionObservation] = []

    for observation in observations:
        if observation.observation_state in _NON_DECISIVE_STATES:
            continue
        if observation.observation_state is ObservationState.OBSCURED_BUT_PERSISTENT:
            if (
                criterion.allow_obscured_persistence
                and current is not None
                and current.status is CriterionStatus.SATISFIED
            ):
                obscured = True
                decisive.append(observation)
            continue
        if observation.status is CriterionStatus.SATISFIED:
            if observation.observation_state is ObservationState.STABLE_LOADING:
                continue
            current = observation
            decisive.append(observation)
            first_satisfied = (
                first_satisfied
                if first_satisfied is not None
                else observation.frame_index
            )
            obscured = False
        elif observation.status is CriterionStatus.VIOLATED:
            current = observation
            decisive.append(observation)
            obscured = False

    if current is None:
        return _unknown_result(
            criterion, observations, "no decisive persistent-state evidence"
        )
    if current.status is CriterionStatus.VIOLATED:
        return CriterionResult(
            criterion_id=criterion.criterion_id,
            temporal_semantics=criterion.temporal_semantics,
            status=CriterionStatus.VIOLATED,
            evidence=_evidence(decisive),
            reason="persistent state was absent or explicitly left at the latest decisive observation",
            first_satisfied_frame=first_satisfied,
            last_evaluated_frame=observations[-1].frame_index,
        )
    return CriterionResult(
        criterion_id=criterion.criterion_id,
        temporal_semantics=criterion.temporal_semantics,
        status=CriterionStatus.SATISFIED,
        evidence=_evidence(decisive),
        reason=(
            "persistent state retained under a non-destructive overlay"
            if obscured
            else "persistent state retained"
        ),
        first_satisfied_frame=first_satisfied,
        last_evaluated_frame=observations[-1].frame_index,
        obscured_but_persistent=obscured,
    )


def _aggregate_eventual(
    criterion: CriterionIR,
    observations: Sequence[CriterionObservation],
) -> CriterionResult:
    decisive_violations: List[CriterionObservation] = []
    for observation in observations:
        if observation.observation_state in _NON_DECISIVE_STATES:
            continue
        if (
            observation.status is CriterionStatus.SATISFIED
            and observation.observation_state is not ObservationState.STABLE_LOADING
        ):
            return CriterionResult(
                criterion_id=criterion.criterion_id,
                temporal_semantics=criterion.temporal_semantics,
                status=CriterionStatus.SATISFIED,
                evidence=_evidence((observation,)),
                reason="eventual state reached within the evaluation window",
                first_satisfied_frame=observation.frame_index,
                last_evaluated_frame=observations[-1].frame_index,
            )
        if observation.status is CriterionStatus.VIOLATED:
            decisive_violations.append(observation)

    if decisive_violations:
        terminal = observations[-1]
        terminal_is_decisive_violation = (
            terminal.observation_state not in _NON_DECISIVE_STATES
            and terminal.observation_state is not ObservationState.STABLE_LOADING
            and terminal.status is CriterionStatus.VIOLATED
        )
        if not terminal_is_decisive_violation:
            return _unknown_result(
                criterion,
                observations,
                "eventual-state deadline ended without decisive terminal evidence",
            )
        return CriterionResult(
            criterion_id=criterion.criterion_id,
            temporal_semantics=criterion.temporal_semantics,
            status=CriterionStatus.VIOLATED,
            evidence=_evidence(decisive_violations),
            reason="deadline ended without reaching the eventual state",
            last_evaluated_frame=observations[-1].frame_index,
        )
    return _unknown_result(
        criterion, observations, "eventual-state evidence remained unknown"
    )


def _aggregate_process(
    criterion: CriterionIR,
    observations: Sequence[CriterionObservation],
) -> CriterionResult:
    decisive = [
        observation
        for observation in observations
        if observation.observation_state not in _NON_DECISIVE_STATES
        and observation.status in {CriterionStatus.SATISFIED, CriterionStatus.VIOLATED}
    ]
    violation = next(
        (
            observation
            for observation in decisive
            if observation.status is CriterionStatus.VIOLATED
        ),
        None,
    )
    if violation is not None:
        status = CriterionStatus.VIOLATED
        reason = "process obligation was violated"
    elif decisive:
        status = CriterionStatus.SATISFIED
        reason = "process obligation was satisfied"
    else:
        return _unknown_result(
            criterion, observations, "process evidence remained unknown"
        )
    return CriterionResult(
        criterion_id=criterion.criterion_id,
        temporal_semantics=criterion.temporal_semantics,
        status=status,
        evidence=_evidence(decisive),
        reason=reason,
        first_satisfied_frame=(
            next(
                (
                    observation.frame_index
                    for observation in decisive
                    if observation.status is CriterionStatus.SATISFIED
                ),
                None,
            )
        ),
        last_evaluated_frame=observations[-1].frame_index,
    )
