"""Contract-driven layered evidence for semantic UI control states.

The evaluator is application-, device-, coordinate- and color-agnostic.  A
Contract declares the desired semantic state and how to obtain the target
anchor.  Runtime evidence then proceeds from accessibility state, through
anchored structure and stable local change, to an optional VLM fallback.
"""

from __future__ import annotations

import json
import math
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from PIL import Image, ImageChops, ImageStat

from .models import ContractIR
from .phase5_full_verifier_comparison import VisionCallRecorder
from .phase5_trace_case import load_actions
from .task_spec import TaskSpec


STATE_EVIDENCE_SCHEMA_VERSION = "mobiagent-layered-state-evidence-v2"
_MIN_VLM_FACT_CONFIDENCE = 0.75
_TRUE = frozenset({"true", "1", "yes", "on", "selected", "checked", "active"})
_FALSE = frozenset({"false", "0", "no", "off", "unselected", "unchecked", "inactive"})
_DIRECT_STATE_KEYS = ("selected", "checked", "activated", "active", "pressed", "toggled")
_TEXT_KEYS = (
    "text",
    "originalText",
    "description",
    "content-desc",
    "contentDescription",
    "accessibilityId",
    "id",
    "resource-id",
    "resourceId",
    "key",
    "value",
)
_ROLE_KEYS = ("type", "class", "role")
_STATEFUL_ROLES = ("tab", "checkbox", "radio", "switch", "toggle", "option", "menuitem")
_ROLE_ALIASES = {
    "sort": ("sort", "sorting", "rank", "ranking", "order", "排序", "排行", "排序项"),
    "filter": ("filter", "filtering", "facet", "category", "筛选", "过滤", "分类"),
    "tab": ("tab", "tabs", "section", "page tab", "标签", "栏目", "分区"),
    "toggle": ("switch", "toggle", "checkbox", "radio", "开关", "选项"),
}
_BOUNDS = re.compile(
    r"\[\s*(-?\d+)\s*,\s*(-?\d+)\s*\]\[\s*(-?\d+)\s*,\s*(-?\d+)\s*\]"
)


@dataclass(frozen=True)
class _Node:
    attributes: Mapping[str, str]
    parent: Optional[int]
    children: tuple[int, ...]


@dataclass(frozen=True)
class _StateSpec:
    criterion_id: str
    desired_state: str
    anchor_source: str
    anchors: tuple[str, ...]
    frame_scope: str
    focused_is_selected: bool = False
    allow_vlm: bool = True


def _as_bool(value: Any) -> Optional[bool]:
    normalized = str(value).strip().casefold()
    if normalized in _TRUE:
        return True
    if normalized in _FALSE:
        return False
    return None


def _normalized(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "")).casefold()


def _optional_fact_bool(value: Any) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    return _as_bool(value)


def _fact_confidence(value: Any) -> Optional[float]:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(confidence) or confidence < 0.0 or confidence > 1.0:
        return None
    return confidence


def _fact_string_list(value: Any) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    return tuple(
        str(item).strip()
        for item in value
        if isinstance(item, str) and str(item).strip()
    )


def _infer_expected_control_role(task_text: str, anchors: Sequence[str]) -> Optional[str]:
    text = _normalized(" ".join((task_text, *anchors)))
    for role, aliases in _ROLE_ALIASES.items():
        if any(_normalized(alias) in text for alias in aliases):
            return role
    return None


def _role_matches(value: Any, expected_role: str) -> bool:
    text = _normalized(value)
    if not text:
        return False
    return any(_normalized(alias) in text for alias in _ROLE_ALIASES[expected_role])


def _member_contains_label(member: str, labels: Sequence[str]) -> bool:
    normalized_member = _normalized(member)
    return any(
        normalized_label
        and (
            normalized_label in normalized_member
            or normalized_member in normalized_label
        )
        for normalized_label in (_normalized(label) for label in labels)
    )


