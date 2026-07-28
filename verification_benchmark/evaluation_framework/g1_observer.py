"""Model-free, structure/ROI-aware G1 observation primitives.

The observer is deliberately read-only and fail-soft.  In particular, an empty
or malformed accessibility hierarchy is evidence degradation, not a trace
validity failure.  A caller-provided ROI can still be compared from screenshots.
"""

from __future__ import annotations

import hashlib
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Optional, Tuple

from PIL import Image

from .models import ObservationState, OverlayKind
from .runner_source import G1FrameContext, G1RegionOfInterest


_SPACE_PATTERN = re.compile(r"\s+")
_BOUNDS_PATTERN = re.compile(
    r"\[\s*(-?\d+)\s*,\s*(-?\d+)\s*\]\[\s*(-?\d+)\s*,\s*(-?\d+)\s*\]"
)


class HierarchyEvidenceStatus(str, Enum):
    AVAILABLE = "AVAILABLE"
    EMPTY = "EMPTY"
    MALFORMED = "MALFORMED"
    MISSING = "MISSING"


class G1EvidenceMode(str, Enum):
    STRUCTURE_AND_ROI = "STRUCTURE_AND_ROI"
    STRUCTURE_ONLY = "STRUCTURE_ONLY"
    SCREENSHOT_ROI_FALLBACK = "SCREENSHOT_ROI_FALLBACK"
    SCREENSHOT_ONLY_AUDIT = "SCREENSHOT_ONLY_AUDIT"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True)
class G1ObservationPolicy:
    """Conservative model-free markers and ROI comparison tolerance."""

    roi_hamming_threshold: int = 4
    loading_markers: Tuple[str, ...] = (
        "正在加载",
        "加载中",
        "请稍候",
        "loading",
    )
    app_overlay_markers: Tuple[str, ...] = (
        "popup",
        "modal",
        "dialog",
        "login_with_",
        "huaweiloginbutton",
    )
    system_overlay_markers: Tuple[str, ...] = (
        "permissioncontroller",
        "system_dialog",
        "permission_dialog",
    )

    def __post_init__(self) -> None:
        if not 0 <= self.roi_hamming_threshold <= 64:
            raise ValueError("roi_hamming_threshold must be between 0 and 64")


@dataclass(frozen=True)
class HierarchyDescriptor:
    status: HierarchyEvidenceStatus
    source_ref: Optional[str]
    structural_fingerprint: Optional[str]
    node_count: int
    semantic_tokens: Tuple[str, ...]
    loading_markers: Tuple[str, ...]
    app_overlay_markers: Tuple[str, ...]
    system_overlay_markers: Tuple[str, ...]
    diagnostic: Optional[str] = None


@dataclass(frozen=True)
class RoiVisualDescriptor:
    roi_id: str
    bounds: Tuple[int, int, int, int]
    source: str
    screenshot_ref: str
    pixel_sha256: str
    perceptual_hash: str


@dataclass(frozen=True)
class G1FrameDescriptor:
    frame_index: int
    hierarchy: HierarchyDescriptor
    roi_visuals: Tuple[RoiVisualDescriptor, ...]
    screenshot_sha256: Optional[str]
    evidence_mode: G1EvidenceMode
    observation_state: ObservationState
    overlay_kind: OverlayKind
    warnings: Tuple[str, ...] = ()


@dataclass(frozen=True)
class G1RoiComparison:
    roi_id: str
    hamming_distance: int
    exact_pixel_match: bool
    stable: bool


@dataclass(frozen=True)
class G1PairComparison:
    previous_frame_index: int
    current_frame_index: int
    structure_equal: Optional[bool]
    roi_comparisons: Tuple[G1RoiComparison, ...]
    observation_state: ObservationState
    overlay_kind: OverlayKind
    evidence_mode: G1EvidenceMode
    reasons: Tuple[str, ...]


