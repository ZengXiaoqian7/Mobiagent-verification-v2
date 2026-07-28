"""Passive, one-way Runner directory side-channel for Audit mode only."""

from __future__ import annotations

import hashlib
import json
import queue
import re
import threading
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Optional, Tuple, Union

from PIL import Image

from .event_log import DurableEventTrace, trace_bundle_to_event_trace
from .models import ContractIR
from .trace_adapter import TraceEvidenceBundle, load_trace_directory


_NUMERIC_ARTIFACT = re.compile(r"^(\d+)\.(jpg|json|xml)$", re.IGNORECASE)


class RunnerArtifactKind(str, Enum):
    SCREENSHOT = "SCREENSHOT"
    HIERARCHY_RAW_JSON = "HIERARCHY_RAW_JSON"
    HIERARCHY_XML = "HIERARCHY_XML"


@dataclass(frozen=True)
class RunnerArtifactSnapshot:
    kind: RunnerArtifactKind
    relative_ref: str
    byte_size: int
    mtime_ns: int
    sha256: str
    readable: bool


@dataclass(frozen=True)
class G1RegionOfInterest:
    """Reserved contract/checker ROI context; the source does not invent ROIs."""

    roi_id: str
    bounds: Tuple[int, int, int, int]
    source: str

    def __post_init__(self) -> None:
        x1, y1, x2, y2 = self.bounds
        if not self.roi_id.strip() or not self.source.strip():
            raise ValueError("ROI id and source must be non-empty")
        if min(self.bounds) < 0 or x2 <= x1 or y2 <= y1:
            raise ValueError("ROI bounds must define a positive non-negative rectangle")


@dataclass(frozen=True)
class G1FrameContext:
    """Raw context for future structure/ROI-aware G1 classification.

    No global pixel-stability score is computed here. Relative artifact refs keep
    the complete screenshot and hierarchy available to a later classifier.
    """

    frame_index: int
    previous_frame_index: Optional[int]
    pre_action_index: int
    screenshot_ref: Optional[str]
    hierarchy_raw_json_ref: Optional[str]
    hierarchy_xml_ref: Optional[str]
    screenshot_size: Optional[Tuple[int, int]]
    artifacts: Tuple[RunnerArtifactSnapshot, ...]
    raw_context_complete: bool
    missing_context: Tuple[str, ...]
    structural_fingerprint: Optional[str] = None
    roi_context: Tuple[G1RegionOfInterest, ...] = ()
    observation_timestamp: Optional[float] = None
    timestamp_source: Optional[str] = None


@dataclass(frozen=True)
class RunnerFrameContextEvent:
    sequence_index: int
    trace_ref: str
    context: G1FrameContext


@dataclass(frozen=True)
class RunnerTraceFinalizedEvent:
    sequence_index: int
    trace_ref: str
    bundle: TraceEvidenceBundle


RunnerSourceEvent = Union[RunnerFrameContextEvent, RunnerTraceFinalizedEvent]


@dataclass(frozen=True)
class SideChannelStats:
    published: int
    dropped_attempts: int
    queued: int


class AuditEventSideChannel:
    """Bounded queue with a producer API that never waits or calls consumers."""

    def __init__(self, *, max_queue_size: int = 256) -> None:
        if max_queue_size <= 0:
            raise ValueError("max_queue_size must be positive")
        self._queue: queue.Queue[RunnerSourceEvent] = queue.Queue(maxsize=max_queue_size)
        self._lock = threading.Lock()
        self._published = 0
        self._dropped_attempts = 0

    def publish_nowait(self, event: RunnerSourceEvent) -> bool:
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            with self._lock:
                self._dropped_attempts += 1
            return False
        with self._lock:
            self._published += 1
        return True

    def drain(self, *, max_items: Optional[int] = None) -> Tuple[RunnerSourceEvent, ...]:
        if max_items is not None and max_items < 0:
            raise ValueError("max_items must be non-negative or null")
        items = []
        while max_items is None or len(items) < max_items:
            try:
                items.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return tuple(items)

    @property
    def stats(self) -> SideChannelStats:
        with self._lock:
            return SideChannelStats(
                published=self._published,
                dropped_attempts=self._dropped_attempts,
                queued=self._queue.qsize(),
            )