def _exclusive_group_support(
    parsed: Mapping[str, Any],
    anchors: Sequence[str],
    peer_label: str,
    *,
    expected_control_role: Optional[str],
) -> Mapping[str, Any]:
    members = _fact_string_list(parsed.get("exclusive_group_members"))
    intervening_non_peers = _fact_string_list(parsed.get("intervening_non_peer_labels"))
    boundary_uncertain = _optional_fact_bool(
        parsed.get("exclusive_group_boundary_uncertain")
    )
    group_role = str(parsed.get("exclusive_group_role") or "").strip()
    target_role = str(parsed.get("target_control_role") or "").strip()
    peer_role = str(parsed.get("exclusive_peer_control_role") or "").strip()
    has_target_member = any(_member_contains_label(member, anchors) for member in members)
    has_peer_member = any(_member_contains_label(member, (peer_label,)) for member in members)

    role_supported = False
    if expected_control_role is not None:
        role_supported = all(
            _role_matches(role, expected_control_role)
            for role in (group_role, target_role, peer_role)
        )
    else:
        normalized_roles = {
            _normalized(role) for role in (target_role, peer_role) if _normalized(role)
        }
        role_supported = len(normalized_roles) == 1 and bool(normalized_roles)

    supported = bool(
        members
        and has_target_member
        and has_peer_member
        and role_supported
        and not intervening_non_peers
        and boundary_uncertain is not True
    )
    if not members:
        reason = "missing_exclusive_group_members"
    elif not has_target_member or not has_peer_member:
        reason = "group_members_do_not_contain_target_and_peer"
    elif not role_supported:
        reason = "target_peer_or_group_role_mismatch"
    elif intervening_non_peers:
        reason = "intervening_non_peer_controls"
    elif boundary_uncertain is True:
        reason = "exclusive_group_boundary_uncertain"
    else:
        reason = "supported"
    return {
        "supported": supported,
        "reason": reason,
        "expected_control_role": expected_control_role,
        "exclusive_group_members": list(members),
        "exclusive_group_role": group_role or None,
        "target_control_role": target_role or None,
        "exclusive_peer_control_role": peer_role or None,
        "intervening_non_peer_labels": list(intervening_non_peers),
        "exclusive_group_boundary_uncertain": boundary_uncertain,
    }