def _normalise(value: Any) -> str:
    return _SPACE_PATTERN.sub(" ", str(value or "")).strip().casefold()


def _safe_artifact_path(trace_dir: Path, relative_ref: Optional[str]) -> Optional[Path]:
    if not relative_ref or "\\" in relative_ref:
        return None
    reference = PurePosixPath(relative_ref)
    if reference.is_absolute() or ".." in reference.parts:
        return None
    root = trace_dir.resolve()
    candidate = (root / Path(*reference.parts)).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate


def _iter_json_nodes(value: Any, path: Tuple[int, ...] = ()) -> Iterable[tuple[Tuple[int, ...], dict[str, Any]]]:
    if not isinstance(value, dict):
        return
    attributes = value.get("attributes")
    if isinstance(attributes, dict):
        yield path, attributes
    children = value.get("children", ())
    if isinstance(children, list):
        for index, child in enumerate(children):
            yield from _iter_json_nodes(child, path + (index,))


def _iter_xml_nodes(root: ET.Element) -> Iterable[tuple[Tuple[int, ...], dict[str, Any]]]:
    def walk(element: ET.Element, path: Tuple[int, ...]) -> Iterable[tuple[Tuple[int, ...], dict[str, Any]]]:
        if element.tag != "hierarchy" or element.attrib:
            yield path, dict(element.attrib)
        for index, child in enumerate(element):
            yield from walk(child, path + (index,))

    yield from walk(root, ())


def _normalised_bounds(value: Any, screenshot_size: Optional[Tuple[int, int]]) -> str:
    if isinstance(value, (list, tuple)) and len(value) == 4 and all(
        isinstance(item, (int, float)) and not isinstance(item, bool) for item in value
    ):
        coordinates = tuple(float(item) for item in value)
    else:
        rendered = str(value or "")
        match = _BOUNDS_PATTERN.fullmatch(rendered.strip())
        if match is None:
            return _normalise(value)
        coordinates = tuple(float(item) for item in match.groups())
    if screenshot_size is None:
        return ",".join(f"{value:g}" for value in coordinates)
    width, height = screenshot_size
    if width <= 0 or height <= 0:
        return _normalise(value)
    x1, y1, x2, y2 = coordinates
    normalised = (
        round(x1 * 10_000 / width),
        round(y1 * 10_000 / height),
        round(x2 * 10_000 / width),
        round(y2 * 10_000 / height),
    )
    return ",".join(str(item) for item in normalised)


def _canonical_node(
    path: Tuple[int, ...],
    attributes: dict[str, Any],
    screenshot_size: Optional[Tuple[int, int]],
) -> tuple[Any, ...]:
    def first(*names: str) -> str:
        for name in names:
            value = _normalise(attributes.get(name))
            if value:
                return value
        return ""

    return (
        path,
        first("type", "class"),
        first("id", "resource-id", "key", "accessibilityId"),
        _normalised_bounds(
            attributes.get("bounds") or attributes.get("origBounds"), screenshot_size
        ),
        first("text", "originalText"),
        first("description", "content-desc"),
        first("clickable"),
        first("enabled"),
        first("selected"),
        first("checked"),
        first("visible"),
    )


def _node_contributes_semantic_markers(attributes: Mapping[str, Any]) -> bool:
    """Exclude inert hierarchy nodes from loading/overlay detection.

    Mobile apps commonly keep containers such as ``loadingContainer`` mounted
    with ``visible=true`` while setting their opacity to zero.  They are useful
    structural evidence, but their resource IDs must not turn a completed page
    into a perpetual loading state.
    """

    visible = _normalise(attributes.get("visible"))
    if visible in {"false", "0", "no"}:
        return False
    opacity = attributes.get("opacity")
    if opacity is not None and str(opacity).strip():
        try:
            if float(str(opacity).strip()) <= 0.001:
                return False
        except ValueError:
            # Keep unknown platform-specific opacity values conservative: the
            # node remains searchable unless it is explicitly transparent.
            pass
    return True


