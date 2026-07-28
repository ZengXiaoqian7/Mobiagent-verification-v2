"""Task-only identity used to compile a Contract before trace evidence is read."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Tuple

from .task_intent import parse_task_intent, parsed_task_intent_from_payload


TASK_SPEC_SCHEMA_VERSION = "mobiagent-verifier-task-spec-v1"
SUPPORTED_TASK_FAMILIES = (
    "search_results",
    "open_detail",
    "select_control",
    "creator_homepage",
    "playback",
    "location_search",
    "route_preview",
    "cross_app_entity_transfer",
    "composite_workflow",
)
_TASK_FAMILY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def infer_task_family(task_text: str, declared_family: str = "") -> str:
    """Conservative task-only family classification; it never sees a trace."""

    declared = declared_family.strip()
    if declared in SUPPORTED_TASK_FAMILIES:
        return declared
    text = task_text.strip()
    parsed = parse_task_intent(text)
    if parsed.intent_family in SUPPORTED_TASK_FAMILIES:
        return parsed.intent_family
    if any(term in text for term in ("随后", "跨应用", "跨 App", "open_app")):
        return "cross_app_entity_transfer"
    if any(term in text for term in ("路线预览", "起点和终点", "查看从")):
        return "route_preview"
    if "主页" in text and any(term in text for term in ("博主", "UP主", "个人")):
        return "creator_homepage"
    if any(term in text for term in ("播放", "正在播放", "播放界面")):
        return "playback"
    if any(term in text for term in ("详情页", "商品详情", "打开一个")):
        return "open_detail"
    if any(term in text for term in ("排序", "筛选", "销量", "价格最低", "价格最高")):
        return "select_control"
    if any(term in text for term in ("高德地图", "地点详情", "不要开始导航")):
        return "location_search"
    if any(term in text for term in ("搜索", "查找", "检索")):
        return "search_results"
    return "unseen"


@dataclass(frozen=True)
class TaskSpec:
    task_id: str
    task_text: str
    task_family: str
    initial_app: str = ""
    target_apps: Tuple[str, ...] = ()
    risk_level: str = "read_only"
    parsed_intent: Mapping[str, Any] = field(default_factory=dict)
    schema_version: str = TASK_SPEC_SCHEMA_VERSION

    def validate(self) -> None:
        if self.schema_version != TASK_SPEC_SCHEMA_VERSION:
            raise ValueError("unsupported TaskSpec schema")
        for name, value in (("task_id", self.task_id), ("task_text", self.task_text)):
            if not isinstance(value, str) or not value.strip() or value != value.strip():
                raise ValueError(f"TaskSpec {name} must be canonical and non-empty")
        if (
            not isinstance(self.task_family, str)
            or not _TASK_FAMILY.fullmatch(self.task_family)
        ):
            raise ValueError(f"invalid task family: {self.task_family!r}")
        if self.risk_level not in {"read_only", "low_risk_write", "high_risk"}:
            raise ValueError("unsupported task risk level")
        if not isinstance(self.target_apps, tuple) or any(
            not isinstance(item, str) or not item.strip() for item in self.target_apps
        ):
            raise ValueError("TaskSpec target_apps must be canonical strings")
        if not isinstance(self.parsed_intent, Mapping):
            raise ValueError("TaskSpec parsed_intent must be a JSON object")
        try:
            json.dumps(
                self.parsed_intent,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("TaskSpec parsed_intent must be finite JSON data") from exc

    def payload(self) -> Mapping[str, Any]:
        self.validate()
        return {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "task_text": self.task_text,
            "task_family": self.task_family,
            "initial_app": self.initial_app,
            "target_apps": list(self.target_apps),
            "risk_level": self.risk_level,
            "parsed_intent": dict(self.parsed_intent),
        }

    @property
    def sha256(self) -> str:
        rendered = json.dumps(
            self.payload(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(rendered).hexdigest()

    @property
    def selection_key(self) -> str:
        return f"task-spec:{self.sha256}"

    @classmethod
    def from_run_manifest(cls, run: Mapping[str, Any]) -> "TaskSpec":
        text = str(run.get("task_text") or "").strip()
        task_id = str(run.get("task_id") or "").strip()
        if not text:
            experiment = str(run.get("experiment_id") or "")
            if experiment.startswith("phase5-cross-app-realism"):
                # Compatibility for early frozen Phase 5 fixtures whose task
                # identity lived in the acquisition manifest rather than the
                # run manifest. No trace or agent output is consulted.
                text = "在淘宝搜索商品并点击销量排序，随后打开小红书搜索来源商品"
            else:
                raise ValueError("run manifest does not contain task_text")
        declared = str(run.get("task_family") or "")
        declared_apps = run.get("target_apps")
        app_values = (
            declared_apps
            if isinstance(declared_apps, list)
            else [run.get("initial_app"), run.get("target_app")]
        )
        apps = tuple(
            dict.fromkeys(
                str(value or "").strip()
                for value in app_values
                if str(value or "").strip()
            )
        )
        parsed_payload = run.get("parsed_intent")
        if isinstance(parsed_payload, Mapping):
            parsed = parsed_task_intent_from_payload(parsed_payload)
        else:
            parsed = parse_task_intent(
                text,
                initial_app=str(run.get("initial_app") or "").strip(),
                target_apps=apps,
            )
        family = infer_task_family(text, declared)
        risk = str(run.get("risk_level") or "").strip() or "read_only"
        return cls(
            task_id=task_id,
            task_text=text,
            task_family=family,
            initial_app=str(run.get("initial_app") or "").strip(),
            target_apps=apps or parsed.target_apps,
            risk_level=risk,
            parsed_intent=parsed.payload(),
        )


__all__ = [
    "SUPPORTED_TASK_FAMILIES",
    "TASK_SPEC_SCHEMA_VERSION",
    "TaskSpec",
    "infer_task_family",
]
