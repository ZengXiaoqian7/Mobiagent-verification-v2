"""Deterministic evaluation shared by in-memory streams and durable replay."""

from __future__ import annotations

from typing import Iterable, Optional

from .aggregation import aggregate_contract
from .event_log import (
    CriterionObservationEvent,
    DurableEventTrace,
    FrameEvidenceEvent,
    TerminationEvent,
    contract_sha256,
)
from .models import ContractIR, CriterionObservation, CriterionResult, RunReport, RunVerdict
from .temporal import aggregate_criterion


REPLAY_ENGINE_VERSION = "harmony-eval-replay-v1"


def _termination(trace: DurableEventTrace) -> TerminationEvent:
    return next(event for event in trace.events if isinstance(event, TerminationEvent))


def _observations(trace: DurableEventTrace) -> tuple[CriterionObservation, ...]:
    return tuple(
        event.observation
        for event in trace.events
        if isinstance(event, CriterionObservationEvent)
    )


def _last_frame(trace: DurableEventTrace, observations: Iterable[CriterionObservation]) -> Optional[int]:
    indices = [observation.frame_index for observation in observations]
    indices.extend(
        event.frame_index for event in trace.events if isinstance(event, FrameEvidenceEvent)
    )
    termination = _termination(trace)
    if termination.declared_done_frame is not None:
        indices.append(termination.declared_done_frame)
    return max(indices) if indices else None


def _criterion_results(
    contract: ContractIR,
    observations: tuple[CriterionObservation, ...],
    *,
    window_end_frame: Optional[int],
    window_end_timestamp: Optional[float] = None,
) -> tuple[CriterionResult, ...]:
    return tuple(
        aggregate_criterion(
            criterion,
            observations,
            window_end_frame=window_end_frame,
            window_end_timestamp=window_end_timestamp,
        )
        for criterion in contract.criteria
    )


def _outcome_at(
    contract: ContractIR,
    trace: DurableEventTrace,
    observations: tuple[CriterionObservation, ...],
    frame: Optional[int],
    timestamp: Optional[float] = None,
) -> Optional[RunVerdict]:
    if frame is None and timestamp is None:
        return None
    results = _criterion_results(
        contract,
        observations,
        window_end_frame=frame if timestamp is None else None,
        window_end_timestamp=timestamp,
    )
    return aggregate_contract(contract, results, trace.capability_profile).outcome_verdict


def replay_event_trace(contract: ContractIR, trace: DurableEventTrace) -> RunReport:
    """Evaluate either an in-memory or checksum-verified decoded event trace."""

    trace.validate()
    expected_contract_hash = contract_sha256(contract)
    if trace.contract_sha256 != expected_contract_hash:
        raise ValueError("event trace contract hash does not match the supplied ContractIR")

    observations = _observations(trace)
    contract_criterion_ids = {criterion.criterion_id for criterion in contract.criteria}
    unexpected_criterion_ids = sorted(
        {observation.criterion_id for observation in observations} - contract_criterion_ids
    )
    if unexpected_criterion_ids:
        raise ValueError(
            f"event trace contains criteria not present in contract: {unexpected_criterion_ids}"
        )
    termination = _termination(trace)
    timestamp_mode = termination.declared_done_timestamp is not None
    final_timestamp = None
    if timestamp_mode:
        final_timestamp = (
            termination.grace_end_timestamp
            if termination.grace_end_timestamp is not None
            else termination.declared_done_timestamp
        )
        final_frame = None
    else:
        final_frame = (
            termination.grace_end_frame
            if termination.grace_end_frame is not None
            else _last_frame(trace, observations)
        )
    final_results = _criterion_results(
        contract,
        observations,
        window_end_frame=final_frame,
        window_end_timestamp=final_timestamp,
    )
    outcome_at_declared_done = _outcome_at(
        contract,
        trace,
        observations,
        termination.declared_done_frame if not timestamp_mode else None,
        termination.declared_done_timestamp,
    )
    outcome_after_grace = _outcome_at(
        contract,
        trace,
        observations,
        termination.grace_end_frame if not timestamp_mode else None,
        termination.grace_end_timestamp,
    )
    return aggregate_contract(
        contract,
        final_results,
        trace.capability_profile,
        termination_quality=termination.quality,
        mode=trace.mode,
        outcome_at_declared_done=outcome_at_declared_done,
        outcome_after_grace=outcome_after_grace,
        declared_done_frame=termination.declared_done_frame,
    )