def _validate_trace_ref(trace_ref: str) -> None:
    if not isinstance(trace_ref, str) or not trace_ref.strip() or "\\" in trace_ref:
        raise ValueError("trace_ref must be a non-empty POSIX relative reference")
    reference = PurePosixPath(trace_ref)
    if reference.is_absolute() or ".." in reference.parts:
        raise ValueError("trace_ref must not escape its source root")


def _json_is_readable(path: Path) -> bool:
    try:
        with path.open("r", encoding="utf-8") as stream:
            json.load(stream)
        return True
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False


def _xml_is_readable(path: Path) -> bool:
    try:
        ET.parse(path)
        return True
    except (OSError, ET.ParseError):
        return False


def _screenshot_size(path: Path) -> Optional[Tuple[int, int]]:
    try:
        with Image.open(path) as image:
            width, height = image.size
        if width <= 0 or height <= 0:
            return None
        return int(width), int(height)
    except (OSError, ValueError):
        return None


def _artifact_snapshot(
    path: Path,
    trace_dir: Path,
    kind: RunnerArtifactKind,
) -> RunnerArtifactSnapshot:
    stat = path.stat()
    if kind is RunnerArtifactKind.SCREENSHOT:
        readable = _screenshot_size(path) is not None
    elif kind is RunnerArtifactKind.HIERARCHY_RAW_JSON:
        readable = _json_is_readable(path)
    else:
        readable = _xml_is_readable(path)
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return RunnerArtifactSnapshot(
        kind=kind,
        relative_ref=path.relative_to(trace_dir).as_posix(),
        byte_size=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
        sha256=digest.hexdigest(),
        readable=readable,
    )


