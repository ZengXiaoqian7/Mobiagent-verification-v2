"""Deterministic, reasoning-free acquisition for locally supported legacy checkers."""

from __future__ import annotations

import hashlib
import json
import math
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Optional, Tuple

from .event_log import (
    ActionEvidenceEvent,
    DurableEventTrace,
    FrameEvidenceEvent,
    contract_sha256,
    event_trace_sha256,
)
from .g1_observer import HierarchyEvidenceStatus
from .legacy_checker_lowering import (
    LegacyCheckerOutcome,
    LegacyCheckerOutcomeTable,
    LegacyCheckerSignal,
    LegacyLoweringEvaluation,
    bind_legacy_checker_outcomes,
    evaluate_lowered_legacy_contract,
)
from .models import (
    CheckerEvidenceIdentityIR,
    ContractCheckerIR,
    ContractIR,
    ContractSourceType,
    EvidenceCapabilityProfile,
    TraceIntegrity,
    _freeze_json_value,
)
from .precomputed_evidence_cache import (
    EvidenceCacheKey,
    RECORDED_PROVIDER_ACQUISITION_VERSION,
    RECORDED_PROVIDER_ID,
    RecordedLlmDecision,
    RecordedLlmOutput,
    RecordedOcrOutput,
    RecordedProviderContext,
    RecordedProviderKind,
)
from .visual_state_evidence_cache import (
    COMPOSITE_EVIDENCE_ACQUISITION_VERSION,
    COMPOSITE_EVIDENCE_PROVIDER_ID,
    VISUAL_STATE_ACQUISITION_VERSION,
    VISUAL_STATE_PROVIDER_ID,
    VisualStateCacheKey,
    VisualStateDecision,
    VisualStateProviderContext,
    composite_evidence_sha256,
)


LEGACY_CHECKER_ACQUISITION_VERSION = "harmony-eval-legacy-checker-acquisition-v1"
LOCAL_DETERMINISTIC_PROVIDER_ID = "local-deterministic-reasoning-free-v1"
LEGACY_CHECKER_EVIDENCE_SCHEMA_VERSION = "harmony-eval-legacy-checker-evidence-v2"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_DIAGNOSTIC_MARKERS = ("reason", "react", "self_report", "verdict", "stop_reason")
_LOCAL_CHECKERS = frozenset({"text", "regex", "ui", "action", "xml", "dynamic_match"})
_EXTERNAL_CHECKERS = frozenset({"ocr", "llm", "icons", "visual_state"})
_OUTCOME_FIELDS = frozenset({"text", "xml_text"})


class LegacyCheckerAcquisitionFailureCode(str, Enum):
    INVALID_EVIDENCE = "INVALID_EVIDENCE"
    SOURCE_UNREADABLE = "SOURCE_UNREADABLE"
    TRACE_MISMATCH = "TRACE_MISMATCH"
    INVALID_CHECKER_SCHEMA = "INVALID_CHECKER_SCHEMA"


class LegacyCheckerAcquisitionError(ValueError):
    def __init__(self, code: LegacyCheckerAcquisitionFailureCode, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class LegacyProcessActionEvidence:
    action_type: str
    fields: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.fields, Mapping):
            raise ValueError("action evidence fields must be a JSON object")
        object.__setattr__(
            self,
            "fields",
            _freeze_json_value(self.fields, context="action evidence fields"),
        )

    def validate(self) -> None:
        if (
            not isinstance(self.action_type, str)
            or not self.action_type.strip()
            or self.action_type != self.action_type.strip()
        ):
            raise ValueError("action evidence type must be canonical")
        if not isinstance(self.fields, Mapping):
            raise ValueError("action evidence fields must remain immutable")
        for key in self.fields:
            if any(marker in key.casefold() for marker in _DIAGNOSTIC_MARKERS):
                raise ValueError("diagnostic action fields are forbidden")


@dataclass(frozen=True)
class LegacyCheckerFrameEvidence:
    frame_index: int
    screenshot_sha256: Optional[str] = None
    outcome_text: Optional[str] = None
    xml_text: Optional[str] = None
    xml_status: HierarchyEvidenceStatus = HierarchyEvidenceStatus.MISSING
    ui_state: Mapping[str, Any] = field(default_factory=dict)
    action: Optional[LegacyProcessActionEvidence] = None

    def __post_init__(self) -> None:
        if not isinstance(self.ui_state, Mapping):
            raise ValueError("UI evidence must be a JSON object")
        object.__setattr__(
            self,
            "ui_state",
            _freeze_json_value(self.ui_state, context="UI evidence"),
        )

    def validate(self) -> None:
        if (
            not isinstance(self.frame_index, int)
            or isinstance(self.frame_index, bool)
            or self.frame_index < 0
        ):
            raise ValueError("checker evidence frame_index must be non-negative")
        for name, value in (
            ("outcome_text", self.outcome_text),
            ("xml_text", self.xml_text),
        ):
            if value is not None and not isinstance(value, str):
                raise ValueError(f"checker evidence {name} must be a string or null")
        if self.screenshot_sha256 is not None and (
            not isinstance(self.screenshot_sha256, str)
            or not _SHA256.fullmatch(self.screenshot_sha256)
        ):
            raise ValueError(
                "checker evidence screenshot_sha256 must be null or lowercase SHA-256"
            )
        if not isinstance(self.xml_status, HierarchyEvidenceStatus):
            raise ValueError("checker evidence xml_status is invalid")
        if (
            self.xml_status is HierarchyEvidenceStatus.MISSING
            and self.xml_text is not None
        ):
            raise ValueError("missing XML evidence must not carry text")
        if self.xml_status is HierarchyEvidenceStatus.EMPTY and (
            self.xml_text is None or self.xml_text.strip()
        ):
            raise ValueError("empty XML evidence must carry only empty/whitespace text")
        if self.xml_status in (
            HierarchyEvidenceStatus.AVAILABLE,
            HierarchyEvidenceStatus.MALFORMED,
        ) and (self.xml_text is None or not self.xml_text.strip()):
            raise ValueError(
                "available/malformed XML evidence must carry non-empty text"
            )
        if self.xml_status is HierarchyEvidenceStatus.AVAILABLE:
            try:
                ET.fromstring(self.xml_text or "")
            except ET.ParseError as exc:
                raise ValueError("available XML evidence is not well formed") from exc
        if not isinstance(self.ui_state, Mapping):
            raise ValueError("UI evidence must remain immutable")
        for key in self.ui_state:
            if any(marker in key.casefold() for marker in _DIAGNOSTIC_MARKERS):
                raise ValueError("diagnostic UI fields are forbidden")
        if self.action is not None:
            if not isinstance(self.action, LegacyProcessActionEvidence):
                raise ValueError("checker action evidence is invalid")
            self.action.validate()


