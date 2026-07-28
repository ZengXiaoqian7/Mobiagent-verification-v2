"""Task-only natural-language parsing into auditable verifier intent.

This parser is deliberately conservative and trace-blind.  It does not try to
solve an app; it extracts enough generic workflow structure for ContractIR
generation while leaving uncertain semantics to verifier evidence.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence, Tuple


TASK_INTENT_SCHEMA_VERSION = "mobiagent-task-intent-v1"


_STEP_ORDER = (
    "open_app",
    "search_query",
    "apply_rank_or_filter",
    "open_detail",
    "add_to_cart",
    "media_playback",
    "place_lookup",
    "route_preview",
    "cross_app_transfer",
)

_READ_ONLY_NEGATIONS = ("不要", "不得", "别", "不需要", "无需", "禁止")
_ORDER_COMMITMENT_STATE_TERMS = (
    "下单",
    "付款",
    "支付",
    "结算",
    "提交订单",
    "确认订单",
    "立即购买",
)


def _contains_any(text: str, terms: Sequence[str]) -> bool:
    return any(term in text for term in terms)


def _has_unnegated(text: str, terms: Sequence[str]) -> bool:
    for term in terms:
        index = text.find(term)
        if index < 0:
            continue
        clause_start = max(
            text.rfind(separator, 0, index)
            for separator in ("，", "。", "；", ";", ",", ".")
        )
        prefix = text[clause_start + 1 : index]
        if not _contains_any(prefix, _READ_ONLY_NEGATIONS):
            return True
    return False


def _quoted_entities(task_text: str) -> Tuple[str, ...]:
    values = re.findall(r"[“\"《']([^”\"》']{1,80})[”\"》']", task_text)
    return tuple(dict.fromkeys(value.strip() for value in values if value.strip()))


def _search_query(task_text: str, quoted: Sequence[str]) -> str:
    if quoted:
        return str(quoted[0])
    normalized = re.sub(r"(卖得最好|卖的最好|最热|热门|销量最高|销量最好)的?", "", task_text)
    patterns = (
        r"(?:搜索|搜一下|搜|查找|找一找|检索)(?:一下)?([^，。；、\s]+?)(?:然后|随后|并|后|$)",
        r"(?:买|看看|找)([^，。；、\s]{2,30}?)(?:然后|随后|并|后|$)",
    )
    for pattern in patterns:
        match = re.search(pattern, normalized)
        if match:
            value = match.group(1).strip("的地得")
            value = _strip_query_type_prefix(value)
            if value:
                return value
    return ""


def _strip_query_type_prefix(value: str) -> str:
    """Remove generic result-type words when they prefix an unquoted target title."""

    stripped = value.strip()
    prefixes = (
        "视频",
        "影片",
        "歌曲",
        "音乐",
        "单曲",
        "笔记",
        "帖子",
        "博主",
        "用户",
        "地点",
        "酒店",
        "机票",
    )
    title_starts = ("当", "我", "你", "他", "她", "它", "这", "那", "谁", "何", "王", "李", "张")
    for prefix in prefixes:
        if (
            stripped.startswith(prefix)
            and len(stripped) > len(prefix) + 1
            and stripped[len(prefix)] in title_starts
        ):
            return stripped[len(prefix) :].strip()
    return stripped


def _target_apps(initial_app: str, declared: Sequence[str], task_text: str) -> Tuple[str, ...]:
    apps = [item.strip() for item in declared if item and item.strip()]
    if initial_app.strip():
        apps.insert(0, initial_app.strip())
    # Generic app hints are task-only metadata, not sample labels.  They help
    # route contracts when the run manifest omitted target_apps.
    hints = (
        "淘宝",
        "天猫",
        "京东",
        "拼多多",
        "小红书",
        "哔哩哔哩",
        "B站",
        "网易云音乐",
        "高德地图",
        "天气",
    )
    apps.extend(term for term in hints if term in task_text)
    return tuple(dict.fromkeys(apps))


def _steps(task_text: str) -> Tuple[str, ...]:
    found: list[str] = []
    if _contains_any(task_text, ("打开", "进入", "去")):
        found.append("open_app")
    if _contains_any(task_text, ("搜索", "搜", "查找", "找一找", "检索")):
        found.append("search_query")
    if _contains_any(
        task_text,
        ("销量", "卖得最好", "卖的最好", "最热", "热门", "排序", "筛选", "价格最低", "价格最高"),
    ):
        found.append("apply_rank_or_filter")
    if _has_unnegated(task_text, ("详情页", "商品详情", "打开一个", "点进", "进入详情")):
        found.append("open_detail")
    if _has_unnegated(task_text, ("加入购物车", "加到购物车", "添加到购物车")):
        found.append("add_to_cart")
    if _has_unnegated(task_text, ("播放", "正在播放", "播放界面")):
        found.append("media_playback")
    if _contains_any(task_text, ("地点详情", "地址", "位置", "查找地点")):
        found.append("place_lookup")
    if _contains_any(task_text, ("路线预览", "路线方案", "从", "到")) and _contains_any(
        task_text, ("地图", "路线", "导航")
    ):
        found.append("route_preview")
    if _contains_any(task_text, ("随后", "跨应用", "跨 App", "open_app")):
        found.append("cross_app_transfer")
    ordered = [step for step in _STEP_ORDER if step in found]
    return tuple(dict.fromkeys(ordered))


def _selection_policy(task_text: str) -> Mapping[str, Any]:
    policy: dict[str, Any] = {}
    if _contains_any(task_text, ("销量", "卖得最好", "卖的最好")):
        policy["rank_by"] = "sales"
        policy["control_labels"] = ["销量"]
    elif _contains_any(task_text, ("最热", "热门")):
        policy["rank_by"] = "popularity"
        policy["control_labels"] = ["热门"]
    if _contains_any(task_text, ("价格最低", "最便宜")):
        policy["rank_by"] = "price_ascending"
        policy["control_labels"] = ["价格"]
    elif _contains_any(task_text, ("价格最高", "最贵")):
        policy["rank_by"] = "price_descending"
        policy["control_labels"] = ["价格"]
    if _contains_any(task_text, ("天猫", "官方", "旗舰店", "自营")):
        policy["filter_labels"] = [
            label for label in ("天猫", "官方", "旗舰店", "自营") if label in task_text
        ]
    return policy


def _forbidden_states(task_text: str, steps: Sequence[str]) -> Tuple[str, ...]:
    states: list[str] = []
    if "add_to_cart" in steps and not _has_unnegated(task_text, _ORDER_COMMITMENT_STATE_TERMS):
        states.extend(("checkout", "payment", "order_submission"))
    negative_clauses = tuple(
        clause
        for clause in re.split(r"[，。；;,.]", task_text)
        if _contains_any(clause, _READ_ONLY_NEGATIONS)
    )
    if any(_contains_any(clause, _ORDER_COMMITMENT_STATE_TERMS) for clause in negative_clauses):
        states.extend(("checkout", "payment", "order_submission"))
    if _contains_any(task_text, ("不要开始导航", "不要导航", "不开始导航")):
        states.extend(("active_navigation",))
    if _contains_any(task_text, ("不要打开商品详情", "不打开商品详情", "不打开详情")):
        states.extend(("detail_page",))
    return tuple(dict.fromkeys(states))


def _intent_family(steps: Sequence[str]) -> str:
    if len(steps) >= 2 and any(step in steps for step in ("add_to_cart", "open_detail", "apply_rank_or_filter")):
        return "composite_workflow"
    if "add_to_cart" in steps:
        return "composite_workflow"
    if "cross_app_transfer" in steps:
        return "cross_app_entity_transfer"
    if "route_preview" in steps:
        return "route_preview"
    if "place_lookup" in steps:
        return "location_search"
    if "media_playback" in steps:
        return "playback"
    if "open_detail" in steps:
        return "open_detail"
    if "apply_rank_or_filter" in steps:
        return "select_control"
    if "search_query" in steps:
        return "search_results"
    return "unseen"


@dataclass(frozen=True)
class ParsedTaskIntent:
    schema_version: str
    intent_family: str
    target_apps: Tuple[str, ...]
    entities: Mapping[str, Any]
    workflow_steps: Tuple[str, ...]
    selection_policy: Mapping[str, Any]
    forbidden_states: Tuple[str, ...]

    def payload(self) -> Mapping[str, Any]:
        return {
            "schema_version": self.schema_version,
            "intent_family": self.intent_family,
            "target_apps": list(self.target_apps),
            "entities": dict(self.entities),
            "workflow_steps": list(self.workflow_steps),
            "selection_policy": dict(self.selection_policy),
            "forbidden_states": list(self.forbidden_states),
        }

    @property
    def sha256(self) -> str:
        rendered = json.dumps(
            self.payload(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(rendered).hexdigest()


def parse_task_intent(
    task_text: str,
    *,
    initial_app: str = "",
    target_apps: Sequence[str] = (),
) -> ParsedTaskIntent:
    text = task_text.strip()
    quoted = _quoted_entities(text)
    steps = _steps(text)
    query = _search_query(text, quoted)
    entities: dict[str, Any] = {}
    if query:
        entities["query"] = query
    if quoted:
        entities["quoted"] = list(quoted)
    policy = _selection_policy(text)
    forbidden = _forbidden_states(text, steps)
    return ParsedTaskIntent(
        schema_version=TASK_INTENT_SCHEMA_VERSION,
        intent_family=_intent_family(steps),
        target_apps=_target_apps(initial_app, target_apps, text),
        entities=entities,
        workflow_steps=steps,
        selection_policy=policy,
        forbidden_states=forbidden,
    )


def parsed_task_intent_from_payload(value: Any) -> ParsedTaskIntent:
    if not isinstance(value, Mapping):
        raise ValueError("parsed task intent must be an object")
    if value.get("schema_version") != TASK_INTENT_SCHEMA_VERSION:
        raise ValueError("unsupported parsed task intent schema")
    target_apps = value.get("target_apps")
    steps = value.get("workflow_steps")
    forbidden = value.get("forbidden_states")
    if not isinstance(target_apps, list) or not isinstance(steps, list) or not isinstance(forbidden, list):
        raise ValueError("parsed task intent list fields are invalid")
    entities = value.get("entities")
    policy = value.get("selection_policy")
    if not isinstance(entities, Mapping) or not isinstance(policy, Mapping):
        raise ValueError("parsed task intent mapping fields are invalid")
    return ParsedTaskIntent(
        schema_version=str(value["schema_version"]),
        intent_family=str(value["intent_family"]),
        target_apps=tuple(str(item) for item in target_apps),
        entities=dict(entities),
        workflow_steps=tuple(str(item) for item in steps),
        selection_policy=dict(policy),
        forbidden_states=tuple(str(item) for item in forbidden),
    )


__all__ = [
    "ParsedTaskIntent",
    "TASK_INTENT_SCHEMA_VERSION",
    "parse_task_intent",
    "parsed_task_intent_from_payload",
]