def _adjudicate_vlm_state_facts(
    parsed: Mapping[str, Any],
    anchors: Sequence[str],
    *,
    expected_control_role: Optional[str] = None,
) -> tuple[str, str, Mapping[str, Any]]:
    """Turn extracted visual facts into a status without trusting a model verdict."""

    facts = {
        "target_visible": _optional_fact_bool(parsed.get("target_visible")),
        "target_desired_state_visible": _optional_fact_bool(
            parsed.get("target_desired_state_visible")
        ),
        "target_clear_contrary_state_visible": _optional_fact_bool(
            parsed.get("target_clear_contrary_state_visible")
        ),
        "exclusive_peer_with_desired_state_visible": _optional_fact_bool(
            parsed.get("exclusive_peer_with_desired_state_visible")
        ),
        "same_exclusive_group": _optional_fact_bool(
            parsed.get("same_exclusive_group")
        ),
        "evidence_conflict": _optional_fact_bool(parsed.get("evidence_conflict")),
    }
    peer_label = str(parsed.get("exclusive_peer_label") or "").strip()
    normalized_anchors = {_normalized(anchor) for anchor in anchors if _normalized(anchor)}
    peer_is_distinct = bool(
        peer_label and _normalized(peer_label) not in normalized_anchors
    )
    confidence = _fact_confidence(parsed.get("confidence"))
    desired = facts["target_desired_state_visible"]
    contrary = facts["target_clear_contrary_state_visible"]
    peer_active = facts["exclusive_peer_with_desired_state_visible"]
    same_group = facts["same_exclusive_group"]
    group_support = _exclusive_group_support(
        parsed,
        anchors,
        peer_label,
        expected_control_role=expected_control_role,
    )
    supported_same_group_peer = bool(
        same_group is True
        and peer_active is True
        and peer_is_distinct
        and group_support["supported"] is True
    )
    explicit_conflict = facts["evidence_conflict"] is True
    inferred_conflict = bool(
        desired is True
        and (
            contrary is True
            or supported_same_group_peer
        )
    )

    status = "UNKNOWN_EVIDENCE"
    basis = "facts_incomplete_conflicting_or_low_confidence"
    reason = "structured visual facts do not establish the semantic target state"
    if (
        confidence is not None
        and confidence >= _MIN_VLM_FACT_CONFIDENCE
        and facts["target_visible"] is True
        and not explicit_conflict
        and not inferred_conflict
    ):
        if desired is True and contrary is not True:
            status = "SATISFIED"
            basis = "target_visibly_in_desired_state"
            reason = "structured visual facts show the semantic target in the desired state"
        elif desired is not True and contrary is True:
            status = "VIOLATED"
            basis = "target_visibly_in_clear_contrary_state"
            reason = "structured visual facts show the semantic target in a clear contrary state"
        elif (
            desired is False
            and supported_same_group_peer
        ):
            status = "VIOLATED"
            basis = "mutually_exclusive_peer_visibly_in_desired_state"
            reason = (
                "semantic target is not in the desired state while mutually exclusive "
                f"peer {peer_label!r} is visibly active"
            )

    audit = {
        **facts,
        "exclusive_peer_label": peer_label or None,
        "exclusive_peer_is_distinct": peer_is_distinct,
        "exclusive_group_support": group_support,
        "confidence": confidence,
        "minimum_confidence": _MIN_VLM_FACT_CONFIDENCE,
        "model_reason": str(parsed.get("reason") or ""),
        "decision_basis": basis,
        "status": status,
    }
    return status, reason, audit


def _task_control_anchors(task_text: str) -> tuple[str, ...]:
    """Extract a requested control phrase without enumerating control names."""

    patterns = (
        r"(?:选择|点击|切换到|切换为|启用|开启)\s*[“\"']?([^“”\"'，。；、]{1,24}?)[”\"']?\s*(?:排序|筛选|选项|标签|tab|开关|模式)",
        r"(?:将|把)\s*[“\"']?([^“”\"'，。；、]{1,24}?)[”\"']?\s*(?:设为|设置为)\s*(?:选中|开启|启用)",
    )
    values: list[str] = []
    for pattern in patterns:
        for match in re.findall(pattern, task_text, flags=re.IGNORECASE):
            value = str(match).strip()
            if value:
                values.append(value)
    return tuple(dict.fromkeys(values))


def _state_specs(contract: ContractIR, task: TaskSpec) -> tuple[_StateSpec, ...]:
    raw = contract.metadata.get("state_evidence")
    if not isinstance(raw, Mapping):
        return ()
    specs = []
    for criterion_id, value in raw.items():
        if not isinstance(criterion_id, str) or not isinstance(value, Mapping):
            continue
        desired = str(value.get("desired_state") or "selected").strip().casefold()
        if desired not in {"selected", "checked", "activated"}:
            continue
        source = str(value.get("anchor_source") or "explicit").strip()
        explicit = value.get("anchors") or ()
        anchors = tuple(
            str(item).strip()
            for item in explicit
            if isinstance(item, str) and item.strip()
        )
        if source == "task_control_phrase":
            anchors = tuple(dict.fromkeys(anchors + _task_control_anchors(task.task_text)))
        scope = str(value.get("frame_scope") or "terminal").strip()
        if scope not in {"source", "terminal"}:
            continue
        specs.append(
            _StateSpec(
                criterion_id=criterion_id,
                desired_state=desired,
                anchor_source=source,
                anchors=anchors,
                frame_scope=scope,
                focused_is_selected=value.get("focused_is_selected") is True,
                allow_vlm=value.get("allow_vlm") is not False,
            )
        )
    return tuple(specs)