@dataclass(frozen=True)
class LegacyCheckerEvidence:
    trace_id: str
    trace_sha256: str
    frames: Tuple[LegacyCheckerFrameEvidence, ...]
    capability_profile: EvidenceCapabilityProfile
    task_description: Optional[str] = None
    contract_parameters: Mapping[str, Any] = field(default_factory=dict)
    trace_contract_sha256: Optional[str] = None
    schema_version: str = LEGACY_CHECKER_EVIDENCE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.contract_parameters, Mapping):
            raise ValueError(
                "checker evidence contract_parameters must be a JSON object"
            )
        object.__setattr__(
            self,
            "contract_parameters",
            _freeze_json_value(
                self.contract_parameters,
                context="checker evidence contract_parameters",
            ),
        )

    def validate(self) -> None:
        if self.schema_version != LEGACY_CHECKER_EVIDENCE_SCHEMA_VERSION:
            raise ValueError("unsupported legacy checker evidence schema")
        if (
            not isinstance(self.trace_id, str)
            or not self.trace_id.strip()
            or self.trace_id != self.trace_id.strip()
        ):
            raise ValueError("checker evidence trace_id must be canonical")
        if not isinstance(self.trace_sha256, str) or not _SHA256.fullmatch(
            self.trace_sha256
        ):
            raise ValueError(
                "checker evidence trace_sha256 must be a lowercase SHA-256"
            )
        if not isinstance(self.frames, tuple) or any(
            not isinstance(frame, LegacyCheckerFrameEvidence) for frame in self.frames
        ):
            raise ValueError("checker evidence frames must be an immutable tuple")
        for frame in self.frames:
            frame.validate()
        indices = tuple(frame.frame_index for frame in self.frames)
        if indices != tuple(range(len(self.frames))):
            raise ValueError("checker evidence frames must be contiguous from zero")
        if not isinstance(self.capability_profile, EvidenceCapabilityProfile):
            raise ValueError("checker evidence capability profile is invalid")
        if self.capability_profile.integrity is TraceIntegrity.INVALID:
            raise LegacyCheckerAcquisitionError(
                LegacyCheckerAcquisitionFailureCode.INVALID_EVIDENCE,
                "G0-invalid evidence cannot enter checker acquisition",
            )
        if self.task_description is not None and not isinstance(
            self.task_description, str
        ):
            raise ValueError("task_description must be a string or null")
        if not isinstance(self.contract_parameters, Mapping):
            raise ValueError("contract_parameters must remain immutable")
        for key in self.contract_parameters:
            if any(marker in key.casefold() for marker in _DIAGNOSTIC_MARKERS):
                raise ValueError("diagnostic contract parameter fields are forbidden")
        if self.trace_contract_sha256 is not None and (
            not isinstance(self.trace_contract_sha256, str)
            or not _SHA256.fullmatch(self.trace_contract_sha256)
        ):
            raise ValueError(
                "trace_contract_sha256 must be null or a lowercase SHA-256"
            )

    @property
    def identity(self) -> CheckerEvidenceIdentityIR:
        self.validate()
        return CheckerEvidenceIdentityIR(
            trace_id=self.trace_id,
            trace_sha256=self.trace_sha256,
            evidence_sha256=legacy_checker_evidence_sha256(self),
            frame_start=0,
            frame_end_exclusive=len(self.frames),
        )


def _json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _json_value(child) for key, child in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(child) for child in value]
    return value


def legacy_checker_evidence_payload(evidence: LegacyCheckerEvidence) -> dict[str, Any]:
    evidence.validate()
    profile = evidence.capability_profile
    return {
        "schema_version": evidence.schema_version,
        "trace_id": evidence.trace_id,
        "trace_sha256": evidence.trace_sha256,
        "trace_contract_sha256": evidence.trace_contract_sha256,
        "frame_window": [0, len(evidence.frames)],
        "task_description": evidence.task_description,
        "contract_parameters": _json_value(evidence.contract_parameters),
        "capability_profile": {
            "screenshot_frames": list(profile.screenshot_frames),
            "hierarchy_raw_json_frames": list(profile.hierarchy_raw_json_frames),
            "hierarchy_xml_frames": list(profile.hierarchy_xml_frames),
            "action_count": profile.action_count,
            "timestamp_sources": list(profile.timestamp_sources),
            "integrity": profile.integrity.value,
            "corrupt_artifacts": list(profile.corrupt_artifacts),
            "warnings": list(profile.warnings),
        },
        "frames": [
            {
                "frame_index": frame.frame_index,
                "screenshot_sha256": frame.screenshot_sha256,
                "outcome_text": frame.outcome_text,
                "xml_text": frame.xml_text,
                "xml_status": frame.xml_status.value,
                "ui_state": _json_value(frame.ui_state),
                "action": (
                    None
                    if frame.action is None
                    else {
                        "action_type": frame.action.action_type,
                        "fields": _json_value(frame.action.fields),
                    }
                ),
            }
            for frame in evidence.frames
        ],
    }


