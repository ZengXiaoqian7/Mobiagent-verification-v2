from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Tuple

from .models import EvidenceCapabilityProfile, TraceIntegrity


_NUMERIC_ARTIFACT = re.compile(r"^(\d+)\.(jpg|json|xml)$", re.IGNORECASE)
_DIAGNOSTIC_FIELD_MARKERS = ("reason", "self_report", "verdict", "stop_reason", "done")


@dataclass(frozen=True)
class OutcomeFrameEvidence:
    frame_index: int
    screenshot_ref: Optional[str] = None
    hierarchy_raw_json_ref: Optional[str] = None
    hierarchy_xml_ref: Optional[str] = None


@dataclass(frozen=True)
class ProcessActionEvidence:
    action_index: int
    action_type: str
    screenshot_size: Optional[Tuple[int, int]] = None
    click_coordinate_size: Optional[Tuple[int, int]] = None


@dataclass(frozen=True)
class DiagnosticEvidenceSummary:
    react_ref: Optional[str]
    react_count: int
    excluded_field_names: Tuple[str, ...]
    declared_done_action_index: Optional[int]


@dataclass(frozen=True)
class TraceEvidenceBundle:
    trace_ref: str
    capability_profile: EvidenceCapabilityProfile
    outcome_frames: Tuple[OutcomeFrameEvidence, ...]
    process_actions: Tuple[ProcessActionEvidence, ...]
    run_timestamp: Optional[str]
    diagnostics: DiagnosticEvidenceSummary


def _read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as stream:
        return json.load(stream)


def _relative(path: Path, trace_dir: Path) -> str:
    return path.relative_to(trace_dir).as_posix()


def _pair(value: Any) -> Optional[Tuple[int, int]]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    if not all(isinstance(item, int) and not isinstance(item, bool) for item in value):
        return None
    return int(value[0]), int(value[1])


def _timestamp(value: Any) -> Optional[str]:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if not isinstance(value, Mapping):
        return None
    date = value.get("date")
    time = value.get("time")
    if isinstance(date, str) and isinstance(time, str) and date.strip() and time.strip():
        return f"{date.strip()}T{time.strip()}"
    return None


def _diagnostic_names(value: Any) -> set[str]:
    names: set[str] = set()
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = str(key).lower()
            if any(marker in normalized for marker in _DIAGNOSTIC_FIELD_MARKERS):
                names.add(str(key))
            names.update(_diagnostic_names(child))
    elif isinstance(value, list):
        for child in value:
            names.update(_diagnostic_names(child))
    return names


def _valid_jpeg(path: Path) -> bool:
    try:
        if path.stat().st_size < 4:
            return False
        with path.open("rb") as stream:
            start = stream.read(2)
            stream.seek(-2, 2)
            end = stream.read(2)
        return start == b"\xff\xd8" and end == b"\xff\xd9"
    except OSError:
        return False


def _classify_artifacts(trace_dir: Path) -> dict[int, dict[str, Path]]:
    artifacts: dict[int, dict[str, Path]] = {}
    for path in trace_dir.iterdir():
        if not path.is_file():
            continue
        match = _NUMERIC_ARTIFACT.match(path.name)
        if match:
            artifacts.setdefault(int(match.group(1)), {})[match.group(2).lower()] = path
    return artifacts