def _mapping_attributes(value: Mapping[str, Any]) -> dict[str, str]:
    nested = value.get("attributes")
    source = nested if isinstance(nested, Mapping) else value
    return {
        str(key): str(child)
        for key, child in source.items()
        if not isinstance(child, (Mapping, list, tuple)) and child is not None
    }


def _json_nodes(path: Path) -> tuple[_Node, ...]:
    try:
        root = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return ()
    nodes: list[dict[str, Any]] = []

    def visit(value: Any, parent: Optional[int]) -> None:
        if isinstance(value, Mapping):
            index = len(nodes)
            nodes.append(
                {"attributes": _mapping_attributes(value), "parent": parent, "children": []}
            )
            if parent is not None:
                nodes[parent]["children"].append(index)
            children = value.get("children")
            if isinstance(children, Sequence) and not isinstance(children, (str, bytes)):
                for child in children:
                    visit(child, index)
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            for child in value:
                visit(child, parent)

    visit(root, None)
    return tuple(
        _Node(item["attributes"], item["parent"], tuple(item["children"]))
        for item in nodes
    )


def _xml_nodes(path: Path) -> tuple[_Node, ...]:
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return ()
    nodes: list[dict[str, Any]] = []

    def visit(element: ET.Element, parent: Optional[int]) -> None:
        index = len(nodes)
        nodes.append(
            {"attributes": dict(element.attrib), "parent": parent, "children": []}
        )
        if parent is not None:
            nodes[parent]["children"].append(index)
        for child in element:
            visit(child, index)

    visit(root, None)
    return tuple(
        _Node(item["attributes"], item["parent"], tuple(item["children"]))
        for item in nodes
    )


def _nodes(trace_root: Path, frame_index: int) -> tuple[_Node, ...]:
    json_nodes = _json_nodes(trace_root / f"{frame_index}.json")
    return json_nodes or _xml_nodes(trace_root / f"{frame_index}.xml")


def _node_text(node: _Node) -> str:
    return " ".join(node.attributes.get(key, "") for key in _TEXT_KEYS)


def _anchor_nodes(nodes: Sequence[_Node], anchors: Sequence[str]) -> tuple[int, ...]:
    normalized_anchors = tuple(_normalized(anchor) for anchor in anchors if _normalized(anchor))
    if not normalized_anchors:
        return ()
    return tuple(
        index
        for index, node in enumerate(nodes)
        if any(anchor in _normalized(_node_text(node)) for anchor in normalized_anchors)
    )


def _state_capable(node: _Node) -> bool:
    if _as_bool(node.attributes.get("checkable")) is True:
        return True
    role = " ".join(node.attributes.get(key, "") for key in _ROLE_KEYS).casefold()
    return any(marker in role for marker in _STATEFUL_ROLES)


def _direct_state(node: _Node, spec: _StateSpec) -> tuple[Optional[bool], Optional[str]]:
    keys = tuple(dict.fromkeys((spec.desired_state,) + _DIRECT_STATE_KEYS))
    for key in keys:
        if key not in node.attributes:
            continue
        value = _as_bool(node.attributes[key])
        if value is True:
            return True, key
        if value is False and _state_capable(node):
            return False, key
    if spec.focused_is_selected and _as_bool(node.attributes.get("focused")) is True:
        return True, "focused"
    return None, None


def _nearby_indices(nodes: Sequence[_Node], anchor: int) -> tuple[int, ...]:
    values = [anchor]
    parent = nodes[anchor].parent
    if parent is not None:
        values.append(parent)
        grandparent = nodes[parent].parent
        if grandparent is not None:
            values.append(grandparent)
    values.extend(nodes[anchor].children)
    return tuple(dict.fromkeys(values))