def legacy_checker_evidence_sha256(evidence: LegacyCheckerEvidence) -> str:
    rendered = json.dumps(
        legacy_checker_evidence_payload(evidence),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(rendered).hexdigest()


def _safe_artifact(root: Path, reference: str) -> Path:
    candidate = (root / reference).resolve()
    resolved_root = root.resolve()
    try:
        candidate.relative_to(resolved_root)
    except ValueError as exc:
        raise LegacyCheckerAcquisitionError(
            LegacyCheckerAcquisitionFailureCode.TRACE_MISMATCH,
            "artifact reference escapes the trace root",
        ) from exc
    return candidate


def _read_actions(
    trace_root: Path,
) -> tuple[Optional[str], dict[int, LegacyProcessActionEvidence]]:
    path = trace_root / "actions.json"
    if not path.is_file():
        return None, {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise LegacyCheckerAcquisitionError(
            LegacyCheckerAcquisitionFailureCode.SOURCE_UNREADABLE,
            "actions.json is unreadable",
        ) from exc
    if not isinstance(payload, Mapping) or not isinstance(payload.get("actions"), list):
        raise LegacyCheckerAcquisitionError(
            LegacyCheckerAcquisitionFailureCode.INVALID_EVIDENCE,
            "actions.json must contain an actions list",
        )
    task_description = payload.get("task_description") or payload.get(
        "old_task_description"
    )
    if task_description is not None and not isinstance(task_description, str):
        raise LegacyCheckerAcquisitionError(
            LegacyCheckerAcquisitionFailureCode.INVALID_EVIDENCE,
            "task description must be textual",
        )
    actions: dict[int, LegacyProcessActionEvidence] = {}
    for ordinal, row in enumerate(payload["actions"], 1):
        if not isinstance(row, Mapping):
            raise LegacyCheckerAcquisitionError(
                LegacyCheckerAcquisitionFailureCode.INVALID_EVIDENCE,
                "action rows must be objects",
            )
        raw_index = row.get("action_index", ordinal)
        if (
            not isinstance(raw_index, int)
            or isinstance(raw_index, bool)
            or raw_index < 0
        ):
            raise LegacyCheckerAcquisitionError(
                LegacyCheckerAcquisitionFailureCode.INVALID_EVIDENCE,
                "action_index must be non-negative",
            )
        action_type = row.get("type")
        if not isinstance(action_type, str) or not action_type.strip():
            raise LegacyCheckerAcquisitionError(
                LegacyCheckerAcquisitionFailureCode.INVALID_EVIDENCE,
                "action type must be non-empty",
            )
        if raw_index in actions:
            raise LegacyCheckerAcquisitionError(
                LegacyCheckerAcquisitionFailureCode.INVALID_EVIDENCE,
                "action_index values must be unique",
            )
        fields = {
            str(key): value
            for key, value in row.items()
            if key not in {"action_index", "type"}
            and not any(marker in str(key).casefold() for marker in _DIAGNOSTIC_MARKERS)
        }
        actions[raw_index] = LegacyProcessActionEvidence(action_type.strip(), fields)
    return task_description, actions


def load_local_legacy_checker_evidence(
    trace: DurableEventTrace,
    trace_root: Path | str,
) -> LegacyCheckerEvidence:
    """Materialize immutable local XML/UI/action evidence anchored to a durable trace."""

    if not isinstance(trace, DurableEventTrace):
        raise ValueError("trace must be a DurableEventTrace")
    trace.validate()
    if trace.capability_profile.integrity is TraceIntegrity.INVALID:
        raise LegacyCheckerAcquisitionError(
            LegacyCheckerAcquisitionFailureCode.INVALID_EVIDENCE,
            "G0-invalid durable trace cannot enter checker acquisition",
        )
    root = Path(trace_root)
    if not root.is_dir():
        raise LegacyCheckerAcquisitionError(
            LegacyCheckerAcquisitionFailureCode.SOURCE_UNREADABLE,
            "trace root does not exist",
        )
    frame_events = {
        event.frame_index: event
        for event in trace.events
        if isinstance(event, FrameEvidenceEvent)
    }
    action_events = {
        event.action_index: event
        for event in trace.events
        if isinstance(event, ActionEvidenceEvent)
    }
    task_description, recorded_actions = _read_actions(root)
    maximum = max(
        set(frame_events) | set(action_events) | set(recorded_actions), default=-1
    )
    frames = []
    for frame_index in range(maximum + 1):
        frame_event = frame_events.get(frame_index)
        screenshot_sha256: Optional[str] = None
        xml_text: Optional[str] = None
        xml_status = HierarchyEvidenceStatus.MISSING
        ui_state: dict[str, Any] = {}
        if frame_event is not None and frame_event.screenshot_ref is not None:
            screenshot_path = _safe_artifact(root, frame_event.screenshot_ref)
            try:
                screenshot_sha256 = hashlib.sha256(
                    screenshot_path.read_bytes()
                ).hexdigest()
            except OSError as exc:
                raise LegacyCheckerAcquisitionError(
                    LegacyCheckerAcquisitionFailureCode.SOURCE_UNREADABLE,
                    "referenced screenshot evidence is unreadable",
                ) from exc
        if frame_event is not None and frame_event.hierarchy_xml_ref is not None:
            path = _safe_artifact(root, frame_event.hierarchy_xml_ref)
            try:
                xml_text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                raise LegacyCheckerAcquisitionError(
                    LegacyCheckerAcquisitionFailureCode.SOURCE_UNREADABLE,
                    "referenced XML evidence is unreadable",
                ) from exc
            if not xml_text.strip():
                xml_status = HierarchyEvidenceStatus.EMPTY
            else:
                try:
                    ET.fromstring(xml_text)
                except ET.ParseError as exc:
                    raise LegacyCheckerAcquisitionError(
                        LegacyCheckerAcquisitionFailureCode.INVALID_EVIDENCE,
                        "malformed XML belongs to G0 trace integrity",
                    ) from exc
                xml_status = HierarchyEvidenceStatus.AVAILABLE
                package = re.search(r'package="([^"]+)"', xml_text)
                if package:
                    ui_state["package"] = package.group(1)
        action = recorded_actions.get(frame_index)
        event_action = action_events.get(frame_index)
        if action is None and event_action is not None:
            action = LegacyProcessActionEvidence(event_action.action_type, {})
        elif (
            action is not None
            and event_action is not None
            and (action.action_type != event_action.action_type)
        ):
            raise LegacyCheckerAcquisitionError(
                LegacyCheckerAcquisitionFailureCode.TRACE_MISMATCH,
                "actions.json type does not match durable action evidence",
            )
        frames.append(
            LegacyCheckerFrameEvidence(
                frame_index=frame_index,
                screenshot_sha256=screenshot_sha256,
                xml_text=xml_text,
                xml_status=xml_status,
                ui_state=ui_state,
                action=action,
            )
        )
    evidence = LegacyCheckerEvidence(
        trace_id=trace.trace_id,
        trace_sha256=event_trace_sha256(trace),
        frames=tuple(frames),
        capability_profile=trace.capability_profile,
        task_description=task_description,
        trace_contract_sha256=trace.contract_sha256,
    )
    evidence.validate()
    return evidence


class _UnavailableEvidence(Exception):
    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code


class _SourceEvidenceMissing(_UnavailableEvidence):
    """The trace/acquisition source omitted evidence required by a checker."""


def _schema(message: str) -> LegacyCheckerAcquisitionError:
    return LegacyCheckerAcquisitionError(
        LegacyCheckerAcquisitionFailureCode.INVALID_CHECKER_SCHEMA,
        message,
    )


def _exact_keys(params: Mapping[str, Any], allowed: set[str], context: str) -> None:
    unexpected = set(params) - allowed
    if unexpected:
        raise _schema(f"{context} has unknown parameters: {sorted(unexpected)}")


def _string_list(
    value: Any, context: str, *, allow_empty: bool = False
) -> Tuple[str, ...]:
    if not isinstance(value, tuple) or any(
        not isinstance(item, str) or not item for item in value
    ):
        raise _schema(f"{context} must be an immutable string list")
    if not allow_empty and not value:
        raise _schema(f"{context} must not be empty")
    return value


def _optional_string_lists(
    params: Mapping[str, Any],
    names: Tuple[str, ...],
) -> None:
    for name in names:
        if name in params:
            _string_list(params[name], name)


def _text_value(frame: LegacyCheckerFrameEvidence) -> str:
    if frame.outcome_text is None or not frame.outcome_text.strip():
        raise _SourceEvidenceMissing("outcome-text-unavailable")
    return frame.outcome_text


def _xml_value(frame: LegacyCheckerFrameEvidence) -> str:
    if frame.xml_status in (
        HierarchyEvidenceStatus.MISSING,
        HierarchyEvidenceStatus.EMPTY,
    ):
        raise _SourceEvidenceMissing(f"xml-{frame.xml_status.value.casefold()}")
    if frame.xml_status is HierarchyEvidenceStatus.MALFORMED:
        raise LegacyCheckerAcquisitionError(
            LegacyCheckerAcquisitionFailureCode.INVALID_EVIDENCE,
            "malformed XML must be handled by G0",
        )
    if frame.xml_text is None:
        raise _SourceEvidenceMissing("xml-unavailable")
    return frame.xml_text


def _evaluate_text(
    params: Mapping[str, Any], frame: LegacyCheckerFrameEvidence
) -> tuple[LegacyCheckerSignal, str, Tuple[str, ...]]:
    _exact_keys(params, {"any", "all"}, "text checker")
    _optional_string_lists(params, ("any", "all"))
    if not any(name in params for name in ("any", "all")):
        raise _schema("text checker must declare any or all")
    text = _text_value(frame)
    any_words = params.get("any", ())
    all_words = params.get("all", ())
    matched = (not any_words or any(word in text for word in any_words)) and (
        not all_words or all(word in text for word in all_words)
    )
    return (
        LegacyCheckerSignal.MATCH if matched else LegacyCheckerSignal.NO_MATCH,
        "substring-match" if matched else "substring-no-match",
        ("text",),
    )


def _compiled_regex(params: Mapping[str, Any]) -> re.Pattern[str]:
    _exact_keys(params, {"pattern", "ignore_case"}, "regex checker")
    pattern = params.get("pattern")
    if not isinstance(pattern, str) or not pattern:
        raise _schema("regex checker pattern must be non-empty")
    ignore_case = params.get("ignore_case", False)
    if not isinstance(ignore_case, bool):
        raise _schema("regex checker ignore_case must be boolean")
    try:
        return re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    except re.error as exc:
        raise _schema(f"regex checker pattern is invalid: {exc}") from exc


def _evaluate_regex(
    params: Mapping[str, Any], frame: LegacyCheckerFrameEvidence
) -> tuple[LegacyCheckerSignal, str, Tuple[str, ...]]:
    pattern = _compiled_regex(params)
    matched = pattern.search(_text_value(frame)) is not None
    return (
        LegacyCheckerSignal.MATCH if matched else LegacyCheckerSignal.NO_MATCH,
        "regex-match" if matched else "regex-no-match",
        ("text",),
    )


def _evaluate_ui(
    params: Mapping[str, Any], frame: LegacyCheckerFrameEvidence
) -> tuple[LegacyCheckerSignal, str, Tuple[str, ...]]:
    _exact_keys(params, {"key", "equals", "in"}, "ui checker")
    key = params.get("key")
    if not isinstance(key, str) or not key:
        raise _schema("ui checker key must be non-empty")
    if "equals" in params and "in" in params:
        raise _schema("ui checker cannot declare both equals and in")
    if "in" in params and (not isinstance(params["in"], tuple) or not params["in"]):
        raise _schema("ui checker in must be a non-empty immutable list")
    if key not in frame.ui_state or frame.ui_state[key] is None:
        raise _SourceEvidenceMissing("ui-key-unavailable")
    value = frame.ui_state[key]
    if "equals" in params:
        matched = value == params["equals"]
    elif "in" in params:
        matched = value in params["in"]
    else:
        matched = True
    return (
        LegacyCheckerSignal.MATCH if matched else LegacyCheckerSignal.NO_MATCH,
        "ui-exact-match" if matched else "ui-exact-no-match",
        (f"ui.{key}",),
    )


def _action_params(params: Mapping[str, Any]) -> Mapping[str, Any]:
    if set(params) == {"type", "params"} and params.get("type") == "action_match":
        nested = params.get("params")
        if not isinstance(nested, Mapping):
            raise _schema("nested action params must be an object")
        params = nested
    _exact_keys(params, {"type", "contains"}, "action checker")
    action_type = params.get("type")
    if action_type is not None and (
        not isinstance(action_type, str) or not action_type
    ):
        raise _schema("action checker type must be a non-empty string")
    contains = params.get("contains", MappingProxyType({}))
    if not isinstance(contains, Mapping):
        raise _schema("action checker contains must be an object")
    if action_type is None and not contains:
        raise _schema("action checker must declare type or contains")
    for key in contains:
        if any(marker in key.casefold() for marker in _DIAGNOSTIC_MARKERS):
            raise _UnavailableEvidence("diagnostic-action-field-forbidden")
    return params


def _evaluate_action(
    params: Mapping[str, Any], frame: LegacyCheckerFrameEvidence
) -> tuple[LegacyCheckerSignal, str, Tuple[str, ...]]:
    params = _action_params(params)
    if frame.action is None:
        raise _SourceEvidenceMissing("action-unavailable")
    expected_type = params.get("type")
    if expected_type is not None and frame.action.action_type != expected_type:
        return LegacyCheckerSignal.NO_MATCH, "action-type-no-match", ("action.type",)
    contains = params.get("contains", MappingProxyType({}))
    missing = tuple(key for key in contains if key not in frame.action.fields)
    if missing:
        raise _SourceEvidenceMissing("action-field-unavailable")
    matched = all(frame.action.fields[key] == value for key, value in contains.items())
    fields = ("action.type",) + tuple(f"action.{key}" for key in contains)
    return (
        LegacyCheckerSignal.MATCH if matched else LegacyCheckerSignal.NO_MATCH,
        "action-exact-match" if matched else "action-exact-no-match",
        fields,
    )


def _evaluate_xml(
    params: Mapping[str, Any], frame: LegacyCheckerFrameEvidence
) -> tuple[LegacyCheckerSignal, str, Tuple[str, ...]]:
    _exact_keys(params, {"any", "all", "none"}, "xml checker")
    _optional_string_lists(params, ("any", "all", "none"))
    if not any(name in params for name in ("any", "all", "none")):
        raise _schema("xml checker must declare any, all, or none")
    text = _xml_value(frame)
    none_words = params.get("none", ())
    if none_words and any(word in text for word in none_words):
        return (
            LegacyCheckerSignal.STRONG_CONTRADICTION,
            "explicit-xml-negative-assertion-triggered",
            ("xml_text",),
        )
    any_words = params.get("any", ())
    all_words = params.get("all", ())
    matched = (not any_words or any(word in text for word in any_words)) and (
        not all_words or all(word in text for word in all_words)
    )
    return (
        LegacyCheckerSignal.MATCH if matched else LegacyCheckerSignal.NO_MATCH,
        "xml-substring-match" if matched else "xml-substring-no-match",
        ("xml_text",),
    )


def _extract_task_keyword(text: str) -> Optional[str]:
    for pattern in (
        r"搜索([^，,。；;\s]+)",
        r"播放([^，,。；;\s]+)",
        r"打开([^，,。；;\s]+)",
        r"查看([^，,。；;\s]+)",
    ):
        match = re.search(pattern, text)
        if not match:
            continue
        keyword = re.sub(
            r"(并.*|且.*|然后.*|进入.*|打开.*|播放.*)$",
            "",
            match.group(1).strip(),
        ).strip()
        if len(keyword) >= 2:
            return keyword
    return None


def _normalized_match(
    keyword: str,
    combined: str,
    *,
    allow_compound_split: bool = False,
    min_char_coverage: float = 0.0,
    required_prefix_length: int = 0,
    prefix_aliases: Mapping[str, Any] = MappingProxyType({}),
) -> bool:
    normalized_keyword = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", keyword.casefold())
    normalized_combined = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", combined.casefold())
    if normalized_keyword in normalized_combined:
        return True
    if allow_compound_split and len(normalized_keyword) >= 4:
        midpoint = len(normalized_keyword) // 2
        first, second = normalized_keyword[:midpoint], normalized_keyword[midpoint:]
        start = normalized_combined.find(first)
        if start >= 0 and normalized_combined.find(second, start + len(first)) >= 0:
            return True
    if min_char_coverage <= 0 or not normalized_keyword:
        return False
    if len(set(normalized_keyword) & set(normalized_combined)) / len(
        set(normalized_keyword)
    ) < min(1.0, max(0.0, min_char_coverage)):
        return False
    if required_prefix_length:
        prefix = normalized_keyword[:required_prefix_length]
        candidates = [prefix]
        for alias in prefix_aliases.get(prefix, ()):
            normalized = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", alias.casefold())
            if normalized:
                candidates.append(normalized)
        if not any(candidate in normalized_combined for candidate in candidates):
            return False
    return True


def _dynamic_fields(params: Mapping[str, Any]) -> Tuple[str, ...]:
    fields = params.get("verification_fields", ("xml_text",))
    fields = _string_list(fields, "dynamic verification_fields")
    if len(fields) != len(set(fields)):
        raise _schema("dynamic verification_fields must be unique")
    if any(field not in _OUTCOME_FIELDS for field in fields):
        raise _UnavailableEvidence("diagnostic-or-unknown-verification-field-forbidden")
    return fields


def _combined_dynamic_text(
    fields: Tuple[str, ...], frame: LegacyCheckerFrameEvidence
) -> str:
    values = []
    for field in fields:
        values.append(_text_value(frame) if field == "text" else _xml_value(frame))
    return " ".join(values)


def _patterns(
    value: Any,
    context: str,
    *,
    default: Tuple[str, ...] = (),
    minimum_groups: int = 0,
) -> Tuple[str, ...]:
    patterns = default if value is None else _string_list(value, context)
    for pattern in patterns:
        try:
            compiled = re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            raise _schema(f"{context} contains invalid regex: {exc}") from exc
        if compiled.groups < minimum_groups:
            raise _schema(
                f"{context} patterns must contain at least {minimum_groups} capture group(s)"
            )
    return patterns


def _dynamic_common(params: Mapping[str, Any], allowed: set[str]) -> Tuple[str, ...]:
    _exact_keys(params, allowed | {"fallback_llm"}, "dynamic checker")
    if params.get("fallback_llm") is True:
        raise _UnavailableEvidence("fallback-llm-forbidden")
    if "fallback_llm" in params and params["fallback_llm"] is not False:
        raise _schema("dynamic fallback_llm must be boolean")
    source_field = params.get("source_field", "task_description")
    if source_field != "task_description":
        raise _UnavailableEvidence("diagnostic-or-unknown-source-field-forbidden")
    return _dynamic_fields(params)


def _evaluate_dynamic(
    params: Mapping[str, Any],
    frame: LegacyCheckerFrameEvidence,
    evidence: LegacyCheckerEvidence,
) -> tuple[LegacyCheckerSignal, str, Tuple[str, ...]]:
    extract = params.get("extract")
    if extract == "task_keyword":
        fields = _dynamic_common(
            params,
            {
                "extract",
                "source_field",
                "verification_fields",
                "require_markers",
                "allow_compound_split",
                "min_char_coverage",
                "required_prefix_length",
                "prefix_aliases",
            },
        )
        _optional_string_lists(params, ("require_markers",))
        allow_split = params.get("allow_compound_split", False)
        coverage = params.get("min_char_coverage", 0.0)
        prefix_length = params.get("required_prefix_length", 0)
        aliases = params.get("prefix_aliases", MappingProxyType({}))
        if not isinstance(allow_split, bool):
            raise _schema("allow_compound_split must be boolean")
        if (
            not isinstance(coverage, (int, float))
            or isinstance(coverage, bool)
            or not math.isfinite(coverage)
            or not 0 <= coverage <= 1
        ):
            raise _schema("min_char_coverage must be finite within [0,1]")
        if (
            not isinstance(prefix_length, int)
            or isinstance(prefix_length, bool)
            or prefix_length < 0
        ):
            raise _schema("required_prefix_length must be non-negative")
        if not isinstance(aliases, Mapping):
            raise _schema("prefix_aliases must be an object")
        for key, values in aliases.items():
            if not isinstance(key, str):
                raise _schema("prefix_alias keys must be strings")
            _string_list(values, "prefix alias values")
        task = evidence.task_description
        if task is None or not task.strip():
            raise _SourceEvidenceMissing("task-description-unavailable")
        keyword = _extract_task_keyword(task)
        combined = _combined_dynamic_text(fields, frame)
        matched = (
            bool(keyword)
            and _normalized_match(
                keyword or "",
                combined,
                allow_compound_split=allow_split,
                min_char_coverage=float(coverage),
                required_prefix_length=prefix_length,
                prefix_aliases=aliases,
            )
            and all(marker in combined for marker in params.get("require_markers", ()))
        )
    elif extract == "task_entity":
        fields = _dynamic_common(
            params,
            {
                "extract",
                "source_field",
                "verification_fields",
                "entity_patterns",
                "require_any",
                "require_all",
            },
        )
        _optional_string_lists(params, ("require_any", "require_all"))
        patterns = _patterns(
            params.get("entity_patterns"),
            "entity_patterns",
            default=(
                r"UP主(.+?)(?:的)?(?:个人)?主页",
                r"博主(.+?)(?:的)?(?:个人)?主页",
                r"进入(.+?)(?:的)?(?:个人)?主页",
            ),
            minimum_groups=1,
        )
        task = evidence.task_description
        if task is None or not task.strip():
            raise _SourceEvidenceMissing("task-description-unavailable")
        entity = None
        for pattern in patterns:
            match = re.search(pattern, task, re.IGNORECASE)
            if match:
                entity = match.group(1).strip(" ：:，,。；;\"'《》")
                if entity:
                    break
        combined = _combined_dynamic_text(fields, frame)
        matched = bool(entity) and _normalized_match(entity or "", combined)
        if params.get("require_any"):
            matched = matched and any(
                marker in combined for marker in params["require_any"]
            )
        matched = matched and all(
            marker in combined for marker in params.get("require_all", ())
        )
    elif extract == "task_entities":
        fields = _dynamic_common(
            params,
            {
                "extract",
                "source_field",
                "verification_fields",
                "entity_patterns",
                "require_any",
                "require_all",
                "require_entity_xml_node",
            },
        )
        _optional_string_lists(params, ("require_any", "require_all"))
        patterns = _patterns(
            params.get("entity_patterns"),
            "entity_patterns",
            minimum_groups=1,
        )
        if not patterns:
            raise _schema("task_entities requires entity_patterns")
        task = evidence.task_description
        if task is None or not task.strip():
            raise _SourceEvidenceMissing("task-description-unavailable")
        entities: list[str] = []
        for pattern in patterns:
            match = re.search(pattern, task, re.IGNORECASE)
            if match:
                entities = [
                    group.strip(" ：:，,。；;\"'《》")
                    for group in match.groups()
                    if group and group.strip(" ：:，,。；;\"'《》")
                ]
                if entities:
                    break
        entity_node = params.get("require_entity_xml_node")
        node_ok = True
        if entity_node is not None:
            if not isinstance(entity_node, Mapping):
                raise _schema("require_entity_xml_node must be an object")
            _exact_keys(
                entity_node,
                {"entity_index", "class", "resource_id", "attribute", "allow_missing"},
                "require_entity_xml_node",
            )
            for name in ("class", "resource_id", "attribute"):
                if name in entity_node and (
                    not isinstance(entity_node[name], str) or not entity_node[name]
                ):
                    raise _schema(f"require_entity_xml_node {name} must be non-empty")
            if "allow_missing" in entity_node and not isinstance(
                entity_node["allow_missing"], bool
            ):
                raise _schema("require_entity_xml_node allow_missing must be boolean")
            entity_index = entity_node.get("entity_index", 0)
            if (
                not isinstance(entity_index, int)
                or isinstance(entity_index, bool)
                or entity_index < 0
            ):
                raise _schema("entity_index must be non-negative")
            if not entities or entity_index >= len(entities):
                node_ok = False
            else:
                root = ET.fromstring(_xml_value(frame))
                candidates = []
                for node in root.iter("node"):
                    if (
                        entity_node.get("class")
                        and node.attrib.get("class") != entity_node["class"]
                    ):
                        continue
                    if (
                        entity_node.get("resource_id")
                        and node.attrib.get("resource-id") != entity_node["resource_id"]
                    ):
                        continue
                    candidates.append(node)
                attribute = entity_node.get("attribute", "text")
                expected = " ".join(entities[entity_index].split()).casefold()
                node_ok = any(
                    " ".join(node.attrib.get(attribute, "").split()).casefold()
                    == expected
                    for node in candidates
                )
                if not candidates and entity_node.get("allow_missing") is True:
                    node_ok = True
        combined = _combined_dynamic_text(fields, frame)
        matched = (
            bool(entities)
            and node_ok
            and all(_normalized_match(entity, combined) for entity in entities)
        )
        if params.get("require_any"):
            matched = matched and any(
                marker in combined for marker in params["require_any"]
            )
        matched = matched and all(
            marker in combined for marker in params.get("require_all", ())
        )
    elif extract in {"selected_control", "selected_controls"}:
        fields = _dynamic_common(
            params,
            {
                "extract",
                "source_field",
                "verification_fields",
                "control_patterns",
                "selected_suffix",
            },
        )
        patterns = _patterns(
            params.get("control_patterns"),
            "control_patterns",
            default=(r"(?:点击|选择)([^，,。；;\s]{1,16}?)(?:排序|筛选)",),
            minimum_groups=1,
        )
        suffix = params.get("selected_suffix", ",已选中")
        if not isinstance(suffix, str) or not suffix:
            raise _schema("selected_suffix must be non-empty")
        task = evidence.task_description
        if task is None or not task.strip():
            raise _SourceEvidenceMissing("task-description-unavailable")
        controls: list[str] = []
        for pattern in patterns:
            for match in re.finditer(pattern, task):
                control = match.group(1).strip()
                if control and control not in controls:
                    controls.append(control)
        if extract == "selected_control":
            controls = controls[:1]
        combined = _combined_dynamic_text(fields, frame)
        matched = bool(controls) and all(
            f"{control}{suffix}" in combined for control in controls
        )
    else:
        raise _UnavailableEvidence("dynamic-extract-variant-unsupported")
    return (
        LegacyCheckerSignal.MATCH if matched else LegacyCheckerSignal.NO_MATCH,
        "dynamic-match" if matched else "dynamic-no-match",
        fields,
    )


def _recorded_entry(
    checker: ContractCheckerIR,
    frame: LegacyCheckerFrameEvidence,
    *,
    node_id: str,
    recorded_context: Optional[RecordedProviderContext],
) -> RecordedOcrOutput | RecordedLlmOutput:
    if recorded_context is None:
        raise _UnavailableEvidence(f"{checker.checker_id}-provider-unavailable")
    binding = recorded_context.plan.binding_for(node_id, checker.checker_id)
    if binding is None:
        raise _UnavailableEvidence(f"{checker.checker_id}-provider-unconfigured")
    if frame.screenshot_sha256 is None:
        raise _SourceEvidenceMissing("screenshot-unavailable")
    key = EvidenceCacheKey(
        screenshot_sha256=frame.screenshot_sha256,
        model_version=binding.model_version,
        request_sha256=binding.request_sha256,
    )
    entry = recorded_context.storage.lookup(binding.provider_kind, key)
    if entry is None:
        raise _UnavailableEvidence(f"{checker.checker_id}-cache-miss")
    if not isinstance(entry.output, (RecordedOcrOutput, RecordedLlmOutput)):
        raise LegacyCheckerAcquisitionError(
            LegacyCheckerAcquisitionFailureCode.INVALID_EVIDENCE,
            "recorded provider output has an impossible schema",
        )
    return entry.output


def _evaluate_recorded_provider(
    checker: ContractCheckerIR,
    frame: LegacyCheckerFrameEvidence,
    *,
    node_id: str,
    recorded_context: Optional[RecordedProviderContext],
) -> tuple[LegacyCheckerSignal, str, Tuple[str, ...]]:
    params = checker.parameters
    if checker.checker_id == "ocr":
        _exact_keys(params, {"any", "all", "pattern", "ignore_case"}, "ocr checker")
        _optional_string_lists(params, ("any", "all"))
        if not any(name in params for name in ("any", "all", "pattern")):
            raise _schema("ocr checker must declare any, all, or pattern")
        pattern = params.get("pattern")
        if pattern is not None and (not isinstance(pattern, str) or not pattern):
            raise _schema("ocr checker pattern must be a non-empty string")
        ignore_case = params.get("ignore_case", False)
        if not isinstance(ignore_case, bool):
            raise _schema("ocr checker ignore_case must be boolean")
        compiled = None
        if pattern is not None:
            try:
                compiled = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
            except re.error as exc:
                raise _schema(f"ocr checker pattern is invalid: {exc}") from exc
        output = _recorded_entry(
            checker,
            frame,
            node_id=node_id,
            recorded_context=recorded_context,
        )
        if not isinstance(output, RecordedOcrOutput):
            raise LegacyCheckerAcquisitionError(
                LegacyCheckerAcquisitionFailureCode.INVALID_EVIDENCE,
                "OCR checker received a non-OCR cache output",
            )
        text = output.text.casefold() if ignore_case else output.text
        any_words = params.get("any", ())
        all_words = params.get("all", ())
        if ignore_case:
            any_words = tuple(word.casefold() for word in any_words)
            all_words = tuple(word.casefold() for word in all_words)
        matched = (
            (bool(any_words) and any(word in text for word in any_words))
            or (bool(all_words) and all(word in text for word in all_words))
            or (compiled is not None and compiled.search(output.text) is not None)
        )
        return (
            LegacyCheckerSignal.MATCH if matched else LegacyCheckerSignal.NO_MATCH,
            "recorded-ocr-match" if matched else "recorded-ocr-no-match",
            ("screenshot_sha256", "recorded_ocr"),
        )

    _exact_keys(params, {"prompt", "expected_true"}, "llm checker")
    prompt = params.get("prompt")
    expected_true = params.get("expected_true")
    if not isinstance(prompt, str) or not prompt.strip():
        raise _schema("llm checker prompt must be non-empty")
    if not isinstance(expected_true, bool):
        raise _schema("llm checker expected_true must be boolean")
    output = _recorded_entry(
        checker,
        frame,
        node_id=node_id,
        recorded_context=recorded_context,
    )
    if not isinstance(output, RecordedLlmOutput):
        raise LegacyCheckerAcquisitionError(
            LegacyCheckerAcquisitionFailureCode.INVALID_EVIDENCE,
            "LLM checker received a non-LLM cache output",
        )
    if output.decision is RecordedLlmDecision.UNKNOWN:
        raise _UnavailableEvidence("llm-recorded-unknown")
    decision = output.decision is RecordedLlmDecision.TRUE
    matched = decision is expected_true
    return (
        LegacyCheckerSignal.MATCH if matched else LegacyCheckerSignal.NO_MATCH,
        "recorded-llm-match" if matched else "recorded-llm-no-match",
        ("screenshot_sha256", "recorded_llm"),
    )


def _evaluate_visual_state_provider(
    checker: ContractCheckerIR,
    frame: LegacyCheckerFrameEvidence,
    *,
    node_id: str,
    visual_state_context: Optional[VisualStateProviderContext],
) -> tuple[LegacyCheckerSignal, str, Tuple[str, ...]]:
    if visual_state_context is None:
        raise _UnavailableEvidence("visual_state-provider-unavailable")
    binding = visual_state_context.plan.binding_for(node_id, checker.checker_id)
    if binding is None:
        raise _UnavailableEvidence("visual_state-provider-unconfigured")
    if frame.screenshot_sha256 is None:
        raise _SourceEvidenceMissing("screenshot-unavailable")
    key = VisualStateCacheKey(
        frame.screenshot_sha256,
        binding.detector_version,
        binding.request_sha256,
    )
    entry = visual_state_context.storage.lookup(key)
    if entry is None:
        raise _UnavailableEvidence("visual_state-cache-miss")
    if entry.output.decision is VisualStateDecision.LOADING_SKELETON:
        return (
            LegacyCheckerSignal.STRONG_CONTRADICTION,
            "recorded-visual-loading-skeleton",
            ("screenshot_sha256", "recorded_visual_state"),
        )
    return (
        LegacyCheckerSignal.MATCH,
        "recorded-visual-loaded-content",
        ("screenshot_sha256", "recorded_visual_state"),
    )


def _evaluate_checker(
    checker: ContractCheckerIR,
    frame: LegacyCheckerFrameEvidence,
    evidence: LegacyCheckerEvidence,
    *,
    node_id: str = "inventory",
    recorded_context: Optional[RecordedProviderContext] = None,
    visual_state_context: Optional[VisualStateProviderContext] = None,
) -> tuple[LegacyCheckerSignal, str, Tuple[str, ...]]:
    checker.validate()
    if checker.checker_id == "icons":
        raise _UnavailableEvidence(f"{checker.checker_id}-provider-unavailable")
    if checker.checker_id == "visual_state":
        return _evaluate_visual_state_provider(
            checker,
            frame,
            node_id=node_id,
            visual_state_context=visual_state_context,
        )
    if checker.checker_id in {"ocr", "llm"}:
        return _evaluate_recorded_provider(
            checker,
            frame,
            node_id=node_id,
            recorded_context=recorded_context,
        )
    if checker.checker_id not in _LOCAL_CHECKERS:
        raise _UnavailableEvidence("checker-unsupported")
    if checker.checker_id == "text":
        return _evaluate_text(checker.parameters, frame)
    if checker.checker_id == "regex":
        return _evaluate_regex(checker.parameters, frame)
    if checker.checker_id == "ui":
        return _evaluate_ui(checker.parameters, frame)
    if checker.checker_id == "action":
        return _evaluate_action(checker.parameters, frame)
    if checker.checker_id == "xml":
        return _evaluate_xml(checker.parameters, frame)
    return _evaluate_dynamic(checker.parameters, frame, evidence)


def acquire_legacy_checker_outcomes(
    contract: ContractIR,
    evidence: LegacyCheckerEvidence,
    *,
    recorded_context: Optional[RecordedProviderContext] = None,
    visual_state_context: Optional[VisualStateProviderContext] = None,
    classify_source_evidence_missing: bool = False,
) -> LegacyCheckerOutcomeTable:
    """Acquire signals from local evidence and an optional frozen local VCR cache."""

    if not isinstance(contract, ContractIR):
        raise ValueError("contract must be a ContractIR")
    contract.validate()
    if (
        contract.compiler_provenance is None
        or contract.compiler_provenance.source_type is not ContractSourceType.LEGACY
        or contract.dag is None
    ):
        raise ValueError(
            "checker acquisition requires a provenance-bound Legacy ContractIR"
        )
    if not isinstance(evidence, LegacyCheckerEvidence):
        raise ValueError("evidence must be LegacyCheckerEvidence")
    evidence.validate()
    if not isinstance(classify_source_evidence_missing, bool):
        raise ValueError("classify_source_evidence_missing must be boolean")
    if recorded_context is not None:
        if not isinstance(recorded_context, RecordedProviderContext):
            raise ValueError("recorded_context must be a RecordedProviderContext")
        recorded_context.validate_against(contract)
    if visual_state_context is not None:
        if not isinstance(visual_state_context, VisualStateProviderContext):
            raise ValueError(
                "visual_state_context must be a VisualStateProviderContext"
            )
        visual_state_context.validate_against(contract)
    if evidence.trace_contract_sha256 is not None and (
        evidence.trace_contract_sha256 != contract_sha256(contract)
    ):
        raise LegacyCheckerAcquisitionError(
            LegacyCheckerAcquisitionFailureCode.TRACE_MISMATCH,
            "durable evidence trace is bound to a different ContractIR",
        )
    if any(
        frame.xml_status is HierarchyEvidenceStatus.MALFORMED
        for frame in evidence.frames
    ):
        raise LegacyCheckerAcquisitionError(
            LegacyCheckerAcquisitionFailureCode.INVALID_EVIDENCE,
            "malformed XML must remain a G0 integrity failure",
        )
    preflight_frame = (
        evidence.frames[0] if evidence.frames else LegacyCheckerFrameEvidence(0)
    )
    for node in contract.dag.nodes:
        for checker in node.checkers:
            try:
                _evaluate_checker(
                    checker,
                    preflight_frame,
                    evidence,
                    node_id=node.node_id,
                    recorded_context=recorded_context,
                    visual_state_context=visual_state_context,
                )
            except _UnavailableEvidence:
                pass
    outcomes = []
    for node in contract.dag.nodes:
        for checker in node.checkers:
            for frame in evidence.frames:
                try:
                    signal, reason_code, fields = _evaluate_checker(
                        checker,
                        frame,
                        evidence,
                        node_id=node.node_id,
                        recorded_context=recorded_context,
                        visual_state_context=visual_state_context,
                    )
                except _SourceEvidenceMissing as exc:
                    signal = (
                        LegacyCheckerSignal.SOURCE_EVIDENCE_MISSING
                        if classify_source_evidence_missing
                        else LegacyCheckerSignal.UNAVAILABLE
                    )
                    reason_code = exc.reason_code
                    fields = ()
                except _UnavailableEvidence as exc:
                    signal = LegacyCheckerSignal.UNAVAILABLE
                    reason_code = exc.reason_code
                    fields = ()
                outcomes.append(
                    LegacyCheckerOutcome(
                        node_id=node.node_id,
                        checker_id=checker.checker_id,
                        frame_index=frame.frame_index,
                        signal=signal,
                        reason_code=reason_code,
                        evidence_fields=fields,
                    )
                )
    if recorded_context is not None and visual_state_context is not None:
        provider_id = COMPOSITE_EVIDENCE_PROVIDER_ID
        acquisition_version = COMPOSITE_EVIDENCE_ACQUISITION_VERSION
        provider_configuration_sha256 = composite_evidence_sha256(
            recorded_context.plan.plan_sha256,
            visual_state_context.plan.plan_sha256,
            identity_kind="PROVIDER_CONFIGURATION",
        )
        evidence_storage_sha256 = composite_evidence_sha256(
            recorded_context.storage.storage_sha256,
            visual_state_context.storage.storage_sha256,
            identity_kind="EVIDENCE_STORAGE",
        )
    elif recorded_context is not None:
        provider_id = RECORDED_PROVIDER_ID
        acquisition_version = RECORDED_PROVIDER_ACQUISITION_VERSION
        provider_configuration_sha256 = recorded_context.plan.plan_sha256
        evidence_storage_sha256 = recorded_context.storage.storage_sha256
    elif visual_state_context is not None:
        provider_id = VISUAL_STATE_PROVIDER_ID
        acquisition_version = VISUAL_STATE_ACQUISITION_VERSION
        provider_configuration_sha256 = visual_state_context.plan.plan_sha256
        evidence_storage_sha256 = visual_state_context.storage.storage_sha256
    else:
        provider_id = LOCAL_DETERMINISTIC_PROVIDER_ID
        acquisition_version = LEGACY_CHECKER_ACQUISITION_VERSION
        provider_configuration_sha256 = None
        evidence_storage_sha256 = None
    return bind_legacy_checker_outcomes(
        contract,
        tuple(outcomes),
        evidence_identity=evidence.identity,
        provider_id=provider_id,
        acquisition_version=acquisition_version,
        provider_configuration_sha256=provider_configuration_sha256,
        evidence_storage_sha256=evidence_storage_sha256,
    )


def acquire_and_evaluate_legacy_contract(
    contract: ContractIR,
    evidence: LegacyCheckerEvidence,
    *,
    deadline_reached: bool,
    recorded_context: Optional[RecordedProviderContext] = None,
    visual_state_context: Optional[VisualStateProviderContext] = None,
    classify_source_evidence_missing: bool = False,
) -> LegacyLoweringEvaluation:
    outcomes = acquire_legacy_checker_outcomes(
        contract,
        evidence,
        recorded_context=recorded_context,
        visual_state_context=visual_state_context,
        classify_source_evidence_missing=classify_source_evidence_missing,
    )
    return evaluate_lowered_legacy_contract(
        contract,
        outcomes,
        frame_count=len(evidence.frames),
        deadline_reached=deadline_reached,
        evidence_identity=evidence.identity,
        capability_profile=evidence.capability_profile,
    )


@dataclass(frozen=True)
class LegacyCheckerInventoryEntry:
    checker_id: str
    occurrence_count: int
    contract_count: int
    parameter_variants: Tuple[Tuple[str, ...], ...]
    required_evidence_fields: Tuple[str, ...]
    local_schema_supported: bool


def _inventory_support(checker: ContractCheckerIR) -> tuple[bool, Tuple[str, ...]]:
    if checker.checker_id in _EXTERNAL_CHECKERS:
        return False, ()
    dummy_frame = LegacyCheckerFrameEvidence(0)
    dummy_evidence = LegacyCheckerEvidence(
        trace_id="inventory",
        trace_sha256="0" * 64,
        frames=(dummy_frame,),
        capability_profile=EvidenceCapabilityProfile(integrity=TraceIntegrity.VALID),
        task_description="搜索示例",
    )
    try:
        _evaluate_checker(checker, dummy_frame, dummy_evidence)
    except _UnavailableEvidence as exc:
        if exc.reason_code in {
            "outcome-text-unavailable",
            "xml-missing",
            "ui-key-unavailable",
            "action-unavailable",
            "action-field-unavailable",
        }:
            fields = {
                "text": ("text",),
                "regex": ("text",),
                "ui": ("ui",),
                "action": ("action",),
                "xml": ("xml_text",),
            }.get(
                checker.checker_id,
                tuple(checker.parameters.get("verification_fields", ())),
            )
            return True, tuple(fields)
        return False, ()
    return True, ()


def legacy_checker_coverage_inventory(
    contracts: Tuple[ContractIR, ...],
) -> Tuple[LegacyCheckerInventoryEntry, ...]:
    if not isinstance(contracts, tuple):
        raise ValueError("inventory contracts must be an immutable tuple")
    occurrences: dict[str, int] = {}
    contract_ids: dict[str, set[str]] = {}
    variants: dict[str, set[Tuple[str, ...]]] = {}
    supported: dict[str, bool] = {}
    fields: dict[str, set[str]] = {}
    for contract in contracts:
        contract.validate()
        if contract.dag is None:
            continue
        seen = set()
        for node in contract.dag.nodes:
            for checker in node.checkers:
                checker_id = checker.checker_id
                occurrences[checker_id] = occurrences.get(checker_id, 0) + 1
                variants.setdefault(checker_id, set()).add(
                    tuple(sorted(checker.parameters))
                )
                seen.add(checker_id)
                is_supported, required_fields = _inventory_support(checker)
                supported[checker_id] = supported.get(checker_id, True) and is_supported
                fields.setdefault(checker_id, set()).update(required_fields)
        for checker_id in seen:
            contract_ids.setdefault(checker_id, set()).add(contract.contract_id)
    return tuple(
        LegacyCheckerInventoryEntry(
            checker_id=checker_id,
            occurrence_count=occurrences[checker_id],
            contract_count=len(contract_ids.get(checker_id, set())),
            parameter_variants=tuple(sorted(variants[checker_id])),
            required_evidence_fields=tuple(sorted(fields.get(checker_id, set()))),
            local_schema_supported=supported.get(checker_id, False),
        )
        for checker_id in sorted(occurrences)
    )


__all__ = [
    "LEGACY_CHECKER_ACQUISITION_VERSION",
    "LEGACY_CHECKER_EVIDENCE_SCHEMA_VERSION",
    "LOCAL_DETERMINISTIC_PROVIDER_ID",
    "LegacyCheckerAcquisitionError",
    "LegacyCheckerAcquisitionFailureCode",
    "LegacyCheckerEvidence",
    "LegacyCheckerFrameEvidence",
    "LegacyCheckerInventoryEntry",
    "LegacyProcessActionEvidence",
    "acquire_and_evaluate_legacy_contract",
    "acquire_legacy_checker_outcomes",
    "legacy_checker_coverage_inventory",
    "legacy_checker_evidence_payload",
    "legacy_checker_evidence_sha256",
    "load_local_legacy_checker_evidence",
]