def _hierarchy_from_nodes(
    nodes: Iterable[tuple[Tuple[int, ...], dict[str, Any]]],
    *,
    source_ref: str,
    policy: G1ObservationPolicy,
    screenshot_size: Optional[Tuple[int, int]],
) -> HierarchyDescriptor:
    raw_nodes = tuple(nodes)
    canonical = tuple(
        _canonical_node(path, attributes, screenshot_size)
        for path, attributes in raw_nodes
    )
    if not canonical:
        return HierarchyDescriptor(
            status=HierarchyEvidenceStatus.EMPTY,
            source_ref=source_ref,
            structural_fingerprint=None,
            node_count=0,
            semantic_tokens=(),
            loading_markers=(),
            app_overlay_markers=(),
            system_overlay_markers=(),
            diagnostic="hierarchy_has_no_nodes",
        )
    token_values = sorted(
        {
            value
            for (path, attributes), node in zip(raw_nodes, canonical)
            if _node_contributes_semantic_markers(attributes)
            for value in node[1:6]
            if isinstance(value, str) and value
        }
    )
    searchable = "\n".join(token_values)
    loading = tuple(marker for marker in policy.loading_markers if _normalise(marker) in searchable)
    app_overlay = tuple(
        marker for marker in policy.app_overlay_markers if _normalise(marker) in searchable
    )
    system_overlay = tuple(
        marker for marker in policy.system_overlay_markers if _normalise(marker) in searchable
    )
    encoded = json.dumps(canonical, ensure_ascii=False, separators=(",", ":"))
    return HierarchyDescriptor(
        status=HierarchyEvidenceStatus.AVAILABLE,
        source_ref=source_ref,
        structural_fingerprint=hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
        node_count=len(canonical),
        semantic_tokens=tuple(token_values),
        loading_markers=loading,
        app_overlay_markers=app_overlay,
        system_overlay_markers=system_overlay,
    )


def _read_hierarchy(
    trace_dir: Path,
    context: G1FrameContext,
    policy: G1ObservationPolicy,
) -> tuple[HierarchyDescriptor, Tuple[str, ...]]:
    warnings: list[str] = []
    raw_json = _safe_artifact_path(trace_dir, context.hierarchy_raw_json_ref)
    if raw_json is not None and raw_json.is_file():
        try:
            payload = json.loads(raw_json.read_text(encoding="utf-8"))
            descriptor = _hierarchy_from_nodes(
                _iter_json_nodes(payload),
                source_ref=context.hierarchy_raw_json_ref or "",
                policy=policy,
                screenshot_size=context.screenshot_size,
            )
            if descriptor.status is HierarchyEvidenceStatus.AVAILABLE:
                return descriptor, tuple(warnings)
            warnings.append("hierarchy_json_empty")
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError):
            warnings.append("hierarchy_json_malformed")

    xml_path = _safe_artifact_path(trace_dir, context.hierarchy_xml_ref)
    if xml_path is None or not xml_path.is_file():
        status = HierarchyEvidenceStatus.MISSING
        diagnostic = "hierarchy_artifact_missing"
    else:
        try:
            if not xml_path.read_bytes().strip():
                status = HierarchyEvidenceStatus.EMPTY
                diagnostic = "hierarchy_xml_empty"
            else:
                root = ET.parse(xml_path).getroot()
                descriptor = _hierarchy_from_nodes(
                    _iter_xml_nodes(root),
                    source_ref=context.hierarchy_xml_ref or "",
                    policy=policy,
                    screenshot_size=context.screenshot_size,
                )
                if descriptor.status is HierarchyEvidenceStatus.AVAILABLE:
                    return descriptor, tuple(warnings)
                status = HierarchyEvidenceStatus.EMPTY
                diagnostic = "hierarchy_xml_has_no_nodes"
        except (OSError, UnicodeError):
            status = HierarchyEvidenceStatus.MISSING
            diagnostic = "hierarchy_xml_unreadable"
        except (ET.ParseError, LookupError, ValueError):
            status = HierarchyEvidenceStatus.MALFORMED
            diagnostic = "hierarchy_xml_malformed"
    warnings.append(diagnostic)
    return (
        HierarchyDescriptor(
            status=status,
            source_ref=context.hierarchy_xml_ref,
            structural_fingerprint=None,
            node_count=0,
            semantic_tokens=(),
            loading_markers=(),
            app_overlay_markers=(),
            system_overlay_markers=(),
            diagnostic=diagnostic,
        ),
        tuple(warnings),
    )