def _bounds(value: Any) -> Optional[tuple[int, int, int, int]]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) and len(value) == 4:
        try:
            result = tuple(int(item) for item in value)
        except (TypeError, ValueError):
            return None
        return result if result[2] > result[0] and result[3] > result[1] else None
    match = _BOUNDS.fullmatch(str(value or "").strip())
    if not match:
        return None
    result = tuple(int(item) for item in match.groups())
    return result if result[2] > result[0] and result[3] > result[1] else None


def _anchor_bounds(nodes: Sequence[_Node], anchor_indices: Sequence[int]) -> Optional[tuple[int, int, int, int]]:
    for index in anchor_indices:
        value = _bounds(nodes[index].attributes.get("bounds"))
        if value is not None:
            return value
    return None


def _nearest_action_bounds(
    actions: Sequence[Mapping[str, Any]], frame_index: int
) -> Optional[tuple[int, int, int, int]]:
    candidates = []
    for ordinal, action in enumerate(actions, 1):
        action_index = int(action.get("action_index") or ordinal)
        if action_index >= frame_index:
            continue
        if str(action.get("type") or "").casefold() not in {"click", "click_input"}:
            continue
        bounds = _bounds(action.get("bounds") or action.get("converted_bounds"))
        if bounds is not None:
            candidates.append((action_index, bounds))
    return max(candidates, default=(0, None), key=lambda item: item[0])[1]


def _crosses_app_boundary(
    actions: Sequence[Mapping[str, Any]], start: int, end: int
) -> bool:
    return any(
        start <= int(action.get("action_index") or ordinal) < end
        and str(action.get("type") or "").casefold() == "open_app"
        for ordinal, action in enumerate(actions, 1)
    )


def _expanded_roi(
    box: tuple[int, int, int, int], width: int, height: int
) -> tuple[int, int, int, int]:
    x1, y1, x2, y2 = box
    pad_x = max(2, math.ceil((x2 - x1) * 0.15))
    pad_y = max(2, math.ceil((y2 - y1) * 0.15))
    return max(0, x1 - pad_x), max(0, y1 - pad_y), min(width, x2 + pad_x), min(height, y2 + pad_y)


def _visual_difference(
    left_path: Path,
    right_path: Path,
    box: tuple[int, int, int, int],
) -> Optional[Mapping[str, Any]]:
    try:
        with Image.open(left_path) as left_image, Image.open(right_path) as right_image:
            left_image.load()
            right_image.load()
            width = min(left_image.width, right_image.width)
            height = min(left_image.height, right_image.height)
            roi = _expanded_roi(box, width, height)
            left = left_image.convert("RGB").crop(roi)
            right = right_image.convert("RGB").crop(roi)
            if left.size != right.size or left.width <= 0 or left.height <= 0:
                return None
            difference = ImageChops.difference(left, right)
            mean = sum(ImageStat.Stat(difference).mean) / (3.0 * 255.0)
            changed = sum(difference.convert("L").histogram()[12:])
            changed_ratio = changed / float(left.width * left.height)
            return {
                "roi_source": "semantic_anchor_or_action_bounds",
                "normalized_mean_difference": round(mean, 6),
                "changed_pixel_ratio": round(changed_ratio, 6),
                "changed": mean >= 0.01 or changed_ratio >= 0.02,
            }
    except (OSError, ValueError):
        return None


def _frame_indices(trace_root: Path) -> tuple[int, ...]:
    values = []
    for path in trace_root.glob("*.jpg"):
        if path.stem.isdigit():
            values.append(int(path.stem))
    return tuple(sorted(set(values)))