class AuditRunnerDirectorySource:
    """Watch Runner outputs from a daemon sidecar without touching Runner code."""

    def __init__(
        self,
        trace_dir: Path | str,
        channel: AuditEventSideChannel,
        *,
        trace_ref: Optional[str] = None,
        poll_interval_seconds: float = 0.05,
    ) -> None:
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")
        self.trace_dir = Path(trace_dir)
        self.trace_ref = trace_ref or self.trace_dir.name
        _validate_trace_ref(self.trace_ref)
        self.channel = channel
        self.poll_interval_seconds = poll_interval_seconds
        self._emitted_frames: set[int] = set()
        self._next_sequence = 0
        self._finalized = False
        self._source_errors = 0
        self._stop = threading.Event()
        self._poll_lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None

    @property
    def finalized(self) -> bool:
        return self._finalized

    @property
    def source_errors(self) -> int:
        return self._source_errors

    @property
    def is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("AuditRunnerDirectorySource cannot be started twice")
        self._thread = threading.Thread(
            target=self._run,
            name=f"harmony-audit-source-{self.trace_dir.name}",
            daemon=True,
        )
        self._thread.start()

    def stop(self, *, join_timeout_seconds: float = 1.0) -> None:
        if join_timeout_seconds < 0:
            raise ValueError("join_timeout_seconds must be non-negative")
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=join_timeout_seconds)

    def _run(self) -> None:
        while not self._stop.is_set() and not self._finalized:
            try:
                self.poll_once()
            except Exception:  # noqa: BLE001 - an audit sidecar must fail away from Runner
                self._source_errors += 1
            self._stop.wait(self.poll_interval_seconds)

    def _artifact_map(self) -> dict[int, dict[str, Path]]:
        artifacts: dict[int, dict[str, Path]] = {}
        if not self.trace_dir.is_dir():
            return artifacts
        for path in self.trace_dir.iterdir():
            if not path.is_file():
                continue
            match = _NUMERIC_ARTIFACT.match(path.name)
            if match:
                artifacts.setdefault(int(match.group(1)), {})[match.group(2).lower()] = path
        return artifacts

    def _final_bundle_if_ready(self) -> Optional[TraceEvidenceBundle]:
        actions_path = self.trace_dir / "actions.json"
        react_path = self.trace_dir / "react.json"
        if not actions_path.is_file() or not react_path.is_file():
            return None
        try:
            with actions_path.open("r", encoding="utf-8") as stream:
                actions = json.load(stream)
            with react_path.open("r", encoding="utf-8") as stream:
                reacts = json.load(stream)
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        if not isinstance(actions, dict) or not isinstance(actions.get("actions"), list):
            return None
        if not isinstance(reacts, list):
            return None
        return load_trace_directory(self.trace_dir, trace_ref=self.trace_ref)

    def _frame_ready_without_finalization(self, artifacts: dict[str, Path]) -> bool:
        screenshot = artifacts.get("jpg")
        raw_json = artifacts.get("json")
        xml = artifacts.get("xml")
        if screenshot is None or _screenshot_size(screenshot) is None:
            return False
        if raw_json is not None and not _json_is_readable(raw_json):
            return False
        if xml is not None and not _xml_is_readable(xml):
            return False
        if raw_json is not None:
            return xml is not None
        return xml is not None

    def _build_context(
        self,
        frame_index: int,
        artifacts: dict[str, Path],
    ) -> G1FrameContext:
        kinds = {
            "jpg": RunnerArtifactKind.SCREENSHOT,
            "json": RunnerArtifactKind.HIERARCHY_RAW_JSON,
            "xml": RunnerArtifactKind.HIERARCHY_XML,
        }
        snapshots = tuple(
            _artifact_snapshot(artifacts[suffix], self.trace_dir, kinds[suffix])
            for suffix in ("jpg", "json", "xml")
            if suffix in artifacts
        )
        readable = {snapshot.kind for snapshot in snapshots if snapshot.readable}
        screenshot = artifacts.get("jpg")
        screenshot_ref = screenshot.name if screenshot is not None else None
        raw_json = artifacts.get("json")
        xml = artifacts.get("xml")
        missing = []
        if RunnerArtifactKind.SCREENSHOT not in readable:
            missing.append("screenshot")
        if not readable.intersection(
            {RunnerArtifactKind.HIERARCHY_RAW_JSON, RunnerArtifactKind.HIERARCHY_XML}
        ):
            missing.append("hierarchy")
        previous = max(self._emitted_frames) if self._emitted_frames else None
        screenshot_snapshot = next(
            (
                snapshot
                for snapshot in snapshots
                if snapshot.kind is RunnerArtifactKind.SCREENSHOT and snapshot.readable
            ),
            None,
        )
        return G1FrameContext(
            frame_index=frame_index,
            previous_frame_index=previous,
            pre_action_index=frame_index,
            screenshot_ref=screenshot_ref,
            hierarchy_raw_json_ref=raw_json.name if raw_json is not None else None,
            hierarchy_xml_ref=xml.name if xml is not None else None,
            screenshot_size=_screenshot_size(screenshot) if screenshot is not None else None,
            artifacts=snapshots,
            raw_context_complete=not missing,
            missing_context=tuple(missing),
            observation_timestamp=(
                screenshot_snapshot.mtime_ns / 1_000_000_000
                if screenshot_snapshot is not None
                else None
            ),
            timestamp_source=(
                "screenshot_mtime_ns" if screenshot_snapshot is not None else None
            ),
        )

    def poll_once(self) -> None:
        if not self._poll_lock.acquire(blocking=False):
            return
        try:
            self._poll_once_unlocked()
        finally:
            self._poll_lock.release()

    def _poll_once_unlocked(self) -> None:
        if self._finalized:
            return
        artifact_map = self._artifact_map()
        final_bundle = self._final_bundle_if_ready()
        indices = sorted(artifact_map)
        highest = indices[-1] if indices else None
        for frame_index in indices:
            if frame_index in self._emitted_frames:
                continue
            artifacts = artifact_map[frame_index]
            boundary_passed = highest is not None and frame_index < highest
            ready = (
                final_bundle is not None
                or boundary_passed
                or self._frame_ready_without_finalization(artifacts)
            )
            if not ready:
                continue
            event = RunnerFrameContextEvent(
                sequence_index=self._next_sequence,
                trace_ref=self.trace_ref,
                context=self._build_context(frame_index, artifacts),
            )
            if not self.channel.publish_nowait(event):
                return
            self._emitted_frames.add(frame_index)
            self._next_sequence += 1

        if final_bundle is not None and set(indices).issubset(self._emitted_frames):
            event = RunnerTraceFinalizedEvent(
                sequence_index=self._next_sequence,
                trace_ref=self.trace_ref,
                bundle=final_bundle,
            )
            if self.channel.publish_nowait(event):
                self._next_sequence += 1
                self._finalized = True


def assemble_finalized_runner_trace(
    event: RunnerTraceFinalizedEvent,
    contract: ContractIR,
    *,
    trace_id: str,
) -> DurableEventTrace:
    """One-way assembly: a finalized audit event can never affect Runner."""

    if event.trace_ref != event.bundle.trace_ref:
        raise ValueError("finalized event trace_ref does not match its evidence bundle")
    return trace_bundle_to_event_trace(event.bundle, contract, trace_id=trace_id)