def _dhash(image: Image.Image) -> str:
    grayscale = image.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
    pixels = grayscale.load()
    value = 0
    for row in range(8):
        for column in range(8):
            value = (value << 1) | int(
                pixels[column, row] > pixels[column + 1, row]
            )
    return f"{value:016x}"


def _roi_descriptor(
    image: Image.Image,
    screenshot_ref: str,
    roi: G1RegionOfInterest,
) -> Optional[RoiVisualDescriptor]:
    x1, y1, x2, y2 = roi.bounds
    width, height = image.size
    if x1 >= width or y1 >= height or x2 > width or y2 > height:
        return None
    crop = image.crop(roi.bounds).convert("RGB")
    pixel_digest = hashlib.sha256(crop.tobytes()).hexdigest()
    return RoiVisualDescriptor(
        roi_id=roi.roi_id,
        bounds=roi.bounds,
        source=roi.source,
        screenshot_ref=screenshot_ref,
        pixel_sha256=pixel_digest,
        perceptual_hash=_dhash(crop),
    )


def describe_g1_frame(
    trace_dir: Path | str,
    context: G1FrameContext,
    *,
    policy: G1ObservationPolicy = G1ObservationPolicy(),
) -> G1FrameDescriptor:
    """Build a G1 descriptor without ever treating hierarchy loss as invalid."""

    root = Path(trace_dir)
    hierarchy, hierarchy_warnings = _read_hierarchy(root, context, policy)
    warnings = list(hierarchy_warnings)
    screenshot_sha256: Optional[str] = None
    visuals: list[RoiVisualDescriptor] = []
    screenshot = _safe_artifact_path(root, context.screenshot_ref)
    if screenshot is not None and screenshot.is_file():
        try:
            screenshot_sha256 = hashlib.sha256(screenshot.read_bytes()).hexdigest()
            with Image.open(screenshot) as image:
                image.load()
                for roi in context.roi_context:
                    descriptor = _roi_descriptor(image, context.screenshot_ref or "", roi)
                    if descriptor is None:
                        warnings.append(f"roi_out_of_bounds:{roi.roi_id}")
                    else:
                        visuals.append(descriptor)
        except (OSError, ValueError):
            warnings.append("screenshot_unreadable")
            screenshot_sha256 = None
            visuals = []
    else:
        warnings.append("screenshot_missing")

    structure_available = hierarchy.status is HierarchyEvidenceStatus.AVAILABLE
    if structure_available and visuals:
        mode = G1EvidenceMode.STRUCTURE_AND_ROI
    elif structure_available:
        mode = G1EvidenceMode.STRUCTURE_ONLY
    elif visuals:
        mode = G1EvidenceMode.SCREENSHOT_ROI_FALLBACK
    elif screenshot_sha256:
        mode = G1EvidenceMode.SCREENSHOT_ONLY_AUDIT
    else:
        mode = G1EvidenceMode.UNAVAILABLE

    if hierarchy.system_overlay_markers:
        overlay = OverlayKind.SYSTEM_DIALOG
    elif hierarchy.app_overlay_markers:
        overlay = OverlayKind.APP_MODAL
    else:
        overlay = OverlayKind.NONE
    if hierarchy.loading_markers:
        state = ObservationState.STABLE_LOADING
    elif overlay is not OverlayKind.NONE:
        state = ObservationState.DEGRADED
    elif not structure_available:
        state = ObservationState.DEGRADED if screenshot_sha256 else ObservationState.UNKNOWN
    else:
        state = ObservationState.UNKNOWN
    return G1FrameDescriptor(
        frame_index=context.frame_index,
        hierarchy=hierarchy,
        roi_visuals=tuple(visuals),
        screenshot_sha256=screenshot_sha256,
        evidence_mode=mode,
        observation_state=state,
        overlay_kind=overlay,
        warnings=tuple(dict.fromkeys(warnings)),
    )