def _state_record(
    trace_root: Path,
    task: TaskSpec,
    spec: _StateSpec,
    frame_index: int,
    actions: Sequence[Mapping[str, Any]],
    recorder: Optional[VisionCallRecorder],
) -> Mapping[str, Any]:
    nodes = _nodes(trace_root, frame_index)
    anchors = _anchor_nodes(nodes, spec.anchors)
    layers: list[Mapping[str, Any]] = []
    decisive: Optional[bool] = None
    decisive_layer = ""

    direct_rows = []
    direct_values: list[bool] = []
    for anchor_index in anchors:
        for index in _nearby_indices(nodes, anchor_index):
            value, key = _direct_state(nodes[index], spec)
            if value is not None:
                direct_rows.append({"node_index": index, "state_key": key, "value": value})
                direct_values.append(value)
    if direct_values and len(set(direct_values)) == 1:
        decisive = direct_values[0]
        decisive_layer = "accessibility_state"
    layers.append(
        {
            "layer": "accessibility_state",
            "anchor_count": len(anchors),
            "state_observations": direct_rows,
            "conflicting": len(set(direct_values)) > 1,
        }
    )

    anchor_box = _anchor_bounds(nodes, anchors)
    action_box = _nearest_action_bounds(actions, frame_index)
    roi = anchor_box or action_box
    frames = _frame_indices(trace_root)
    previous = max((item for item in frames if item < frame_index), default=None)
    next_frame = min((item for item in frames if item > frame_index), default=None)
    transition = None
    persistence = None
    if roi is not None and previous is not None:
        transition = _visual_difference(
            trace_root / f"{previous}.jpg", trace_root / f"{frame_index}.jpg", roi
        )
    if (
        roi is not None
        and next_frame is not None
        and not _crosses_app_boundary(actions, frame_index, next_frame)
    ):
        persistence = _visual_difference(
            trace_root / f"{frame_index}.jpg", trace_root / f"{next_frame}.jpg", roi
        )
    stable_local_change = bool(
        anchors
        and transition
        and transition.get("changed") is True
        and persistence
        and persistence.get("changed") is False
    )
    layers.append(
        {
            "layer": "anchored_local_transition",
            "roi_available": roi is not None,
            "roi_from": "hierarchy_anchor" if anchor_box is not None else ("action_bounds" if action_box is not None else None),
            "previous_frame": previous,
            "next_same_app_frame": (
                next_frame
                if next_frame is not None and not _crosses_app_boundary(actions, frame_index, next_frame)
                else None
            ),
            "transition": transition,
            "persistence": persistence,
            "stable_local_change": stable_local_change,
        }
    )
    if decisive is None and stable_local_change:
        decisive = True
        decisive_layer = "anchored_stable_local_transition"

    status = (
        "SATISFIED"
        if decisive is True
        else ("VIOLATED" if decisive is False else "UNKNOWN_EVIDENCE")
    )
    reason = {
        "SATISFIED": f"semantic target state is supported by {decisive_layer}",
        "VIOLATED": f"semantic target state is contradicted by {decisive_layer}",
        "UNKNOWN_EVIDENCE": "layered deterministic evidence does not establish the semantic target state",
    }[status]

    if status == "UNKNOWN_EVIDENCE" and spec.allow_vlm and recorder is not None:
        images = tuple(
            trace_root / f"{item}.jpg"
            for item in (previous, frame_index)
            if item is not None and (trace_root / f"{item}.jpg").is_file()
        )
        if images:
            try:
                expected_control_role = _infer_expected_control_role(
                    task.task_text, spec.anchors
                )
                parsed = recorder.judge_json(
                    prompt=(
                        "Extract observable facts about one semantic mobile UI control state. "
                        "Do not output a pass/fail/unknown verdict.\n"
                        f"Task: {task.task_text}\n"
                        f"Semantic target anchors: {list(spec.anchors)}\n"
                        f"Desired state: {spec.desired_state}\n"
                        f"Expected control role: {expected_control_role or 'unknown'}\n"
                        "Images are ordered before then after when both exist. Report only visual "
                        "facts from the after image, using the before image as supporting context. "
                        "Mere presence, a tap, focus, or transient animation does not prove the "
                        "desired state. A mutually exclusive peer must belong to the same immediate "
                        "control group as the target; do not treat a highlighted control in another "
                        "filter, category, toolbar, or section as a peer. If the target belongs to "
                        "a sorting control group, a highlighted category/filter/tab is not a peer, "
                        "even when it is on the same row. Report the immediate group members and "
                        "control roles separately from the same-group boolean. A clear contrary state "
                        "means an explicit off/unselected alternative, not merely the absence of styling."
                    ),
                    images=images,
                    call_label=f"layered-state:{spec.criterion_id}",
                    schema_hint=(
                        '{"target_visible":true|false|null,'
                        '"target_desired_state_visible":true|false|null,'
                        '"target_clear_contrary_state_visible":true|false|null,'
                        '"exclusive_peer_with_desired_state_visible":true|false|null,'
                        '"exclusive_peer_label":"string or empty",'
                        '"same_exclusive_group":true|false|null,'
                        '"exclusive_group_members":["labels in immediate group"],'
                        '"exclusive_group_role":"sort|filter|tab|toggle|other|unknown",'
                        '"target_control_role":"sort|filter|tab|toggle|other|unknown",'
                        '"exclusive_peer_control_role":"sort|filter|tab|toggle|other|unknown",'
                        '"intervening_non_peer_labels":["labels"],'
                        '"exclusive_group_boundary_uncertain":true|false|null,'
                        '"evidence_conflict":true|false,'
                        '"reason":"short fact-only explanation","confidence":0.0}'
                    ),
                )
                vlm_status, vlm_reason, fact_audit = _adjudicate_vlm_state_facts(
                    parsed,
                    spec.anchors,
                    expected_control_role=expected_control_role,
                )
                layers.append(
                    {
                        "layer": "vlm_fact_extraction",
                        "facts": fact_audit,
                        "transition_corroboration": transition,
                    }
                )
                status = vlm_status
                reason = vlm_reason
            except Exception as exc:  # noqa: BLE001 - verifier must fail closed.
                layers.append(
                    {
                        "layer": "vlm_fact_extraction",
                        "facts": None,
                        "status": "UNKNOWN_EVIDENCE",
                        "error": type(exc).__name__,
                    }
                )

    return {
        "status": status,
        "reason": reason,
        "frame_index": frame_index,
        "evidence": {
            "schema_version": STATE_EVIDENCE_SCHEMA_VERSION,
            "desired_state": spec.desired_state,
            "anchor_source": spec.anchor_source,
            "anchors": list(spec.anchors),
            "layers": layers,
        },
    }