def load_trace_directory(path: Path | str, *, trace_ref: Optional[str] = None) -> TraceEvidenceBundle:
    """Read a Runner trace without exposing diagnostic text as outcome evidence."""

    trace_dir = Path(path)
    if not trace_dir.is_dir():
        raise ValueError(f"trace directory does not exist: {trace_dir}")

    artifacts = _classify_artifacts(trace_dir)
    corrupt: list[str] = []
    warnings: list[str] = []
    valid_screenshots: list[int] = []
    valid_raw_json: list[int] = []
    valid_xml: list[int] = []

    for index, kinds in sorted(artifacts.items()):
        jpg = kinds.get("jpg")
        if jpg is not None:
            if _valid_jpeg(jpg):
                valid_screenshots.append(index)
            else:
                corrupt.append(_relative(jpg, trace_dir))
        raw_json = kinds.get("json")
        if raw_json is not None:
            try:
                _read_json(raw_json)
                valid_raw_json.append(index)
            except (OSError, UnicodeError, json.JSONDecodeError):
                corrupt.append(_relative(raw_json, trace_dir))
        xml = kinds.get("xml")
        if xml is not None:
            try:
                if xml.read_bytes().strip():
                    ET.parse(xml)
                    valid_xml.append(index)
                else:
                    warnings.append(f"hierarchy XML empty at frame {index}")
            except (OSError, ET.ParseError):
                corrupt.append(_relative(xml, trace_dir))

    actions_path = trace_dir / "actions.json"
    actions_meta: Mapping[str, Any] = {}
    action_rows: list[Any] = []
    if actions_path.is_file():
        try:
            loaded_actions = _read_json(actions_path)
            if not isinstance(loaded_actions, Mapping) or not isinstance(loaded_actions.get("actions"), list):
                raise ValueError("actions.json must contain an object with an actions list")
            actions_meta = loaded_actions
            action_rows = loaded_actions["actions"]
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
            corrupt.append("actions.json")
    else:
        warnings.append("actions.json unavailable")

    process_actions = []
    declared_done_action_index = None
    for ordinal, row in enumerate(action_rows, 1):
        if not isinstance(row, Mapping):
            corrupt.append(f"actions.json#/{ordinal - 1}")
            continue
        raw_index = row.get("action_index", ordinal)
        action_index = raw_index if isinstance(raw_index, int) and not isinstance(raw_index, bool) else ordinal
        action_type = str(row.get("type") or "unknown")
        if action_type.lower() == "done":
            declared_done_action_index = action_index
        process_actions.append(
            ProcessActionEvidence(
                action_index=action_index,
                action_type=action_type,
                screenshot_size=_pair(row.get("screenshot_size")),
                click_coordinate_size=_pair(row.get("click_coordinate_size")),
            )
        )

    react_path = trace_dir / "react.json"
    react_rows: list[Any] = []
    diagnostic_names = _diagnostic_names(actions_meta)
    if react_path.is_file():
        try:
            loaded_react = _read_json(react_path)
            if not isinstance(loaded_react, list):
                raise ValueError("react.json must contain a list")
            react_rows = loaded_react
            diagnostic_names.update(_diagnostic_names(loaded_react))
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
            warnings.append("react.json unreadable (diagnostic-only)")
    else:
        warnings.append("react.json unavailable (diagnostic-only)")

    all_indices = sorted(set(valid_screenshots) | set(valid_raw_json) | set(valid_xml))
    outcome_frames = tuple(
        OutcomeFrameEvidence(
            frame_index=index,
            screenshot_ref=f"{index}.jpg" if index in valid_screenshots else None,
            hierarchy_raw_json_ref=f"{index}.json" if index in valid_raw_json else None,
            hierarchy_xml_ref=f"{index}.xml" if index in valid_xml else None,
        )
        for index in all_indices
    )
    if not valid_xml:
        warnings.append("hierarchy XML projection unavailable")
    if not valid_screenshots and not valid_raw_json and not valid_xml and not process_actions:
        warnings.append("no usable outcome or process evidence")

    if corrupt:
        integrity = TraceIntegrity.INVALID
    elif warnings:
        integrity = TraceIntegrity.DEGRADED
    else:
        integrity = TraceIntegrity.VALID
    timestamp = _timestamp(actions_meta.get("execution_timestamp"))
    timestamp_sources = ("actions.json:execution_timestamp",) if timestamp else ()
    profile = EvidenceCapabilityProfile(
        screenshot_frames=tuple(valid_screenshots),
        hierarchy_raw_json_frames=tuple(valid_raw_json),
        hierarchy_xml_frames=tuple(valid_xml),
        action_count=len(process_actions),
        react_count=len(react_rows),
        timestamp_sources=timestamp_sources,
        integrity=integrity,
        corrupt_artifacts=tuple(sorted(set(corrupt))),
        warnings=tuple(dict.fromkeys(warnings)),
    )
    return TraceEvidenceBundle(
        trace_ref=trace_ref or trace_dir.name,
        capability_profile=profile,
        outcome_frames=outcome_frames,
        process_actions=tuple(process_actions),
        run_timestamp=timestamp,
        diagnostics=DiagnosticEvidenceSummary(
            react_ref="react.json" if react_path.is_file() else None,
            react_count=len(react_rows),
            excluded_field_names=tuple(sorted(diagnostic_names)),
            declared_done_action_index=declared_done_action_index,
        ),
    )


def discover_trace_directories(root: Path | str) -> Tuple[Path, ...]:
    root_path = Path(root)
    if not root_path.is_dir():
        return ()
    candidates = set()
    for path in root_path.rglob("*"):
        if not path.is_file():
            continue
        if path.name in {"actions.json", "react.json"} or _NUMERIC_ARTIFACT.match(path.name):
            candidates.add(path.parent)
    return tuple(sorted(candidates, key=lambda item: item.as_posix()))


def capability_report(root: Path | str) -> dict[str, Any]:
    """Return only derived capability facts and relative source references."""

    root_path = Path(root)
    traces = []
    for trace_dir in discover_trace_directories(root_path):
        relative = trace_dir.relative_to(root_path).as_posix()
        bundle = load_trace_directory(trace_dir, trace_ref=relative)
        profile = bundle.capability_profile
        traces.append(
            {
                "trace_ref": relative,
                "integrity": profile.integrity.value,
                "screenshot_frames": len(profile.screenshot_frames),
                "hierarchy_raw_json_frames": len(profile.hierarchy_raw_json_frames),
                "hierarchy_xml_frames": len(profile.hierarchy_xml_frames),
                "action_count": profile.action_count,
                "react_count": profile.react_count,
                "timestamp_available": bool(profile.timestamp_sources),
                "available_capabilities": sorted(capability.value for capability in profile.available),
            }
        )
    return {"trace_count": len(traces), "traces": traces}
