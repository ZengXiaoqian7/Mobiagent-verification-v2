"""Immutable, diagnostic-free event log schema and checksum envelope."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Optional, Tuple, Union

from .models import (
    ContractIR,
    CriterionObservation,
    CriterionStatus,
    EvidenceCapabilityProfile,
    EvidencePointer,
    ObservationState,
    OverlayKind,
    RunMode,
    TerminationQuality,
    TraceIntegrity,
)
from .trace_adapter import TraceEvidenceBundle


EVENT_LOG_SCHEMA_VERSION = "harmony-eval-event-log-v1"
EVENT_LOG_ENVELOPE_SCHEMA_VERSION = "harmony-eval-event-log-envelope-v1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class EvaluationEventKind(str, Enum):
    FRAME_EVIDENCE = "FRAME_EVIDENCE"
    ACTION_EVIDENCE = "ACTION_EVIDENCE"
    CRITERION_OBSERVATION = "CRITERION_OBSERVATION"
    TERMINATION = "TERMINATION"


def _validate_index(name: str, value: int) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")


def _validate_relative_ref(name: str, value: Optional[str]) -> None:
    if value is None:
        return
    if not isinstance(value, str) or not value.strip() or "\\" in value:
        raise ValueError(f"{name} must be a non-empty POSIX relative reference")
    reference = PurePosixPath(value)
    if reference.is_absolute() or ".." in reference.parts:
        raise ValueError(f"{name} must not escape the trace root")


@dataclass(frozen=True)
class FrameEvidenceEvent:
    sequence_index: int
    frame_index: int
    observation_state: ObservationState = ObservationState.UNKNOWN
    overlay_kind: OverlayKind = OverlayKind.NONE
    screenshot_ref: Optional[str] = None
    hierarchy_raw_json_ref: Optional[str] = None
    hierarchy_xml_ref: Optional[str] = None
    timestamp: Optional[float] = None

    @property
    def kind(self) -> EvaluationEventKind:
        return EvaluationEventKind.FRAME_EVIDENCE

    def validate(self) -> None:
        _validate_index("sequence_index", self.sequence_index)
        _validate_index("frame_index", self.frame_index)
        for name, value in (
            ("screenshot_ref", self.screenshot_ref),
            ("hierarchy_raw_json_ref", self.hierarchy_raw_json_ref),
            ("hierarchy_xml_ref", self.hierarchy_xml_ref),
        ):
            _validate_relative_ref(name, value)
        if not any(
            (self.screenshot_ref, self.hierarchy_raw_json_ref, self.hierarchy_xml_ref)
        ):
            raise ValueError(
                "frame evidence event must reference at least one outcome artifact"
            )
        if self.timestamp is not None and (
            not isinstance(self.timestamp, (int, float))
            or isinstance(self.timestamp, bool)
            or not math.isfinite(self.timestamp)
            or self.timestamp < 0
        ):
            raise ValueError("timestamp must be a finite non-negative number")


@dataclass(frozen=True)
class ActionEvidenceEvent:
    sequence_index: int
    action_index: int
    action_type: str
    screenshot_size: Optional[Tuple[int, int]] = None
    click_coordinate_size: Optional[Tuple[int, int]] = None

    @property
    def kind(self) -> EvaluationEventKind:
        return EvaluationEventKind.ACTION_EVIDENCE

    def validate(self) -> None:
        _validate_index("sequence_index", self.sequence_index)
        _validate_index("action_index", self.action_index)
        if not isinstance(self.action_type, str) or not self.action_type.strip():
            raise ValueError("action_type must be non-empty")
        for name, size in (
            ("screenshot_size", self.screenshot_size),
            ("click_coordinate_size", self.click_coordinate_size),
        ):
            if size is not None and (
                len(size) != 2
                or any(
                    not isinstance(value, int) or isinstance(value, bool) or value <= 0
                    for value in size
                )
            ):
                raise ValueError(f"{name} must contain two positive integers")


@dataclass(frozen=True)
class CriterionObservationEvent:
    sequence_index: int
    observation: CriterionObservation

    @property
    def kind(self) -> EvaluationEventKind:
        return EvaluationEventKind.CRITERION_OBSERVATION

    def validate(self) -> None:
        _validate_index("sequence_index", self.sequence_index)
        if (
            not isinstance(self.observation.criterion_id, str)
            or not self.observation.criterion_id.strip()
        ):
            raise ValueError("criterion_id must be non-empty")
        if not isinstance(self.observation.explicit_revocation, bool):
            raise ValueError("explicit_revocation must be boolean")
        evidence = self.observation.evidence
        if evidence is not None:
            if not isinstance(evidence.source, str) or not evidence.source.strip():
                raise ValueError("evidence source must be non-empty")
            if evidence.timestamp is not None and (
                not isinstance(evidence.timestamp, (int, float))
                or isinstance(evidence.timestamp, bool)
                or not math.isfinite(evidence.timestamp)
                or evidence.timestamp < 0
            ):
                raise ValueError(
                    "evidence timestamp must be a finite non-negative number"
                )
            if evidence.detail is not None and not isinstance(evidence.detail, str):
                raise ValueError("evidence detail must be a string or null")


@dataclass(frozen=True)
class TerminationEvent:
    sequence_index: int
    quality: TerminationQuality
    declared_done_frame: Optional[int] = None
    grace_end_frame: Optional[int] = None
    declared_done_timestamp: Optional[float] = None
    grace_end_timestamp: Optional[float] = None

    @property
    def kind(self) -> EvaluationEventKind:
        return EvaluationEventKind.TERMINATION

    def validate(self) -> None:
        _validate_index("sequence_index", self.sequence_index)
        if self.declared_done_frame is not None:
            _validate_index("declared_done_frame", self.declared_done_frame)
        if self.grace_end_frame is not None:
            _validate_index("grace_end_frame", self.grace_end_frame)
            if self.declared_done_frame is None:
                raise ValueError("grace_end_frame requires declared_done_frame")
            if self.grace_end_frame < self.declared_done_frame:
                raise ValueError("grace_end_frame cannot precede declared_done_frame")
        for name, timestamp in (
            ("declared_done_timestamp", self.declared_done_timestamp),
            ("grace_end_timestamp", self.grace_end_timestamp),
        ):
            if timestamp is not None and (
                not isinstance(timestamp, (int, float))
                or isinstance(timestamp, bool)
                or not math.isfinite(timestamp)
                or timestamp < 0
            ):
                raise ValueError(f"{name} must be a finite non-negative number")
        if (
            self.declared_done_timestamp is not None
            and self.declared_done_frame is None
        ):
            raise ValueError("declared_done_timestamp requires declared_done_frame")
        if self.grace_end_timestamp is not None:
            if self.declared_done_timestamp is None or self.grace_end_frame is None:
                raise ValueError(
                    "grace_end_timestamp requires declared done and grace frame/time boundaries"
                )
            if self.grace_end_timestamp < self.declared_done_timestamp:
                raise ValueError(
                    "grace_end_timestamp cannot precede declared_done_timestamp"
                )


EvaluationEvent = Union[
    FrameEvidenceEvent,
    ActionEvidenceEvent,
    CriterionObservationEvent,
    TerminationEvent,
]


@dataclass(frozen=True)
class DurableEventTrace:
    trace_id: str
    contract_sha256: str
    capability_profile: EvidenceCapabilityProfile
    events: Tuple[EvaluationEvent, ...]
    mode: RunMode = RunMode.AUDIT_BENCHMARK
    source_trace_ref: Optional[str] = None
    run_timestamp: Optional[str] = None
    schema_version: str = EVENT_LOG_SCHEMA_VERSION

    def validate(self) -> None:
        if self.schema_version != EVENT_LOG_SCHEMA_VERSION:
            raise ValueError(f"unsupported event log schema: {self.schema_version}")
        if not isinstance(self.trace_id, str) or not self.trace_id.strip():
            raise ValueError("trace_id must be non-empty")
        if not isinstance(self.contract_sha256, str) or not _SHA256.fullmatch(
            self.contract_sha256
        ):
            raise ValueError("contract_sha256 must be a lowercase SHA-256 digest")
        if not isinstance(self.mode, RunMode):
            raise ValueError("mode must be a RunMode")
        if not isinstance(self.capability_profile, EvidenceCapabilityProfile):
            raise ValueError("capability_profile must be an EvidenceCapabilityProfile")
        _validate_relative_ref("source_trace_ref", self.source_trace_ref)
        if self.run_timestamp is not None and (
            not isinstance(self.run_timestamp, str) or not self.run_timestamp.strip()
        ):
            raise ValueError("run_timestamp must be a non-empty string or null")
        for name, count in (
            ("action_count", self.capability_profile.action_count),
            ("react_count", self.capability_profile.react_count),
        ):
            if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if not isinstance(self.capability_profile.integrity, TraceIntegrity):
            raise ValueError("capability integrity must be a TraceIntegrity")
        for name, indices in (
            ("screenshot_frames", self.capability_profile.screenshot_frames),
            (
                "hierarchy_raw_json_frames",
                self.capability_profile.hierarchy_raw_json_frames,
            ),
            ("hierarchy_xml_frames", self.capability_profile.hierarchy_xml_frames),
        ):
            if (
                any(
                    not isinstance(index, int) or isinstance(index, bool) or index < 0
                    for index in indices
                )
                or tuple(sorted(set(indices))) != indices
            ):
                raise ValueError(f"{name} must be sorted, unique non-negative integers")
        for name, values in (
            ("timestamp_sources", self.capability_profile.timestamp_sources),
            ("corrupt_artifacts", self.capability_profile.corrupt_artifacts),
            ("warnings", self.capability_profile.warnings),
        ):
            if any(not isinstance(value, str) for value in values):
                raise ValueError(f"{name} must contain strings")
        if not isinstance(self.events, tuple) or not self.events:
            raise ValueError("durable event trace must contain events")
        for expected, event in enumerate(self.events):
            if not isinstance(
                event,
                (
                    FrameEvidenceEvent,
                    ActionEvidenceEvent,
                    CriterionObservationEvent,
                    TerminationEvent,
                ),
            ):
                raise ValueError("event trace contains an unsupported event object")
            if event.sequence_index != expected:
                raise ValueError(
                    "event sequence_index values must be contiguous from zero"
                )
            event.validate()
        termination_events = [
            event for event in self.events if isinstance(event, TerminationEvent)
        ]
        if len(termination_events) != 1:
            raise ValueError(
                "durable event trace must contain exactly one termination event"
            )
        if self.events[-1] is not termination_events[0]:
            raise ValueError("termination event must be the final event")


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    try:
        rendered = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"value is not canonical-JSON serializable: {exc}") from exc
    return rendered.encode("utf-8")


def _json_payload_value(value: Any) -> Any:
    """Return mutable JSON containers without exposing ContractIR internals."""

    if isinstance(value, Mapping):
        return {key: _json_payload_value(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_payload_value(child) for child in value]
    return value


def contract_payload(contract: ContractIR) -> dict[str, Any]:
    contract.validate()
    payload = {
        "schema_version": contract.schema_version,
        "contract_id": contract.contract_id,
        "source": contract.source,
        "required_capabilities": [
            item.value for item in contract.required_capabilities
        ],
        "metadata": _json_payload_value(contract.metadata),
        "criteria": [
            {
                "criterion_id": criterion.criterion_id,
                "temporal_semantics": criterion.temporal_semantics.value,
                "required": criterion.required,
                "allow_obscured_persistence": criterion.allow_obscured_persistence,
                "required_capabilities": [
                    item.value for item in criterion.required_capabilities
                ],
                "description": criterion.description,
            }
            for criterion in contract.criteria
        ],
    }
    if contract.task_family is not None:
        payload["task_family"] = contract.task_family
    if contract.compiler_provenance is not None:
        provenance = contract.compiler_provenance
        payload["compiler_provenance"] = {
            "source_type": provenance.source_type.value,
            "source_id": provenance.source_id,
            "source_version": provenance.source_version,
            "source_digest": provenance.source_digest,
            "source_locator": provenance.source_locator,
            "selection_key": provenance.selection_key,
        }
    if contract.g1_bindings:
        payload["g1_bindings"] = [
            {
                "criterion_id": binding.criterion_id,
                "checker": binding.checker.value,
                "rois": [
                    {
                        "roi_id": roi.roi_id,
                        "bounds": [float(value) for value in roi.bounds],
                        "coordinate_space": roi.coordinate_space.value,
                        "reference_size": (
                            list(roi.reference_size) if roi.reference_size else None
                        ),
                    }
                    for roi in binding.rois
                ],
            }
            for binding in contract.g1_bindings
        ]
    if contract.dag is not None:
        payload["dag"] = {
            "nodes": [
                {
                    "node_id": node.node_id,
                    "condition_operator": node.condition_operator.value,
                    "checker_ids": list(node.checker_ids),
                    "condition_sha256": node.condition_sha256,
                    "score": node.score,
                    "checkers": [
                        {
                            "checker_id": checker.checker_id,
                            "parameters": _json_payload_value(checker.parameters),
                        }
                        for checker in node.checkers
                    ],
                }
                for node in contract.dag.nodes
            ],
            "edges": [
                {
                    "parent_id": edge.parent_id,
                    "child_id": edge.child_id,
                    "kind": edge.kind.value,
                }
                for edge in contract.dag.edges
            ],
            "success": {
                "operator": contract.dag.success.operator.value,
                "node_ids": list(contract.dag.success.node_ids),
            },
        }
    return payload


def contract_sha256(contract: ContractIR) -> str:
    return hashlib.sha256(_canonical_bytes(contract_payload(contract))).hexdigest()


def _profile_payload(profile: EvidenceCapabilityProfile) -> dict[str, Any]:
    return {
        "screenshot_frames": list(profile.screenshot_frames),
        "hierarchy_raw_json_frames": list(profile.hierarchy_raw_json_frames),
        "hierarchy_xml_frames": list(profile.hierarchy_xml_frames),
        "action_count": profile.action_count,
        "react_count": profile.react_count,
        "timestamp_sources": list(profile.timestamp_sources),
        "integrity": profile.integrity.value,
        "corrupt_artifacts": list(profile.corrupt_artifacts),
        "warnings": list(profile.warnings),
    }


def _evidence_payload(evidence: Optional[EvidencePointer]) -> Optional[dict[str, Any]]:
    if evidence is None:
        return None
    return {
        "frame_index": evidence.frame_index,
        "source": evidence.source,
        "timestamp": evidence.timestamp,
        "detail": evidence.detail,
    }


def _event_payload(event: EvaluationEvent) -> dict[str, Any]:
    if isinstance(event, FrameEvidenceEvent):
        return {
            "kind": event.kind.value,
            "sequence_index": event.sequence_index,
            "frame_index": event.frame_index,
            "observation_state": event.observation_state.value,
            "overlay_kind": event.overlay_kind.value,
            "screenshot_ref": event.screenshot_ref,
            "hierarchy_raw_json_ref": event.hierarchy_raw_json_ref,
            "hierarchy_xml_ref": event.hierarchy_xml_ref,
            "timestamp": event.timestamp,
        }
    if isinstance(event, ActionEvidenceEvent):
        return {
            "kind": event.kind.value,
            "sequence_index": event.sequence_index,
            "action_index": event.action_index,
            "action_type": event.action_type,
            "screenshot_size": (
                list(event.screenshot_size) if event.screenshot_size else None
            ),
            "click_coordinate_size": (
                list(event.click_coordinate_size)
                if event.click_coordinate_size
                else None
            ),
        }
    if isinstance(event, CriterionObservationEvent):
        observation = event.observation
        return {
            "kind": event.kind.value,
            "sequence_index": event.sequence_index,
            "criterion_id": observation.criterion_id,
            "status": observation.status.value,
            "frame_index": observation.frame_index,
            "observation_state": observation.observation_state.value,
            "overlay_kind": observation.overlay_kind.value,
            "evidence": _evidence_payload(observation.evidence),
            "explicit_revocation": observation.explicit_revocation,
        }
    payload = {
        "kind": event.kind.value,
        "sequence_index": event.sequence_index,
        "quality": event.quality.value,
        "declared_done_frame": event.declared_done_frame,
        "grace_end_frame": event.grace_end_frame,
    }
    if (
        event.declared_done_timestamp is not None
        or event.grace_end_timestamp is not None
    ):
        payload["declared_done_timestamp"] = event.declared_done_timestamp
        payload["grace_end_timestamp"] = event.grace_end_timestamp
    return payload


def event_trace_payload(trace: DurableEventTrace) -> dict[str, Any]:
    trace.validate()
    return {
        "schema_version": trace.schema_version,
        "trace_id": trace.trace_id,
        "contract_sha256": trace.contract_sha256,
        "mode": trace.mode.value,
        "source_trace_ref": trace.source_trace_ref,
        "run_timestamp": trace.run_timestamp,
        "capability_profile": _profile_payload(trace.capability_profile),
        "events": [_event_payload(event) for event in trace.events],
    }


def event_trace_sha256(trace: DurableEventTrace) -> str:
    return hashlib.sha256(_canonical_bytes(event_trace_payload(trace))).hexdigest()


def _expect_keys(value: Mapping[str, Any], expected: set[str], context: str) -> None:
    actual = set(value)
    if actual != expected:
        raise ValueError(
            f"{context} keys mismatch; missing={sorted(expected - actual)}, "
            f"unexpected={sorted(actual - expected)}"
        )


def _require_mapping(value: Any, context: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{context} must be an object")
    return value


def _integer_tuple(value: Any, context: str) -> Tuple[int, ...]:
    if not isinstance(value, list) or any(
        not isinstance(item, int) or isinstance(item, bool) or item < 0
        for item in value
    ):
        raise ValueError(f"{context} must be a list of non-negative integers")
    return tuple(value)


def _string_tuple(value: Any, context: str) -> Tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{context} must be a list of strings")
    return tuple(value)


def _optional_pair(value: Any, context: str) -> Optional[Tuple[int, int]]:
    if value is None:
        return None
    values = _integer_tuple(value, context)
    if len(values) != 2 or any(item <= 0 for item in values):
        raise ValueError(f"{context} must contain two positive integers")
    return values[0], values[1]


def _profile_from_payload(value: Any) -> EvidenceCapabilityProfile:
    payload = _require_mapping(value, "capability_profile")
    expected = {
        "screenshot_frames",
        "hierarchy_raw_json_frames",
        "hierarchy_xml_frames",
        "action_count",
        "react_count",
        "timestamp_sources",
        "integrity",
        "corrupt_artifacts",
        "warnings",
    }
    _expect_keys(payload, expected, "capability_profile")
    action_count = payload["action_count"]
    react_count = payload["react_count"]
    if any(
        not isinstance(item, int) or isinstance(item, bool) or item < 0
        for item in (action_count, react_count)
    ):
        raise ValueError("capability counts must be non-negative integers")
    return EvidenceCapabilityProfile(
        screenshot_frames=_integer_tuple(
            payload["screenshot_frames"], "screenshot_frames"
        ),
        hierarchy_raw_json_frames=_integer_tuple(
            payload["hierarchy_raw_json_frames"], "hierarchy_raw_json_frames"
        ),
        hierarchy_xml_frames=_integer_tuple(
            payload["hierarchy_xml_frames"], "hierarchy_xml_frames"
        ),
        action_count=action_count,
        react_count=react_count,
        timestamp_sources=_string_tuple(
            payload["timestamp_sources"], "timestamp_sources"
        ),
        integrity=TraceIntegrity(payload["integrity"]),
        corrupt_artifacts=_string_tuple(
            payload["corrupt_artifacts"], "corrupt_artifacts"
        ),
        warnings=_string_tuple(payload["warnings"], "warnings"),
    )


def _evidence_from_payload(value: Any) -> Optional[EvidencePointer]:
    if value is None:
        return None
    payload = _require_mapping(value, "criterion evidence")
    _expect_keys(
        payload, {"frame_index", "source", "timestamp", "detail"}, "criterion evidence"
    )
    return EvidencePointer(
        frame_index=payload["frame_index"],
        source=payload["source"],
        timestamp=payload["timestamp"],
        detail=payload["detail"],
    )


def _event_from_payload(value: Any) -> EvaluationEvent:
    payload = _require_mapping(value, "event")
    try:
        kind = EvaluationEventKind(payload.get("kind"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"unsupported event kind: {payload.get('kind')}") from exc
    if kind is EvaluationEventKind.FRAME_EVIDENCE:
        expected = {
            "kind",
            "sequence_index",
            "frame_index",
            "observation_state",
            "overlay_kind",
            "screenshot_ref",
            "hierarchy_raw_json_ref",
            "hierarchy_xml_ref",
            "timestamp",
        }
        _expect_keys(payload, expected, "frame event")
        return FrameEvidenceEvent(
            sequence_index=payload["sequence_index"],
            frame_index=payload["frame_index"],
            observation_state=ObservationState(payload["observation_state"]),
            overlay_kind=OverlayKind(payload["overlay_kind"]),
            screenshot_ref=payload["screenshot_ref"],
            hierarchy_raw_json_ref=payload["hierarchy_raw_json_ref"],
            hierarchy_xml_ref=payload["hierarchy_xml_ref"],
            timestamp=payload["timestamp"],
        )
    if kind is EvaluationEventKind.ACTION_EVIDENCE:
        expected = {
            "kind",
            "sequence_index",
            "action_index",
            "action_type",
            "screenshot_size",
            "click_coordinate_size",
        }
        _expect_keys(payload, expected, "action event")
        return ActionEvidenceEvent(
            sequence_index=payload["sequence_index"],
            action_index=payload["action_index"],
            action_type=payload["action_type"],
            screenshot_size=_optional_pair(
                payload["screenshot_size"], "screenshot_size"
            ),
            click_coordinate_size=_optional_pair(
                payload["click_coordinate_size"], "click_coordinate_size"
            ),
        )
    if kind is EvaluationEventKind.CRITERION_OBSERVATION:
        expected = {
            "kind",
            "sequence_index",
            "criterion_id",
            "status",
            "frame_index",
            "observation_state",
            "overlay_kind",
            "evidence",
            "explicit_revocation",
        }
        _expect_keys(payload, expected, "criterion observation event")
        return CriterionObservationEvent(
            sequence_index=payload["sequence_index"],
            observation=CriterionObservation(
                criterion_id=payload["criterion_id"],
                status=CriterionStatus(payload["status"]),
                frame_index=payload["frame_index"],
                observation_state=ObservationState(payload["observation_state"]),
                overlay_kind=OverlayKind(payload["overlay_kind"]),
                evidence=_evidence_from_payload(payload["evidence"]),
                explicit_revocation=payload["explicit_revocation"],
            ),
        )
    legacy_expected = {
        "kind",
        "sequence_index",
        "quality",
        "declared_done_frame",
        "grace_end_frame",
    }
    timestamped_expected = legacy_expected | {
        "declared_done_timestamp",
        "grace_end_timestamp",
    }
    if set(payload) not in (legacy_expected, timestamped_expected):
        _expect_keys(payload, legacy_expected, "termination event")
    return TerminationEvent(
        sequence_index=payload["sequence_index"],
        quality=TerminationQuality(payload["quality"]),
        declared_done_frame=payload["declared_done_frame"],
        grace_end_frame=payload["grace_end_frame"],
        declared_done_timestamp=payload.get("declared_done_timestamp"),
        grace_end_timestamp=payload.get("grace_end_timestamp"),
    )


def event_trace_from_payload(value: Any) -> DurableEventTrace:
    payload = _require_mapping(value, "event trace payload")
    expected = {
        "schema_version",
        "trace_id",
        "contract_sha256",
        "mode",
        "source_trace_ref",
        "run_timestamp",
        "capability_profile",
        "events",
    }
    _expect_keys(payload, expected, "event trace payload")
    raw_events = payload["events"]
    if not isinstance(raw_events, list):
        raise ValueError("events must be a list")
    trace = DurableEventTrace(
        schema_version=payload["schema_version"],
        trace_id=payload["trace_id"],
        contract_sha256=payload["contract_sha256"],
        mode=RunMode(payload["mode"]),
        source_trace_ref=payload["source_trace_ref"],
        run_timestamp=payload["run_timestamp"],
        capability_profile=_profile_from_payload(payload["capability_profile"]),
        events=tuple(_event_from_payload(item) for item in raw_events),
    )
    trace.validate()
    return trace


def write_durable_event_trace(path: Path | str, trace: DurableEventTrace) -> str:
    """Create a checksum envelope once; existing paths are never overwritten."""

    destination = Path(path)
    payload = event_trace_payload(trace)
    payload_sha256 = hashlib.sha256(_canonical_bytes(payload)).hexdigest()
    envelope = {
        "envelope_schema": EVENT_LOG_ENVELOPE_SCHEMA_VERSION,
        "payload_sha256": payload_sha256,
        "payload": payload,
    }
    rendered = json.dumps(
        envelope,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        allow_nan=False,
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(rendered)
        stream.write("\n")
    return payload_sha256


def read_durable_event_trace(path: Path | str) -> DurableEventTrace:
    source = Path(path)
    try:
        loaded = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"durable event trace is unreadable: {exc}") from exc
    envelope = _require_mapping(loaded, "event log envelope")
    _expect_keys(
        envelope, {"envelope_schema", "payload_sha256", "payload"}, "event log envelope"
    )
    if envelope["envelope_schema"] != EVENT_LOG_ENVELOPE_SCHEMA_VERSION:
        raise ValueError(
            f"unsupported event log envelope: {envelope['envelope_schema']}"
        )
    expected_digest = envelope["payload_sha256"]
    if not isinstance(expected_digest, str) or not _SHA256.fullmatch(expected_digest):
        raise ValueError("payload_sha256 is not a valid lowercase SHA-256 digest")
    payload = _require_mapping(envelope["payload"], "event trace payload")
    actual_digest = hashlib.sha256(_canonical_bytes(payload)).hexdigest()
    if not hmac.compare_digest(actual_digest, expected_digest):
        raise ValueError("durable event trace checksum mismatch")
    return event_trace_from_payload(payload)


def trace_bundle_to_event_trace(
    bundle: TraceEvidenceBundle,
    contract: ContractIR,
    *,
    trace_id: str,
) -> DurableEventTrace:
    """Convert adapter output to a diagnostic-free, immutable evidence event stream."""

    frames = {frame.frame_index: frame for frame in bundle.outcome_frames}
    actions = {action.action_index: action for action in bundle.process_actions}
    events: list[EvaluationEvent] = []
    for index in sorted(set(frames) | set(actions)):
        frame = frames.get(index)
        if frame is not None:
            events.append(
                FrameEvidenceEvent(
                    sequence_index=len(events),
                    frame_index=frame.frame_index,
                    screenshot_ref=frame.screenshot_ref,
                    hierarchy_raw_json_ref=frame.hierarchy_raw_json_ref,
                    hierarchy_xml_ref=frame.hierarchy_xml_ref,
                )
            )
        action = actions.get(index)
        if action is not None:
            events.append(
                ActionEvidenceEvent(
                    sequence_index=len(events),
                    action_index=action.action_index,
                    action_type=action.action_type,
                    screenshot_size=action.screenshot_size,
                    click_coordinate_size=action.click_coordinate_size,
                )
            )
    declared_done = bundle.diagnostics.declared_done_action_index
    last_observed_frame = max(frames) if frames else None
    grace_end = (
        last_observed_frame
        if declared_done is not None
        and last_observed_frame is not None
        and last_observed_frame > declared_done
        else None
    )
    events.append(
        TerminationEvent(
            sequence_index=len(events),
            quality=TerminationQuality.UNKNOWN,
            declared_done_frame=declared_done,
            grace_end_frame=grace_end,
        )
    )
    trace = DurableEventTrace(
        trace_id=trace_id,
        contract_sha256=contract_sha256(contract),
        capability_profile=bundle.capability_profile,
        events=tuple(events),
        mode=RunMode.AUDIT_BENCHMARK,
        source_trace_ref=bundle.trace_ref,
        run_timestamp=bundle.run_timestamp,
    )
    trace.validate()
    return trace


def attach_criterion_observations(
    trace: DurableEventTrace,
    contract: ContractIR,
    observations: Tuple[CriterionObservation, ...],
) -> DurableEventTrace:
    """Attach checker observations to an evidence trace without replacing G1 facts.

    This is the shared bridge between checker routing and temporal replay. Existing
    G1 observations remain intact; duplicate criterion/frame observations are
    rejected so two backends cannot silently overwrite one another.
    """

    trace.validate()
    if trace.contract_sha256 != contract_sha256(contract):
        raise ValueError(
            "event trace contract hash does not match the supplied ContractIR"
        )
    if not isinstance(observations, tuple):
        raise ValueError("criterion observations must be an immutable tuple")
    contract_ids = {criterion.criterion_id for criterion in contract.criteria}
    frame_indices = {
        event.frame_index
        for event in trace.events
        if isinstance(event, FrameEvidenceEvent)
    }
    existing_keys = {
        (event.observation.criterion_id, event.observation.frame_index)
        for event in trace.events
        if isinstance(event, CriterionObservationEvent)
    }
    new_keys = []
    for observation in observations:
        if not isinstance(observation, CriterionObservation):
            raise ValueError("checker output contains a non-observation value")
        if observation.criterion_id not in contract_ids:
            raise ValueError(
                "checker observation references a criterion absent from ContractIR"
            )
        if observation.frame_index not in frame_indices:
            raise ValueError("checker observation references a frame absent from trace")
        key = (observation.criterion_id, observation.frame_index)
        if key in existing_keys:
            raise ValueError("checker observation duplicates an existing G1 fact")
        new_keys.append(key)
    if len(new_keys) != len(set(new_keys)):
        raise ValueError("checker observations contain duplicate criterion/frame keys")

    by_frame: dict[int, list[CriterionObservation]] = {}
    for observation in observations:
        by_frame.setdefault(observation.frame_index, []).append(observation)
    events: list[EvaluationEvent] = []
    for event in trace.events:
        if isinstance(event, TerminationEvent):
            continue
        events.append(replace(event, sequence_index=len(events)))
        if isinstance(event, FrameEvidenceEvent):
            for observation in sorted(
                by_frame.get(event.frame_index, ()),
                key=lambda item: item.criterion_id,
            ):
                events.append(
                    CriterionObservationEvent(
                        sequence_index=len(events), observation=observation
                    )
                )
    termination = next(
        event for event in trace.events if isinstance(event, TerminationEvent)
    )
    events.append(replace(termination, sequence_index=len(events)))
    enriched = replace(trace, events=tuple(events))
    enriched.validate()
    return enriched
