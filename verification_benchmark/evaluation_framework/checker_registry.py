"""Criterion-driven checker registry for non-specialized task families."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from .models import (
    ContractCheckerIR,
    ContractIR,
    DagDependencyMode,
    DagLogicalOperator,
    EvidenceCapability,
    TemporalSemantics,
    TraceIntegrity,
)
from .phase5_full_verifier_comparison import VisionCallRecorder
from .phase5_trace_case import CasePaths, load_actions
from .task_spec import TaskSpec
from .trace_adapter import TraceEvidenceBundle
from .state_evidence import evaluate_contract_state_evidence


CHECKER_REGISTRY_VERSION = "mobiagent-criterion-checker-registry-v3"
_STATUSES = {
    "SATISFIED",
    "VIOLATED",
    "UNKNOWN_EVIDENCE",
    "SOURCE_EVIDENCE_MISSING",
    "UNSUPPORTED_CAPABILITY",
}


def _record(
    status: str,
    reason: str,
    frame_index: int,
    *,
    evidence: Any = None,
) -> Mapping[str, Any]:
    return {
        "status": status if status in _STATUSES else "UNKNOWN_EVIDENCE",
        "reason": reason,
        "frame_index": frame_index,
        "evidence": evidence,
    }


def _visible_hierarchy_text(trace_root: Path, frame_index: int) -> str:
    xml_path = trace_root / f"{frame_index}.xml"
    values: list[str] = []
    if xml_path.is_file():
        try:
            root = ET.parse(xml_path).getroot()
            for node in root.iter():
                for key in ("text", "content-desc", "description", "value"):
                    value = node.attrib.get(key)
                    if value:
                        values.append(value)
        except (OSError, ET.ParseError):
            pass
    json_path = trace_root / f"{frame_index}.json"
    if json_path.is_file():
        # Raw hierarchy JSON layouts differ across Harmony versions.  Reading
        # only quoted strings keeps this layer deterministic and reasoning-free.
        try:
            values.extend(re.findall(r'"(?:text|content|description|value)"\s*:\s*"([^"\\]*)"', json_path.read_text(encoding="utf-8")))
        except (OSError, UnicodeError):
            pass
    return " ".join(values)


def _quoted_entities(task_text: str) -> tuple[str, ...]:
    values = re.findall(r"[“\"《']([^”\"》']{2,})[”\"》']", task_text)
    return tuple(dict.fromkeys(value.strip() for value in values if value.strip()))


def _task_entities(task: TaskSpec) -> tuple[str, ...]:
    values = list(_quoted_entities(task.task_text))
    intent = task.parsed_intent if isinstance(task.parsed_intent, Mapping) else {}
    entities = intent.get("entities", {})
    if isinstance(entities, Mapping):
        query = entities.get("query")
        if isinstance(query, str) and query.strip():
            values.append(query.strip())
        quoted = entities.get("quoted")
        if isinstance(quoted, Sequence) and not isinstance(quoted, (str, bytes)):
            values.extend(str(item).strip() for item in quoted if str(item).strip())
    return tuple(dict.fromkeys(values))


def _hierarchy_semantic_status(task: TaskSpec, visible: str) -> tuple[str, str]:
    if not visible.strip():
        return "UNKNOWN_EVIDENCE", "terminal hierarchy has no usable visible text"
    if task.task_family == "cross_app_entity_transfer":
        return (
            "UNKNOWN_EVIDENCE",
            "cross-App entity transfer requires correlated source and target evidence",
        )
    entities = _task_entities(task)
    matched = tuple(item for item in entities if item.casefold() in visible.casefold())
    markers = {
        "search_results": (
            "搜索",
            "综合",
            "相关",
            "结果",
            "详情",
            "播放",
            "暂停",
            "评论",
            "点赞",
            "收藏",
            "关注",
            "简介",
            "视频",
            "主页",
            "作品",
            "笔记",
            "价格",
            "地址",
            "路线",
            "电话",
        ),
        "open_detail": ("加入购物车", "立即购买", "去购买", "价格", "￥", "¥"),
        "select_control": ("销量", "价格", "筛选", "综合"),
        "creator_homepage": ("主页", "动态", "投稿", "笔记", "关注"),
        "playback": ("播放", "暂停", "歌曲", "单曲"),
        "location_search": ("路线", "地点", "地址", "导航"),
        "route_preview": ("路线", "公里", "分钟", "驾车", "公交", "步行"),
        "cross_app_entity_transfer": ("搜索", "相关", "结果"),
        "composite_workflow": ("结果", "商品", "详情", "购物车", "已选", "播放", "路线"),
    }.get(
        task.task_family,
        ("已开启", "已启用", "成功", "完成", "结果", "详情", "确认"),
    )
    marker = next((item for item in markers if item.casefold() in visible.casefold()), None)
    if matched and marker:
        return (
            "SATISFIED",
            f"structured hierarchy contains task entity and {task.task_family} surface marker",
        )
    return "UNKNOWN_EVIDENCE", "structured hierarchy does not establish the complete terminal semantics"


def _action_precheck(
    task: TaskSpec,
    actions: Sequence[Mapping[str, Any]],
    frame_index: int,
) -> Mapping[str, Any]:
    types = tuple(str(row.get("type") or "").lower() for row in actions)
    inputs = tuple(
        str(row.get("text") or "").strip()
        for row in actions
        if str(row.get("type") or "").lower() == "click_input"
        and str(row.get("text") or "").strip()
    )
    clicks = sum(item in {"click", "click_input"} for item in types)
    intent = task.parsed_intent if isinstance(task.parsed_intent, Mapping) else {}
    steps = tuple(str(item) for item in intent.get("workflow_steps", ()) if str(item))
    if task.task_family == "composite_workflow" and steps:
        required_input = any(step in steps for step in ("search_query", "place_lookup", "route_preview"))
        required_clicks = sum(
            step in steps
            for step in ("apply_rank_or_filter", "open_detail", "add_to_cart", "media_playback")
        )
        satisfied = (not required_input or bool(inputs)) and clicks >= max(1, required_clicks)
    elif task.task_family == "cross_app_entity_transfer":
        satisfied = "open_app" in types and len(inputs) >= 2
    elif task.task_family == "search_results":
        satisfied = bool(inputs) or clicks >= 1
    elif task.task_family == "route_preview":
        satisfied = bool(inputs) and clicks >= 2
    elif task.task_family in {"open_detail", "select_control", "creator_homepage", "playback"}:
        satisfied = bool(inputs) and clicks >= 2
    else:
        satisfied = bool(inputs)
    return _record(
        "SATISFIED" if satisfied else "VIOLATED",
        (
            "observable action sequence contains the task-family interaction"
            if satisfied
            else "observable action sequence is missing the required task-family interaction"
        ),
        frame_index,
        evidence={"action_types": list(types), "input_count": len(inputs)},
    )


_SUBMISSION_TERMS = (
    "发送",
    "发出",
    "发表",
    "发布",
    "评论",
    "回复",
    "发送按钮",
    "send",
    "post",
    "comment",
    "reply",
)

_EFFECTFUL_VISUAL_TERMS = (
    "加入购物车",
    "加到购物车",
    "添加到购物车",
    "购物车",
    "加购",
    "收藏",
    "已收藏",
    "点赞",
    "关注",
    "已关注",
    "喜欢",
    "发送",
    "发出",
    "发表",
    "发布",
    "评论",
    "回复",
    "提交",
    "确定",
    "应用",
    "筛选",
    "add to cart",
    "cart",
    "collect",
    "favorite",
    "like",
    "follow",
    "send",
    "submit",
    "post",
    "comment",
    "reply",
    "apply",
    "filter",
)


def _mentions_any(text: str, terms: Sequence[str]) -> bool:
    folded = text.casefold()
    return any(term.casefold() in folded for term in terms)


def _action_text(action: Mapping[str, Any]) -> str:
    values = []
    for key in (
        "type",
        "target_element",
        "text",
        "app_name",
        "resource_id",
        "content_desc",
    ):
        value = action.get(key)
        if value is not None:
            values.append(str(value))
    return " ".join(values)


def _is_effectful_visual_context(
    task: TaskSpec,
    criterion_id: str,
    criterion_description: str,
) -> bool:
    intent = task.parsed_intent if isinstance(task.parsed_intent, Mapping) else {}
    steps = tuple(str(item) for item in intent.get("workflow_steps", ()) if str(item))
    context = " ".join((task.task_text, criterion_id, criterion_description))
    return (
        any(
            step in steps
            for step in (
                "add_to_cart",
                "apply_rank_or_filter",
                "media_playback",
            )
        )
        or _mentions_any(context, _EFFECTFUL_VISUAL_TERMS)
    )


def _effectful_action_indices(
    actions: Sequence[Mapping[str, Any]],
) -> tuple[int, ...]:
    values = []
    for index, action in enumerate(actions):
        action_type = str(action.get("type") or "").lower()
        if action_type not in {"click", "click_input", "input"}:
            continue
        if _mentions_any(_action_text(action), _EFFECTFUL_VISUAL_TERMS):
            values.append(int(action.get("action_index", index + 1)))
    return tuple(values)


def _effect_delta_frame_pair(
    actions: Sequence[Mapping[str, Any]],
    frames: Sequence[int],
    *,
    prefer_last: bool = True,
) -> tuple[int, int, Mapping[str, Any]] | None:
    available = tuple(sorted(set(int(frame) for frame in frames)))
    if not available:
        return None
    indexed_actions = {
        int(action.get("action_index", index + 1)): action
        for index, action in enumerate(actions)
    }
    candidates = _effectful_action_indices(actions)
    if not candidates:
        return None
    ordered = tuple(reversed(candidates)) if prefer_last else candidates
    for action_index in ordered:
        before = max((frame for frame in available if frame <= action_index), default=None)
        after = min((frame for frame in available if frame > action_index), default=None)
        if before is not None and after is not None:
            return before, after, indexed_actions.get(action_index, {})
    return None


def _effectful_action_without_post_frame(
    actions: Sequence[Mapping[str, Any]],
    frames: Sequence[int],
) -> Mapping[str, Any] | None:
    available = tuple(sorted(set(int(frame) for frame in frames)))
    if not available:
        return None
    last_frame = max(available)
    indexed_actions = {
        int(action.get("action_index", index + 1)): action
        for index, action in enumerate(actions)
    }
    candidates = _effectful_action_indices(actions)
    if not candidates:
        return None
    last_action = max(candidates)
    if last_action >= last_frame:
        return indexed_actions.get(last_action, {})
    return None


def _effect_delta_vlm(
    recorder: VisionCallRecorder,
    task: TaskSpec,
    criterion_id: str,
    criterion_description: str,
    before_image: Path,
    after_image: Path,
    action: Mapping[str, Any],
) -> Mapping[str, Any]:
    parsed = recorder.judge_json(
        prompt=(
            "Compare two ordered mobile screenshots to verify an effectful UI action.\n"
            "Images are ordered BEFORE then AFTER. Judge only visible evidence; do not "
            "use the agent's claim.\n"
            f"Task: {task.task_text}\n"
            f"Criterion: {criterion_id}\n"
            f"Criterion description: {criterion_description}\n"
            f"Observed action: {_action_text(action)}\n"
            "Return SATISFIED only when the AFTER image clearly shows a new durable "
            "effect of this action that was absent in BEFORE, such as a newly appeared "
            "or increased badge/count near the acted control or related global control, "
            "a success toast, a selected/active state change, the requested item entering "
            "a cart/list/container, or an equivalent effect-visible state. For add-to-cart, "
            "a cart badge update, item-card quantity badge, add success message, or cart "
            "state containing the target item can be sufficient; entering checkout/payment "
            "is not required and may be prohibited. Return VIOLATED when AFTER clearly "
            "shows no relevant new effect, the action remains incomplete, the wrong item/"
            "control changed, or the UI is still an intermediate selector/input without "
            "the requested effect. Return UNKNOWN_EVIDENCE only when the change is too "
            "ambiguous, obscured, cropped, unreadable, or the before/after relationship "
            "cannot be established."
        ),
        images=(before_image, after_image),
        call_label=f"effect-delta:{task.task_family}:{criterion_id}",
        schema_hint=(
            '{"status":"SATISFIED|VIOLATED|UNKNOWN_EVIDENCE",'
            '"reason":"short before-after visual evidence explanation",'
            '"changed_element":"short label or empty","confidence":0.0}'
        ),
    )
    status = str(parsed.get("status") or "UNKNOWN_EVIDENCE").upper()
    if status not in {"SATISFIED", "VIOLATED", "UNKNOWN_EVIDENCE"}:
        status = "UNKNOWN_EVIDENCE"
    return {
        "status": status,
        "reason": str(parsed.get("reason") or "effect delta model returned no reason"),
        "confidence": parsed.get("confidence"),
        "changed_element": parsed.get("changed_element"),
    }


def _effect_delta_status(
    task: TaskSpec,
    criterion_id: str,
    criterion_description: str,
    trace_root: Path,
    actions: Sequence[Mapping[str, Any]],
    frames: Sequence[int],
    recorder: Optional[VisionCallRecorder],
) -> Mapping[str, Any] | None:
    if not _is_effectful_visual_context(task, criterion_id, criterion_description):
        return None
    pair = _effect_delta_frame_pair(actions, frames)
    if pair is not None and recorder is not None:
        before, after, action = pair
        before_image = trace_root / f"{before}.jpg"
        after_image = trace_root / f"{after}.jpg"
        if before_image.is_file() and after_image.is_file():
            semantic = _effect_delta_vlm(
                recorder,
                task,
                criterion_id,
                criterion_description,
                before_image,
                after_image,
                action,
            )
            return _record(
                str(semantic["status"]),
                str(semantic["reason"]),
                after,
                evidence={
                    "schema_version": "mobiagent-effect-delta-evidence-v1",
                    "before_frame": before,
                    "after_frame": after,
                    "action": dict(action),
                    "changed_element": semantic.get("changed_element"),
                    "confidence": semantic.get("confidence"),
                },
            )
    trailing_action = _effectful_action_without_post_frame(actions, frames)
    if trailing_action is not None:
        return _record(
            "VIOLATED",
            "effectful action was attempted but the trace has no post-action observation proving the requested effect",
            max(frames) if frames else 0,
            evidence={
                "schema_version": "mobiagent-effect-delta-evidence-v1",
                "missing_post_action_frame": True,
                "action": dict(trailing_action),
            },
        )
    return None


def _process_obligation_status(
    task: TaskSpec,
    criterion_id: str,
    criterion_description: str,
    actions: Sequence[Mapping[str, Any]],
    frame_index: int,
    *,
    done: bool,
) -> Mapping[str, Any]:
    """Evaluate process obligations by contract semantics, not id prefixes."""

    context = " ".join((task.task_text, criterion_id, criterion_description))
    if _mentions_any(context, _SUBMISSION_TERMS):
        matching_actions = tuple(
            int(action.get("action_index", index + 1))
            for index, action in enumerate(actions)
            if _mentions_any(_action_text(action), _SUBMISSION_TERMS)
        )
        if not matching_actions:
            return _record(
                "VIOLATED",
                "observable action sequence lacks the required submit/send/post interaction",
                frame_index,
                evidence={
                    "action_count": len(actions),
                    "required_interaction": "submit_or_send",
                },
            )
        if not done:
            return _record(
                "VIOLATED",
                "submit/send/post interaction was attempted but the trace never reached final done",
                frame_index,
                evidence={
                    "matching_action_indices": list(matching_actions),
                    "final_action_type": (
                        str(actions[-1].get("type") or "") if actions else None
                    ),
                },
            )
        return _record(
            "SATISFIED",
            "observable action sequence contains the required submit/send/post interaction and final done",
            frame_index,
            evidence={"matching_action_indices": list(matching_actions)},
        )
    return _action_precheck(task, actions, frame_index)


def _semantic_vlm(
    recorder: VisionCallRecorder,
    task: TaskSpec,
    criterion_id: str,
    images: Sequence[Path],
    *,
    criterion_description: str = "",
) -> Mapping[str, Any]:
    if criterion_id == "negative.forbidden_state_absent":
        instruction = (
            "Decide whether the terminal UI visibly avoids every explicit prohibited state "
            "in the task (for example starting navigation or opening a forbidden detail). "
            "SATISFIED requires visible support; VIOLATED requires a clear prohibited state."
        )
    elif task.task_family == "cross_app_entity_transfer":
        instruction = (
            "The images are ordered source-App evidence then terminal target-App evidence. "
            "Verify that the transferred entity is visibly supported by the source, that the "
            "target query/result refers to that same entity, and that the target state satisfies "
            "the task. Mere presence of two search pages is insufficient."
        )
    elif task.task_family == "search_results":
        instruction = (
            "Decide whether this stable terminal mobile UI visibly satisfies the requested "
            "search target and every mandatory constraint in the task, including route, "
            "date, time window, arrival/departure semantics, sort/filter mode, entity, "
            "location, quantity, and price/room/category constraints. Query submission/"
            "search action may be judged by a separate process criterion, so do not "
            "require the terminal screenshot itself to show the search box or query entry "
            "when a matching result page/detail is visible. A matching result detail, "
            "content, media playback, creator, item, or place landing page may satisfy "
            "the outcome when it visibly matches the requested entity and constraints. "
            "Return VIOLATED, not UNKNOWN_EVIDENCE, when the screenshot is readable and "
            "clearly shows a result/detail page with a wrong or missing mandatory "
            "constraint, a different active filter/sort from the requested one, an "
            "arrival-vs-departure mismatch, wrong route/date/entity, loading, unrelated "
            "vertical, or mere typed input. If the task asks for constrained search or "
            "filtering, success requires a visible applied constraint/filter or visible "
            "returned items that all comply without contradictory active controls. Use "
            "UNKNOWN_EVIDENCE only when the visible evidence is ambiguous, unreadable, "
            "obscured, cropped, or genuinely insufficient to decide."
        )
    else:
        instruction = (
            "Decide whether this stable terminal mobile UI visibly satisfies the complete "
            "task, including the requested entity and required page/state. Do not use the "
            "agent's claim. Loading, wrong entity, wrong page, or mere typed input is not success. "
            "Return VIOLATED, not UNKNOWN_EVIDENCE, when the screenshot is readable and "
            "clearly shows an incompatible state or incomplete intermediate state, such as "
            "an input/search form, editor/composer with unsent content, selector sheet, "
            "date/city picker, permission/login/ad interstitial, wrong route/date/entity, "
            "or missing submitted/sent/effect-visible state for an action task. Use "
            "UNKNOWN_EVIDENCE only when the visible evidence is ambiguous, unreadable, "
            "obscured, cropped, or genuinely insufficient to decide."
        )
    parsed = recorder.judge_json(
        prompt=(
            "You are a strict observer-only mobile task verifier.\n"
            f"Task: {task.task_text}\nCriterion: {criterion_id}\n"
            f"Parsed task intent: {dict(task.parsed_intent) if isinstance(task.parsed_intent, Mapping) else {}}\n"
            f"Criterion description: {criterion_description}\n{instruction}"
        ),
        images=images,
        call_label=f"generic:{task.task_family}:{criterion_id}",
        schema_hint=(
            '{"status":"SATISFIED|VIOLATED|UNKNOWN_EVIDENCE",'
            '"reason":"short visible-evidence explanation","confidence":0.0}'
        ),
    )
    status = str(parsed.get("status") or "UNKNOWN_EVIDENCE").upper()
    if status not in {"SATISFIED", "VIOLATED", "UNKNOWN_EVIDENCE"}:
        status = "UNKNOWN_EVIDENCE"
    return {
        "status": status,
        "reason": str(parsed.get("reason") or "semantic model returned no reason"),
        "confidence": parsed.get("confidence"),
    }


def _page_domain_spec(contract: ContractIR, criterion_id: str) -> Mapping[str, Any]:
    root = contract.metadata.get("page_domain_semantics")
    if not isinstance(root, Mapping):
        return {}
    value = root.get(criterion_id)
    return value if isinstance(value, Mapping) else {}


def _domain_marker_hits(specs: Any, visible: str) -> tuple[Mapping[str, Any], tuple[str, ...]] | None:
    if not isinstance(specs, Sequence) or isinstance(specs, (str, bytes)):
        return None
    folded = visible.casefold()
    for item in specs:
        if not isinstance(item, Mapping):
            continue
        any_markers = item.get("markers_any")
        all_markers = item.get("markers_all")
        if not isinstance(any_markers, Sequence) or isinstance(any_markers, (str, bytes)):
            any_markers = ()
        if not isinstance(all_markers, Sequence) or isinstance(all_markers, (str, bytes)):
            all_markers = ()
        all_hits = tuple(
            str(marker)
            for marker in all_markers
            if str(marker).strip() and str(marker).casefold() in folded
        )
        if len(all_hits) != len(tuple(marker for marker in all_markers if str(marker).strip())):
            continue
        any_hits = tuple(
            str(marker)
            for marker in any_markers
            if str(marker).strip() and str(marker).casefold() in folded
        )
        if any_markers and not any_hits:
            continue
        hits = all_hits + any_hits
        if hits:
            return item, hits
    return None


def _domain_descriptions(specs: Any) -> tuple[str, ...]:
    if not isinstance(specs, Sequence) or isinstance(specs, (str, bytes)):
        return ()
    values: list[str] = []
    for item in specs:
        if isinstance(item, Mapping):
            domain_id = str(item.get("domain_id") or "").strip()
            description = str(item.get("description") or "").strip()
            if domain_id and description:
                values.append(f"{domain_id}: {description}")
            elif domain_id:
                values.append(domain_id)
            elif description:
                values.append(description)
    return tuple(values)


def _page_domain_vlm(
    recorder: VisionCallRecorder,
    task: TaskSpec,
    criterion_id: str,
    image: Path,
    spec: Mapping[str, Any],
    *,
    criterion_description: str,
) -> Mapping[str, Any]:
    expected = _domain_descriptions(spec.get("expected"))
    prohibited = _domain_descriptions(spec.get("prohibited"))
    parsed = recorder.judge_json(
        prompt=(
            "You are a strict observer-only mobile task verifier.\n"
            "Judge the terminal page domain, not whether the agent claims success.\n"
            "A page domain is the generic UI/workflow surface currently shown, such as "
            "search results, item detail, media playback, route preview, local service "
            "delivery, taxi hailing, checkout, or active navigation.\n"
            f"Task: {task.task_text}\nCriterion: {criterion_id}\n"
            f"Criterion description: {criterion_description}\n"
            f"Expected page domains: {list(expected)}\n"
            f"Prohibited page domains: {list(prohibited)}\n"
            "Return VIOLATED if a prohibited or unrelated page domain is clearly visible. "
            "Return SATISFIED only when the expected generic page domain is clearly visible. "
            "Return UNKNOWN_EVIDENCE when the screenshot is ambiguous."
        ),
        images=(image,),
        call_label=f"generic:{task.task_family}:{criterion_id}",
        schema_hint=(
            '{"status":"SATISFIED|VIOLATED|UNKNOWN_EVIDENCE",'
            '"page_domain":"generic visible page/workflow domain",'
            '"expected_domain_visible":true,'
            '"prohibited_domain_visible":false,'
            '"reason":"short visible-evidence explanation","confidence":0.0}'
        ),
    )
    status = str(parsed.get("status") or "UNKNOWN_EVIDENCE").upper()
    if parsed.get("prohibited_domain_visible") is True:
        status = "VIOLATED"
    elif parsed.get("expected_domain_visible") is True and status != "VIOLATED":
        status = "SATISFIED"
    if status not in {"SATISFIED", "VIOLATED", "UNKNOWN_EVIDENCE"}:
        status = "UNKNOWN_EVIDENCE"
    page_domain = str(parsed.get("page_domain") or "").strip()
    reason = str(parsed.get("reason") or "page-domain model returned no reason")
    if page_domain:
        reason = f"{reason} page_domain={page_domain}"
    return {
        "status": status,
        "reason": reason,
        "confidence": parsed.get("confidence"),
    }


def _page_domain_status(
    contract: ContractIR,
    task: TaskSpec,
    criterion_id: str,
    visible: str,
    image: Path,
    recorder: Optional[VisionCallRecorder],
    *,
    frame_index: int,
    criterion_description: str,
) -> Mapping[str, Any]:
    spec = _page_domain_spec(contract, criterion_id)
    if not spec:
        return _record(
            "SATISFIED",
            "contract declares no explicit page-domain constraint",
            frame_index,
        )

    prohibited_hit = _domain_marker_hits(spec.get("prohibited"), visible)
    if prohibited_hit is not None:
        domain, hits = prohibited_hit
        return _record(
            "VIOLATED",
            (
                "terminal UI matches prohibited page domain "
                f"{domain.get('domain_id')!r} via visible markers {list(hits)}"
            ),
            frame_index,
            evidence={"domain": dict(domain), "markers": list(hits)},
        )

    expected_hit = _domain_marker_hits(spec.get("expected"), visible)
    if expected_hit is not None:
        domain, hits = expected_hit
        return _record(
            "SATISFIED",
            (
                "terminal UI matches expected page domain "
                f"{domain.get('domain_id')!r} via visible markers {list(hits)}"
            ),
            frame_index,
            evidence={"domain": dict(domain), "markers": list(hits)},
        )

    if recorder is not None and image.is_file():
        try:
            semantic = _page_domain_vlm(
                recorder,
                task,
                criterion_id,
                image,
                spec,
                criterion_description=criterion_description,
            )
            return _record(
                str(semantic["status"]),
                str(semantic["reason"]),
                frame_index,
                evidence={"confidence": semantic.get("confidence")},
            )
        except Exception as exc:  # noqa: BLE001
            return _record(
                "UNKNOWN_EVIDENCE",
                f"selective page-domain checker failed closed: {type(exc).__name__}",
                frame_index,
            )

    return _record(
        "UNKNOWN_EVIDENCE",
        "page-domain constraint requires terminal semantic evidence",
        frame_index,
    )


_FORBIDDEN_STATE_MARKERS = {
    "checkout": ("结算", "提交订单", "确认订单", "收银台"),
    "payment": ("支付", "付款", "收银台", "确认支付"),
    "order_submission": ("提交订单", "确认订单", "下单"),
    "active_navigation": ("退出导航", "继续导航", "导航中"),
    "detail_page": ("详情", "商品详情", "加入购物车", "立即购买"),
}


def _intent_forbidden_states(task: TaskSpec) -> tuple[str, ...]:
    intent = task.parsed_intent if isinstance(task.parsed_intent, Mapping) else {}
    values = intent.get("forbidden_states", ())
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        return ()
    return tuple(str(item) for item in values if str(item).strip())


def _forbidden_state_marker_hits(
    forbidden_states: Sequence[str], visible: str
) -> tuple[str, tuple[str, ...]] | None:
    folded = visible.casefold()
    for state in forbidden_states:
        markers = _FORBIDDEN_STATE_MARKERS.get(state, ())
        hits = tuple(
            marker for marker in markers if marker.casefold() in folded
        )
        if hits:
            return state, hits
    return None


def _logical_status(operator: DagLogicalOperator, values: Sequence[str]) -> str:
    if not values:
        return "UNKNOWN_EVIDENCE"
    if operator is DagLogicalOperator.ALL_OF:
        if "VIOLATED" in values:
            return "VIOLATED"
        if all(value == "SATISFIED" for value in values):
            return "SATISFIED"
        return "UNKNOWN_EVIDENCE"
    if "SATISFIED" in values:
        return "SATISFIED"
    if all(value == "VIOLATED" for value in values):
        return "VIOLATED"
    return "UNKNOWN_EVIDENCE"


def _parameter_text_match(checker: ContractCheckerIR, text: str) -> str:
    params = checker.parameters
    folded = text.casefold()
    any_values = tuple(str(value) for value in params.get("any", ()))
    all_values = tuple(str(value) for value in params.get("all", ()))
    none_values = tuple(str(value) for value in params.get("none", ()))
    if any(value.casefold() in folded for value in none_values):
        return "VIOLATED"
    any_ok = not any_values or any(value.casefold() in folded for value in any_values)
    all_ok = all(value.casefold() in folded for value in all_values)
    pattern = params.get("pattern")
    if pattern is not None:
        flags = re.IGNORECASE if params.get("ignore_case") is True else 0
        pattern_ok = re.search(str(pattern), text, flags) is not None
    else:
        pattern_ok = True
    return "SATISFIED" if any_ok and all_ok and pattern_ok else "VIOLATED"


def _dag_status(
    contract: ContractIR,
    task: TaskSpec,
    trace_root: Path,
    terminal: int,
    visible: str,
    recorder: Optional[VisionCallRecorder],
    actions: Sequence[Mapping[str, Any]],
    frames: Sequence[int],
) -> tuple[str, Mapping[str, str]]:
    dag = contract.dag
    if dag is None:
        return "UNKNOWN_EVIDENCE", {}
    xml_path = trace_root / f"{terminal}.xml"
    xml_text = xml_path.read_text(encoding="utf-8") if xml_path.is_file() else ""
    image = trace_root / f"{terminal}.jpg"
    node_conditions: dict[str, str] = {}
    node_results: dict[str, str] = {}
    for node_id in dag.topological_order():
        node = next(item for item in dag.nodes if item.node_id == node_id)
        checker_statuses = []
        for checker in node.checkers:
            if checker.checker_id in {"text", "ocr"}:
                checker_statuses.append(_parameter_text_match(checker, visible))
            elif checker.checker_id == "xml":
                checker_statuses.append(_parameter_text_match(checker, xml_text))
            elif checker.checker_id == "regex":
                checker_statuses.append(
                    _parameter_text_match(checker, visible + "\n" + xml_text)
                )
            elif checker.checker_id == "llm" and recorder is not None and image.is_file():
                try:
                    checker_prompt = str(checker.parameters.get("prompt") or "")
                    pair = (
                        _effect_delta_frame_pair(actions, frames)
                        if _is_effectful_visual_context(
                            task, "jit.dag_execution", checker_prompt
                        )
                        else None
                    )
                    if pair is not None:
                        before, after, action = pair
                        images = (
                            trace_root / f"{before}.jpg",
                            trace_root / f"{after}.jpg",
                        )
                        prompt = (
                            "Strictly judge this task-only JIT checker from ordered "
                            "before/after mobile UI screenshots.\n"
                            "Images are ordered BEFORE then AFTER. Use the before image "
                            "only as context for whether the requested effect newly "
                            "appears after the observed action.\n"
                            f"Task: {task.task_text}\n"
                            f"Observed action: {_action_text(action)}\n"
                            f"Checker: {checker_prompt}\n"
                            "Return SATISFIED only for a clear new durable effect of "
                            "the action, such as a new/increased badge, success toast, "
                            "selected/active state change, or requested item entering "
                            "a cart/list/container. Return VIOLATED for clear no-effect "
                            "or wrong/incomplete effect. Return UNKNOWN_EVIDENCE only "
                            "for ambiguous, obscured, cropped, or unreadable changes."
                        )
                    else:
                        images = (image,)
                        prompt = (
                            "Strictly judge this task-only JIT checker from visible UI.\n"
                            f"Task: {task.task_text}\n"
                            f"Checker: {checker_prompt}"
                        )
                    parsed = recorder.judge_json(
                        prompt=prompt,
                        images=images,
                        call_label=f"jit-dag:{node.node_id}:{checker.checker_id}",
                        schema_hint=(
                            '{"status":"SATISFIED|VIOLATED|UNKNOWN_EVIDENCE",'
                            '"reason":"short visible-evidence explanation"}'
                        ),
                    )
                    status = str(parsed.get("status") or "UNKNOWN_EVIDENCE").upper()
                    checker_statuses.append(
                        status if status in _STATUSES else "UNKNOWN_EVIDENCE"
                    )
                except Exception:  # noqa: BLE001 - model failure is unknown.
                    checker_statuses.append("UNKNOWN_EVIDENCE")
            else:
                checker_statuses.append("UNKNOWN_EVIDENCE")
        condition = _logical_status(node.condition_operator, checker_statuses)
        node_conditions[node_id] = condition
        mode, parents = dag.effective_dependency(node_id)
        if mode is DagDependencyMode.ROOT:
            node_results[node_id] = condition
        else:
            dependency_operator = (
                DagLogicalOperator.ALL_OF
                if mode is DagDependencyMode.ALL_OF
                else DagLogicalOperator.ANY_OF
            )
            dependency = _logical_status(
                dependency_operator, [node_results[parent] for parent in parents]
            )
            node_results[node_id] = _logical_status(
                DagLogicalOperator.ALL_OF, [dependency, condition]
            )
    final = _logical_status(
        dag.success.operator,
        [node_results[node_id] for node_id in dag.success.node_ids],
    )
    return final, node_results


@dataclass(frozen=True)
class CriterionCheckerRegistry:
    """Evaluate Contract criteria using strong facts before selective semantics."""

    version: str = CHECKER_REGISTRY_VERSION

    def evaluate(
        self,
        case: CasePaths,
        contract: ContractIR,
        task: TaskSpec,
        bundle: TraceEvidenceBundle,
        trace_root: Path,
        recorder: Optional[VisionCallRecorder],
    ) -> Mapping[str, Any]:
        contract.validate()
        task.validate()
        _, actions = load_actions(trace_root)
        frames = bundle.capability_profile.screenshot_frames
        terminal = max(frames) if frames else 0
        first = min(frames) if frames else terminal
        criteria: dict[str, Mapping[str, Any]] = {}
        done = bool(actions) and str(actions[-1].get("type") or "").lower() == "done"
        visible = _visible_hierarchy_text(trace_root, terminal)
        used_vlm = False
        has_prohibition = any(
            term in task.task_text for term in ("不要", "不得", "仅", "只搜索", "只查看")
        ) or bool(_intent_forbidden_states(task))
        semantic_frames = (terminal,)
        if task.task_family == "cross_app_entity_transfer":
            open_indices = tuple(
                int(row.get("action_index", index + 1))
                for index, row in enumerate(actions)
                if str(row.get("type") or "").lower() == "open_app"
            )
            if open_indices:
                source_candidates = tuple(
                    frame for frame in frames if frame <= open_indices[0]
                )
                if source_candidates:
                    semantic_frames = (max(source_candidates), terminal)

        dag_status, dag_nodes = _dag_status(
            contract, task, trace_root, terminal, visible, recorder, actions, frames
        )
        if contract.dag is not None and recorder is not None:
            used_vlm = any(
                checker.checker_id == "llm"
                for node in contract.dag.nodes
                for checker in node.checkers
            )

        layered_state = evaluate_contract_state_evidence(
            case,
            contract,
            task,
            trace_root,
            {"source": first, "terminal": terminal},
            recorder,
        )
        if recorder is not None:
            used_vlm = used_vlm or any(
                any(
                    layer.get("layer") == "vlm_fact_extraction"
                    for layer in record.get("evidence", {}).get("layers", ())
                    if isinstance(layer, Mapping)
                )
                for record in layered_state.values()
                if isinstance(record.get("evidence"), Mapping)
            )

        for criterion in contract.criteria:
            criterion_id = criterion.criterion_id
            if criterion_id in layered_state:
                criteria[criterion_id] = layered_state[criterion_id]
            elif criterion_id == "trace.integrity":
                criteria[criterion_id] = _record(
                    (
                        "VIOLATED"
                        if bundle.capability_profile.integrity is TraceIntegrity.INVALID
                        else "SATISFIED"
                    ),
                    "canonical trace acquisition integrity was checked",
                    first,
                )
            elif criterion_id.startswith("quality."):
                # G1 assembly contributes these observations directly.
                continue
            elif criterion_id.startswith("process."):
                criteria[criterion_id] = (
                    _action_precheck(task, actions, terminal)
                    if actions
                    else _record(
                        "UNSUPPORTED_CAPABILITY",
                        "actions evidence is unavailable",
                        terminal,
                    )
                )
            elif criterion_id.startswith("termination."):
                criteria[criterion_id] = _record(
                    "SATISFIED" if done and bool(frames) else "VIOLATED",
                    (
                        "done is final and follows terminal observable evidence"
                        if done and bool(frames)
                        else "done is missing, non-final, or lacks terminal observable evidence"
                    ),
                    terminal,
                )
            elif criterion_id == "jit.dag_execution":
                criteria[criterion_id] = _record(
                    dag_status,
                    "validated JIT DAG evaluated over terminal observable evidence",
                    terminal,
                    evidence={"node_statuses": dict(dag_nodes)},
                )
            elif (
                criterion.temporal_semantics is TemporalSemantics.PROCESS_OBLIGATION
                or EvidenceCapability.ACTIONS in criterion.required_capabilities
            ):
                criteria[criterion_id] = (
                    _process_obligation_status(
                        task,
                        criterion_id,
                        criterion.description,
                        actions,
                        terminal,
                        done=done,
                    )
                    if actions
                    else _record(
                        "UNSUPPORTED_CAPABILITY",
                        "actions evidence is unavailable",
                        terminal,
                    )
                )
            elif criterion_id.startswith("negative."):
                intent_forbidden = _intent_forbidden_states(task)
                forbidden_hit = _forbidden_state_marker_hits(intent_forbidden, visible)
                if forbidden_hit is not None:
                    state, hits = forbidden_hit
                    status = "VIOLATED"
                    reason = (
                        f"terminal UI shows forbidden state {state!r} via visible "
                        f"markers {list(hits)}"
                    )
                elif intent_forbidden:
                    status = "SATISFIED"
                    reason = (
                        "terminal UI has no visible markers for parsed forbidden "
                        f"states {list(intent_forbidden)}"
                    )
                elif not has_prohibition:
                    status = "SATISFIED"
                    reason = "task declares no explicit prohibited terminal state"
                elif recorder is not None and frames:
                    try:
                        semantic = _semantic_vlm(
                            recorder,
                            task,
                            criterion_id,
                            (trace_root / f"{terminal}.jpg",),
                            criterion_description=criterion.description,
                        )
                        status = str(semantic["status"])
                        reason = str(semantic["reason"])
                        used_vlm = True
                    except Exception as exc:  # noqa: BLE001
                        status = "UNKNOWN_EVIDENCE"
                        reason = (
                            "selective negative checker failed closed: "
                            f"{type(exc).__name__}"
                        )
                else:
                    status = "UNKNOWN_EVIDENCE"
                    reason = "explicit task prohibition requires terminal semantic evidence"
                criteria[criterion_id] = _record(status, reason, terminal)
            elif criterion_id == "outcome.page_domain_semantics":
                record = _page_domain_status(
                    contract,
                    task,
                    criterion_id,
                    visible,
                    trace_root / f"{terminal}.jpg",
                    recorder,
                    frame_index=terminal,
                    criterion_description=criterion.description,
                )
                criteria[criterion_id] = record
                used_vlm = used_vlm or (
                    recorder is not None
                    and record.get("evidence") is not None
                    and isinstance(record.get("evidence"), Mapping)
                    and "confidence" in record.get("evidence", {})
                )
            else:
                status, reason = _hierarchy_semantic_status(task, visible)
                if status == "UNKNOWN_EVIDENCE" and recorder is not None and frames:
                    try:
                        semantic = _semantic_vlm(
                            recorder,
                            task,
                            criterion_id,
                            tuple(
                                trace_root / f"{frame}.jpg"
                                for frame in semantic_frames
                            ),
                            criterion_description=criterion.description,
                        )
                        status = str(semantic["status"])
                        reason = str(semantic["reason"])
                        used_vlm = True
                    except Exception as exc:  # noqa: BLE001
                        reason = (
                            "selective semantic checker failed closed: "
                            f"{type(exc).__name__}"
                        )
                record = _record(status, reason, terminal)
                if status != "SATISFIED":
                    delta = _effect_delta_status(
                        task,
                        criterion_id,
                        criterion.description,
                        trace_root,
                        actions,
                        frames,
                        recorder,
                    )
                    if delta is not None:
                        record = delta
                        used_vlm = used_vlm or recorder is not None
                criteria[criterion_id] = record
        return {
            "run_id": task.task_id,
            "task_id": task.task_id,
            "verifier": "CRITERION_CHECKER_REGISTRY",
            "verifier_version": self.version,
            "criteria": criteria,
            "evidence_frames": {"source": first, "terminal": terminal},
            "used_selective_vlm": used_vlm,
        }


__all__ = ["CHECKER_REGISTRY_VERSION", "CriterionCheckerRegistry"]