def _hamming_distance(left: str, right: str) -> int:
    return (int(left, 16) ^ int(right, 16)).bit_count()


def compare_g1_frames(
    previous: G1FrameDescriptor,
    current: G1FrameDescriptor,
    *,
    policy: G1ObservationPolicy = G1ObservationPolicy(),
) -> G1PairComparison:
    """Compare two descriptors; global screenshot equality is audit-only."""

    previous_rois = {roi.roi_id: roi for roi in previous.roi_visuals}
    current_rois = {roi.roi_id: roi for roi in current.roi_visuals}
    roi_comparisons = []
    for roi_id in sorted(previous_rois.keys() & current_rois.keys()):
        left = previous_rois[roi_id]
        right = current_rois[roi_id]
        distance = _hamming_distance(left.perceptual_hash, right.perceptual_hash)
        roi_comparisons.append(
            G1RoiComparison(
                roi_id=roi_id,
                hamming_distance=distance,
                exact_pixel_match=left.pixel_sha256 == right.pixel_sha256,
                stable=distance <= policy.roi_hamming_threshold,
            )
        )

    previous_fp = previous.hierarchy.structural_fingerprint
    current_fp = current.hierarchy.structural_fingerprint
    structure_equal = previous_fp == current_fp if previous_fp and current_fp else None
    degraded = (
        previous.hierarchy.status is not HierarchyEvidenceStatus.AVAILABLE
        or current.hierarchy.status is not HierarchyEvidenceStatus.AVAILABLE
    )
    reasons: list[str] = []
    if current.hierarchy.loading_markers:
        state = ObservationState.STABLE_LOADING
        reasons.append("explicit_loading_marker")
    elif current.overlay_kind is not OverlayKind.NONE:
        state = ObservationState.DEGRADED
        reasons.append("explicit_overlay_marker_requires_contract_reasoning")
    elif degraded and roi_comparisons:
        state = ObservationState.DEGRADED
        reasons.append("hierarchy_degraded_roi_visual_fallback")
    elif degraded:
        state = ObservationState.DEGRADED if current.screenshot_sha256 else ObservationState.UNKNOWN
        reasons.append("hierarchy_degraded_without_comparable_roi")
    elif structure_equal and roi_comparisons and all(item.stable for item in roi_comparisons):
        state = ObservationState.STABLE_SEMANTIC
        reasons.append("structure_and_contract_roi_stable")
    elif structure_equal and not roi_comparisons:
        state = ObservationState.UNKNOWN
        reasons.append("structure_stable_without_contract_roi")
    elif structure_equal is False or any(not item.stable for item in roi_comparisons):
        state = ObservationState.UNSTABLE_TRANSITION
        reasons.append("structure_or_contract_roi_changed")
    else:
        state = ObservationState.UNKNOWN
        reasons.append("insufficient_comparable_evidence")

    return G1PairComparison(
        previous_frame_index=previous.frame_index,
        current_frame_index=current.frame_index,
        structure_equal=structure_equal,
        roi_comparisons=tuple(roi_comparisons),
        observation_state=state,
        overlay_kind=current.overlay_kind,
        evidence_mode=current.evidence_mode,
        reasons=tuple(reasons),
    )
