"""One-way ContractIR ROI/checker assembly into typed criterion observations."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Optional, Tuple

from .event_log import (
    CriterionObservationEvent,
    DurableEventTrace,
    FrameEvidenceEvent,
    TerminationEvent,
    contract_sha256,
)
from .g1_observer import (
    G1FrameDescriptor,
    G1ObservationPolicy,
    G1PairComparison,
    compare_g1_frames,
    describe_g1_frame,
)
from .models import (
    ContractIR,
    ContractRoiIR,
    CriterionObservation,
    CriterionStatus,
    EvidencePointer,
    G1CheckerKind,
    G1CriterionBindingIR,
    ObservationState,
    OverlayKind,
    RoiCoordinateSpace,
)
from .runner_source import G1FrameContext, G1RegionOfInterest


@dataclass(frozen=True)
class G1RoiMapping:
    criterion_id: str
    roi_id: str
    coordinate_space: RoiCoordinateSpace
    contract_bounds: Tuple[float, float, float, float]
    reference_size: Optional[Tuple[int, int]]
    screenshot_size: Tuple[int, int]
    pixel_bounds: Tuple[int, int, int, int]
    region: G1RegionOfInterest


@dataclass(frozen=True)
class G1ContractFrame:
    descriptor: G1FrameDescriptor
    roi_mappings: Tuple[G1RoiMapping, ...]
    observation_timestamp: Optional[float]
    timestamp_source: Optional[str]


@dataclass(frozen=True)
class G1ObservationAssembly:
    frames: Tuple[G1ContractFrame, ...]
    observations: Tuple[CriterionObservation, ...]

    @property
    def criterion_sequence(self) -> Tuple[tuple[str, int, CriterionStatus], ...]:
        return tuple(
            (observation.criterion_id, observation.frame_index, observation.status)
            for observation in self.observations
        )


def map_contract_roi(
    contract_id: str,
    binding: G1CriterionBindingIR,
    roi: ContractRoiIR,
    screenshot_size: Tuple[int, int],
) -> G1RoiMapping:
    """Map normalized/reference coordinates to current screenshot pixels."""

    if not isinstance(contract_id, str) or not contract_id.strip():
        raise ValueError("contract_id must be non-empty")
    binding.validate()
    roi.validate()
    if roi not in binding.rois:
        raise ValueError("ROI is not declared by the supplied G1 binding")
    width, height = screenshot_size
    if any(not isinstance(value, int) or isinstance(value, bool) or value <= 0 for value in (width, height)):
        raise ValueError("screenshot_size must contain two positive integers")
    x1, y1, x2, y2 = (float(value) for value in roi.bounds)
    if roi.coordinate_space is RoiCoordinateSpace.REFERENCE_PIXELS:
        if roi.reference_size is None:  # guarded by validate; retained for type narrowing
            raise ValueError("reference-pixel ROI has no reference_size")
        reference_width, reference_height = roi.reference_size
        x1, x2 = x1 / reference_width, x2 / reference_width
        y1, y2 = y1 / reference_height, y2 / reference_height
    pixel_x1 = min(width - 1, max(0, math.floor(x1 * width)))
    pixel_y1 = min(height - 1, max(0, math.floor(y1 * height)))
    pixel_x2 = min(width, max(pixel_x1 + 1, math.ceil(x2 * width)))
    pixel_y2 = min(height, max(pixel_y1 + 1, math.ceil(y2 * height)))
    pixel_bounds = (pixel_x1, pixel_y1, pixel_x2, pixel_y2)
    qualified_roi_id = f"{binding.criterion_id}::{roi.roi_id}"
    source = (
        f"contract:{contract_id}/{binding.criterion_id}/{roi.roi_id}"
        f"#{roi.coordinate_space.value.lower()}"
    )
    region = G1RegionOfInterest(qualified_roi_id, pixel_bounds, source)
    return G1RoiMapping(
        criterion_id=binding.criterion_id,
        roi_id=roi.roi_id,
        coordinate_space=roi.coordinate_space,
        contract_bounds=tuple(float(value) for value in roi.bounds),
        reference_size=roi.reference_size,
        screenshot_size=screenshot_size,
        pixel_bounds=pixel_bounds,
        region=region,
    )


def map_contract_rois(
    contract: ContractIR,
    screenshot_size: Tuple[int, int],
) -> Tuple[G1RoiMapping, ...]:
    contract.validate()
    return tuple(
        map_contract_roi(contract.contract_id, binding, roi, screenshot_size)
        for binding in contract.g1_bindings
        for roi in binding.rois
    )


def _binding_descriptor(
    descriptor: G1FrameDescriptor,
    binding: G1CriterionBindingIR,
) -> G1FrameDescriptor:
    prefix = f"{binding.criterion_id}::"
    return replace(
        descriptor,
        roi_visuals=tuple(
            visual for visual in descriptor.roi_visuals if visual.roi_id.startswith(prefix)
        ),
    )


def _checker_status(
    binding: G1CriterionBindingIR,
    current: G1FrameDescriptor,
    comparison: Optional[G1PairComparison],
) -> CriterionStatus:
    stable = bool(
        comparison and comparison.observation_state is ObservationState.STABLE_SEMANTIC
    )
    if binding.checker is G1CheckerKind.NO_BLOCKING_OVERLAY:
        if current.overlay_kind is not OverlayKind.NONE:
            return CriterionStatus.VIOLATED
        return CriterionStatus.SATISFIED if stable else CriterionStatus.UNKNOWN_EVIDENCE
    if binding.checker is G1CheckerKind.NOT_LOADING:
        if current.observation_state is ObservationState.STABLE_LOADING:
            return CriterionStatus.VIOLATED
        return CriterionStatus.SATISFIED if stable else CriterionStatus.UNKNOWN_EVIDENCE
    return CriterionStatus.SATISFIED if stable else CriterionStatus.UNKNOWN_EVIDENCE


def _criterion_observation_state(
    binding: G1CriterionBindingIR,
    status: CriterionStatus,
    current: G1FrameDescriptor,
    comparison: Optional[G1PairComparison],
) -> ObservationState:
    if status is CriterionStatus.VIOLATED:
        if binding.checker is G1CheckerKind.NO_BLOCKING_OVERLAY:
            return ObservationState.STABLE_SEMANTIC
        if binding.checker is G1CheckerKind.NOT_LOADING:
            return ObservationState.STABLE_LOADING
    return comparison.observation_state if comparison is not None else current.observation_state


def _evidence_detail(
    binding: G1CriterionBindingIR,
    frame: G1ContractFrame,
    comparison: Optional[G1PairComparison],
) -> str:
    mappings = [
        {
            "roi_id": mapping.roi_id,
            "coordinate_space": mapping.coordinate_space.value,
            "contract_bounds": list(mapping.contract_bounds),
            "reference_size": list(mapping.reference_size) if mapping.reference_size else None,
            "screenshot_size": list(mapping.screenshot_size),
            "pixel_bounds": list(mapping.pixel_bounds),
        }
        for mapping in frame.roi_mappings
        if mapping.criterion_id == binding.criterion_id
    ]
    return json.dumps(
        {
            "checker": binding.checker.value,
            "evidence_mode": frame.descriptor.evidence_mode.value,
            "timestamp_source": frame.timestamp_source,
            "rois": mappings,
            "comparison_reasons": list(comparison.reasons) if comparison else [],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def assemble_contract_g1_observations(
    trace_dir: Path | str,
    contract: ContractIR,
    contexts: Tuple[G1FrameContext, ...],
    *,
    policy: G1ObservationPolicy = G1ObservationPolicy(),
) -> G1ObservationAssembly:
    """Read evidence once and emit deterministic, typed observations only."""

    contract.validate()
    if len({context.frame_index for context in contexts}) != len(contexts):
        raise ValueError("G1 contexts must have unique frame_index values")
    ordered_contexts = tuple(sorted(contexts, key=lambda context: context.frame_index))
    frames = []
    for context in ordered_contexts:
        mappings = (
            map_contract_rois(contract, context.screenshot_size)
            if context.screenshot_size is not None
            else ()
        )
        mapped_context = replace(
            context,
            roi_context=tuple(mapping.region for mapping in mappings),
        )
        frames.append(
            G1ContractFrame(
                descriptor=describe_g1_frame(trace_dir, mapped_context, policy=policy),
                roi_mappings=mappings,
                observation_timestamp=context.observation_timestamp,
                timestamp_source=context.timestamp_source,
            )
        )

    observations = []
    for frame_position, frame in enumerate(frames):
        previous_frame = frames[frame_position - 1] if frame_position else None
        for binding in contract.g1_bindings:
            current_descriptor = _binding_descriptor(frame.descriptor, binding)
            comparison = None
            if previous_frame is not None:
                comparison = compare_g1_frames(
                    _binding_descriptor(previous_frame.descriptor, binding),
                    current_descriptor,
                    policy=policy,
                )
            status = _checker_status(binding, current_descriptor, comparison)
            source_ref = current_descriptor.hierarchy.source_ref
            if not source_ref and current_descriptor.roi_visuals:
                source_ref = current_descriptor.roi_visuals[0].screenshot_ref
            observations.append(
                CriterionObservation(
                    criterion_id=binding.criterion_id,
                    status=status,
                    frame_index=current_descriptor.frame_index,
                    observation_state=_criterion_observation_state(
                        binding,
                        status,
                        current_descriptor,
                        comparison,
                    ),
                    overlay_kind=current_descriptor.overlay_kind,
                    evidence=EvidencePointer(
                        frame_index=current_descriptor.frame_index,
                        source=source_ref or "g1-observer",
                        timestamp=frame.observation_timestamp,
                        detail=_evidence_detail(binding, frame, comparison),
                    ),
                    explicit_revocation=status is CriterionStatus.VIOLATED,
                )
            )
    return G1ObservationAssembly(frames=tuple(frames), observations=tuple(observations))


def attach_g1_observations(
    trace: DurableEventTrace,
    contract: ContractIR,
    assembly: G1ObservationAssembly,
) -> DurableEventTrace:
    """Return an enriched immutable trace; never calls or feeds back into Runner."""

    trace.validate()
    if trace.contract_sha256 != contract_sha256(contract):
        raise ValueError("event trace contract hash does not match the supplied ContractIR")
    if any(isinstance(event, CriterionObservationEvent) for event in trace.events):
        raise ValueError("G1 observations can only attach to an evidence-only event trace")
    assembly_frames = tuple(frame.descriptor.frame_index for frame in assembly.frames)
    if len(set(assembly_frames)) != len(assembly_frames):
        raise ValueError("G1 assembly frame indices must be unique")
    expected_observation_order = tuple(
        (binding.criterion_id, frame_index)
        for frame_index in assembly_frames
        for binding in contract.g1_bindings
    )
    actual_observation_order = tuple(
        (observation.criterion_id, observation.frame_index)
        for observation in assembly.observations
    )
    if actual_observation_order != expected_observation_order:
        raise ValueError("G1 observation order/completeness does not match ContractIR bindings")
    frame_indices = {
        event.frame_index for event in trace.events if isinstance(event, FrameEvidenceEvent)
    }
    observation_frames = {observation.frame_index for observation in assembly.observations}
    if not observation_frames.issubset(frame_indices):
        raise ValueError("G1 observation references a frame absent from the event trace")
    contract_ids = {criterion.criterion_id for criterion in contract.criteria}
    if any(observation.criterion_id not in contract_ids for observation in assembly.observations):
        raise ValueError("G1 observation references a criterion absent from ContractIR")

    contract_frames = {frame.descriptor.frame_index: frame for frame in assembly.frames}
    observations_by_frame: dict[int, list[CriterionObservation]] = {}
    for observation in assembly.observations:
        observations_by_frame.setdefault(observation.frame_index, []).append(observation)
    events = []
    for event in trace.events:
        if isinstance(event, TerminationEvent):
            continue
        if isinstance(event, FrameEvidenceEvent):
            contract_frame = contract_frames.get(event.frame_index)
            if contract_frame is not None:
                event = replace(
                    event,
                    observation_state=contract_frame.descriptor.observation_state,
                    overlay_kind=contract_frame.descriptor.overlay_kind,
                    timestamp=contract_frame.observation_timestamp,
                )
        events.append(replace(event, sequence_index=len(events)))
        if isinstance(event, FrameEvidenceEvent):
            for observation in observations_by_frame.get(event.frame_index, ()):
                events.append(
                    CriterionObservationEvent(
                        sequence_index=len(events), observation=observation
                    )
                )
    termination = next(event for event in trace.events if isinstance(event, TerminationEvent))
    events.append(replace(termination, sequence_index=len(events)))
    enriched = replace(trace, events=tuple(events))
    enriched.validate()
    return enriched