def evaluate_contract_state_evidence(
    case: Any,
    contract: ContractIR,
    task: TaskSpec,
    trace_root: Path,
    evidence_frames: Mapping[str, Any],
    recorder: Optional[VisionCallRecorder] = None,
) -> Mapping[str, Mapping[str, Any]]:
    """Evaluate every Contract-declared semantic control state in layer order."""

    del case  # Reserved for future source identity/audit bindings.
    _, actions = load_actions(trace_root)
    results: dict[str, Mapping[str, Any]] = {}
    for spec in _state_specs(contract, task):
        frame_value = evidence_frames.get(spec.frame_scope)
        if frame_value is None:
            results[spec.criterion_id] = {
                "status": "UNKNOWN_EVIDENCE",
                "reason": f"{spec.frame_scope} evidence frame is unavailable",
                "frame_index": int(evidence_frames.get("terminal") or 0),
                "evidence": {"schema_version": STATE_EVIDENCE_SCHEMA_VERSION},
            }
            continue
        results[spec.criterion_id] = _state_record(
            trace_root,
            task,
            spec,
            int(frame_value),
            actions,
            recorder,
        )
    return results


__all__ = [
    "STATE_EVIDENCE_SCHEMA_VERSION",
    "_adjudicate_vlm_state_facts",
    "evaluate_contract_state_evidence",
]
