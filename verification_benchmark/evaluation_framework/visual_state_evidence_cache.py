"""Typed, immutable local cache for deterministic visual-state evidence.

The evaluation path in this module performs dictionary lookup only.  Pixel
decoding and metric generation live behind ``evaluate_visual_state_image`` so
the development-only materializer can run them before a replay begins.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Optional, Tuple

from .event_log import contract_sha256
from .models import ContractIR


VISUAL_STATE_CACHE_SCHEMA_VERSION = "harmony-eval-visual-state-cache-v1"
VISUAL_STATE_PLAN_SCHEMA_VERSION = "harmony-eval-visual-state-provider-plan-v1"
VISUAL_STATE_DETECTOR_VERSION = "enhanced-v2-loading-skeleton-652ec29-v1"
VISUAL_STATE_PROVIDER_ID = "local-typed-visual-state-cache-v1"
VISUAL_STATE_ACQUISITION_VERSION = "harmony-eval-visual-state-acquisition-v1"
COMPOSITE_EVIDENCE_PROVIDER_ID = "local-composite-evidence-cache-v1"
COMPOSITE_EVIDENCE_ACQUISITION_VERSION = (
    "harmony-eval-composite-recorded-acquisition-v1"
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _sha256(value: Any, context: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError(f"{context} must be a lowercase SHA-256")
    return value


def _canonical_id(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{context} must be a canonical non-empty string")
    return value


def _ratio(value: Any, context: str) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        or not 0.0 <= value <= 1.0
    ):
        raise ValueError(f"{context} must be finite within [0,1]")
    return float(value)


def _exact_keys(value: Mapping[str, Any], expected: set[str], context: str) -> None:
    if set(value) != expected:
        raise ValueError(
            f"{context} fields must be exactly {sorted(expected)}; got {sorted(value)}"
        )


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key is forbidden: {key}")
        result[key] = value
    return result


@dataclass(frozen=True)
class VisualStateRequestIR:
    """Effective request, including the exact crop and frozen thresholds."""

    not_loading_skeleton: bool
    crop_top_ratio: float = 0.12
    light_low_sat_threshold: float = 0.94
    max_dark_ratio: float = 0.02
    max_color_ratio: float = 0.01

    def validate(self) -> None:
        if self.not_loading_skeleton is not True:
            raise ValueError(
                "visual-state v1 only supports the not_loading_skeleton=true constraint"
            )
        crop = _ratio(self.crop_top_ratio, "crop_top_ratio")
        if crop > 0.9:
            raise ValueError("crop_top_ratio must not exceed 0.9")
        _ratio(self.light_low_sat_threshold, "light_low_sat_threshold")
        _ratio(self.max_dark_ratio, "max_dark_ratio")
        _ratio(self.max_color_ratio, "max_color_ratio")

    @classmethod
    def from_checker_parameters(
        cls, params: Mapping[str, Any]
    ) -> "VisualStateRequestIR":
        if not isinstance(params, Mapping):
            raise ValueError("visual_state parameters must be an object")
        allowed = {
            "not_loading_skeleton",
            "crop_top_ratio",
            "light_low_sat_threshold",
            "max_dark_ratio",
            "max_color_ratio",
        }
        unexpected = set(params) - allowed
        if unexpected:
            raise ValueError(
                f"visual_state has unknown parameters: {sorted(unexpected)}"
            )
        request = cls(
            not_loading_skeleton=params.get("not_loading_skeleton"),
            crop_top_ratio=params.get("crop_top_ratio", 0.12),
            light_low_sat_threshold=params.get("light_low_sat_threshold", 0.94),
            max_dark_ratio=params.get("max_dark_ratio", 0.02),
            max_color_ratio=params.get("max_color_ratio", 0.01),
        )
        request.validate()
        return request

    def payload(self) -> dict[str, Any]:
        self.validate()
        return {
            "request_kind": "NOT_LOADING_SKELETON",
            "roi": {
                "coordinate_space": "NORMALIZED",
                "bounds": [0.0, float(self.crop_top_ratio), 1.0, 1.0],
            },
            "not_loading_skeleton": self.not_loading_skeleton,
            "crop_top_ratio": float(self.crop_top_ratio),
            "light_low_sat_threshold": float(self.light_low_sat_threshold),
            "max_dark_ratio": float(self.max_dark_ratio),
            "max_color_ratio": float(self.max_color_ratio),
        }

    @property
    def request_sha256(self) -> str:
        return _digest(self.payload())


class VisualStateDecision(str, Enum):
    LOADED_CONTENT = "LOADED_CONTENT"
    LOADING_SKELETON = "LOADING_SKELETON"


@dataclass(frozen=True)
class VisualStateMetrics:
    light_low_sat_ratio: float
    dark_ratio: float
    color_ratio: float

    def validate(self) -> None:
        _ratio(self.light_low_sat_ratio, "light_low_sat_ratio")
        _ratio(self.dark_ratio, "dark_ratio")
        _ratio(self.color_ratio, "color_ratio")

    def payload(self) -> dict[str, float]:
        self.validate()
        return {
            "light_low_sat_ratio": float(self.light_low_sat_ratio),
            "dark_ratio": float(self.dark_ratio),
            "color_ratio": float(self.color_ratio),
        }


@dataclass(frozen=True)
class VisualStateOutput:
    decision: VisualStateDecision
    metrics: VisualStateMetrics
    output_sha256: str

    @classmethod
    def create(
        cls, decision: VisualStateDecision, metrics: VisualStateMetrics
    ) -> "VisualStateOutput":
        metrics.validate()
        if not isinstance(decision, VisualStateDecision):
            raise ValueError("visual-state decision is invalid")
        digest = _digest({"decision": decision.value, "metrics": metrics.payload()})
        return cls(decision, metrics, digest)

    def validate(self) -> None:
        if not isinstance(self.decision, VisualStateDecision):
            raise ValueError("visual-state decision is invalid")
        if not isinstance(self.metrics, VisualStateMetrics):
            raise ValueError("visual-state metrics are invalid")
        self.metrics.validate()
        _sha256(self.output_sha256, "visual-state output_sha256")
        expected = _digest(
            {"decision": self.decision.value, "metrics": self.metrics.payload()}
        )
        if self.output_sha256 != expected:
            raise ValueError("visual-state output hash mismatch")

    def payload(self) -> dict[str, Any]:
        self.validate()
        return {
            "decision": self.decision.value,
            "metrics": self.metrics.payload(),
            "output_sha256": self.output_sha256,
        }


@dataclass(frozen=True)
class VisualStateCacheKey:
    screenshot_sha256: str
    detector_version: str
    request_sha256: str

    def validate(self) -> None:
        _sha256(self.screenshot_sha256, "visual-state screenshot_sha256")
        _canonical_id(self.detector_version, "visual-state detector_version")
        _sha256(self.request_sha256, "visual-state request_sha256")

    def payload(self) -> dict[str, str]:
        self.validate()
        return {
            "screenshot_sha256": self.screenshot_sha256,
            "detector_version": self.detector_version,
            "request_sha256": self.request_sha256,
        }


@dataclass(frozen=True)
class VisualStateCacheEntry:
    key: VisualStateCacheKey
    output: VisualStateOutput
    schema_version: str = VISUAL_STATE_CACHE_SCHEMA_VERSION

    def validate(self) -> None:
        if self.schema_version != VISUAL_STATE_CACHE_SCHEMA_VERSION:
            raise ValueError("unsupported visual-state cache schema")
        if not isinstance(self.key, VisualStateCacheKey):
            raise ValueError("visual-state cache key is invalid")
        if not isinstance(self.output, VisualStateOutput):
            raise ValueError("visual-state cache output is invalid")
        self.key.validate()
        self.output.validate()

    def payload(self) -> dict[str, Any]:
        self.validate()
        return {
            "schema_version": self.schema_version,
            "key": self.key.payload(),
            "output": self.output.payload(),
        }


def _parse_entry(value: Any) -> VisualStateCacheEntry:
    if not isinstance(value, Mapping):
        raise ValueError("visual-state cache line must be an object")
    _exact_keys(value, {"schema_version", "key", "output"}, "cache entry")
    if value["schema_version"] != VISUAL_STATE_CACHE_SCHEMA_VERSION:
        raise ValueError("unsupported visual-state cache schema")
    raw_key = value["key"]
    raw_output = value["output"]
    if not isinstance(raw_key, Mapping) or not isinstance(raw_output, Mapping):
        raise ValueError("visual-state key and output must be objects")
    _exact_keys(
        raw_key,
        {"screenshot_sha256", "detector_version", "request_sha256"},
        "cache key",
    )
    _exact_keys(raw_output, {"decision", "metrics", "output_sha256"}, "cache output")
    metrics = raw_output["metrics"]
    if not isinstance(metrics, Mapping):
        raise ValueError("visual-state metrics must be an object")
    _exact_keys(
        metrics,
        {"light_low_sat_ratio", "dark_ratio", "color_ratio"},
        "cache metrics",
    )
    try:
        decision = VisualStateDecision(raw_output["decision"])
    except (TypeError, ValueError) as exc:
        raise ValueError("visual-state decision is invalid") from exc
    entry = VisualStateCacheEntry(
        VisualStateCacheKey(
            raw_key["screenshot_sha256"],
            raw_key["detector_version"],
            raw_key["request_sha256"],
        ),
        VisualStateOutput(
            decision,
            VisualStateMetrics(
                metrics["light_low_sat_ratio"],
                metrics["dark_ratio"],
                metrics["color_ratio"],
            ),
            raw_output["output_sha256"],
        ),
    )
    entry.validate()
    return entry


class VisualStateEvidenceStorage:
    """Eagerly loaded immutable storage; lookup performs no filesystem I/O."""

    __slots__ = ("_entries", "_storage_sha256")

    def __init__(self, entries: Tuple[VisualStateCacheEntry, ...]) -> None:
        if not isinstance(entries, tuple):
            raise ValueError("visual-state entries must be an immutable tuple")
        index: dict[VisualStateCacheKey, VisualStateCacheEntry] = {}
        for entry in entries:
            if not isinstance(entry, VisualStateCacheEntry):
                raise ValueError("visual-state cache contains an invalid entry")
            entry.validate()
            if entry.key in index:
                raise ValueError("duplicate visual-state composite key is forbidden")
            index[entry.key] = entry
        payloads = sorted(
            (entry.payload() for entry in entries),
            key=lambda item: (
                item["key"]["screenshot_sha256"],
                item["key"]["detector_version"],
                item["key"]["request_sha256"],
            ),
        )
        self._entries = MappingProxyType(index)
        self._storage_sha256 = _digest(
            {
                "schema_version": VISUAL_STATE_CACHE_SCHEMA_VERSION,
                "entries": payloads,
            }
        )

    @classmethod
    def from_jsonl(cls, path: Path | str) -> "VisualStateEvidenceStorage":
        try:
            lines = Path(path).read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as exc:
            raise ValueError("visual-state cache is unreadable") from exc
        entries = []
        for line_number, line in enumerate(lines, 1):
            if not line.strip():
                raise ValueError(
                    f"blank visual-state JSONL line is forbidden at line {line_number}"
                )
            try:
                raw = json.loads(line, object_pairs_hook=_reject_duplicate_json_keys)
                entries.append(_parse_entry(raw))
            except (json.JSONDecodeError, ValueError) as exc:
                raise ValueError(
                    f"invalid visual-state cache entry at line {line_number}: {exc}"
                ) from exc
        return cls(tuple(entries))

    @property
    def storage_sha256(self) -> str:
        return self._storage_sha256

    @property
    def entry_count(self) -> int:
        return len(self._entries)

    @property
    def entries(self) -> Tuple[VisualStateCacheEntry, ...]:
        return tuple(self._entries.values())

    def lookup(self, key: VisualStateCacheKey) -> Optional[VisualStateCacheEntry]:
        if not isinstance(key, VisualStateCacheKey):
            raise ValueError("visual-state lookup key is invalid")
        key.validate()
        return self._entries.get(key)


@dataclass(frozen=True)
class VisualStateProviderBindingIR:
    node_id: str
    checker_id: str
    detector_version: str
    request: VisualStateRequestIR

    def validate(self) -> None:
        _canonical_id(self.node_id, "visual-state binding node_id")
        if self.checker_id != "visual_state":
            raise ValueError("visual-state binding must target visual_state")
        _canonical_id(self.detector_version, "visual-state detector_version")
        if not isinstance(self.request, VisualStateRequestIR):
            raise ValueError("visual-state binding request is invalid")
        self.request.validate()

    @property
    def request_sha256(self) -> str:
        self.validate()
        return self.request.request_sha256

    def payload(self) -> dict[str, Any]:
        self.validate()
        return {
            "node_id": self.node_id,
            "checker_id": self.checker_id,
            "detector_version": self.detector_version,
            "request_sha256": self.request_sha256,
            "request": self.request.payload(),
        }


@dataclass(frozen=True)
class VisualStateProviderPlan:
    contract_sha256: str
    bindings: Tuple[VisualStateProviderBindingIR, ...]
    schema_version: str = VISUAL_STATE_PLAN_SCHEMA_VERSION

    def validate(self) -> None:
        if self.schema_version != VISUAL_STATE_PLAN_SCHEMA_VERSION:
            raise ValueError("unsupported visual-state provider plan schema")
        _sha256(self.contract_sha256, "visual-state plan contract_sha256")
        if not isinstance(self.bindings, tuple) or not self.bindings:
            raise ValueError("visual-state plan bindings must be a non-empty tuple")
        keys = []
        for binding in self.bindings:
            if not isinstance(binding, VisualStateProviderBindingIR):
                raise ValueError("visual-state plan contains an invalid binding")
            binding.validate()
            keys.append((binding.node_id, binding.checker_id))
        if len(keys) != len(set(keys)):
            raise ValueError("visual-state plan binding keys must be unique")

    def validate_against(self, contract: ContractIR) -> None:
        self.validate()
        contract.validate()
        if self.contract_sha256 != contract_sha256(contract):
            raise ValueError("visual-state plan is bound to a different ContractIR")
        if contract.dag is None:
            raise ValueError("visual-state plan requires a contract DAG")
        actual = {
            (node.node_id, checker.checker_id): checker
            for node in contract.dag.nodes
            for checker in node.checkers
            if checker.checker_id == "visual_state"
        }
        if set(actual) != {
            (binding.node_id, binding.checker_id) for binding in self.bindings
        }:
            raise ValueError("visual-state plan must cover every visual_state checker")
        for binding in self.bindings:
            expected = VisualStateRequestIR.from_checker_parameters(
                actual[(binding.node_id, binding.checker_id)].parameters
            )
            if binding.request != expected:
                raise ValueError("visual-state plan request differs from ContractIR")

    @property
    def plan_sha256(self) -> str:
        self.validate()
        return _digest(
            {
                "schema_version": self.schema_version,
                "contract_sha256": self.contract_sha256,
                "bindings": sorted(
                    (binding.payload() for binding in self.bindings),
                    key=lambda item: (item["node_id"], item["checker_id"]),
                ),
            }
        )

    def binding_for(
        self, node_id: str, checker_id: str
    ) -> Optional[VisualStateProviderBindingIR]:
        self.validate()
        return next(
            (
                binding
                for binding in self.bindings
                if binding.node_id == node_id and binding.checker_id == checker_id
            ),
            None,
        )


def visual_state_provider_plan(contract: ContractIR) -> VisualStateProviderPlan:
    contract.validate()
    if contract.dag is None:
        raise ValueError("visual-state plan requires a contract DAG")
    bindings = tuple(
        VisualStateProviderBindingIR(
            node.node_id,
            checker.checker_id,
            VISUAL_STATE_DETECTOR_VERSION,
            VisualStateRequestIR.from_checker_parameters(checker.parameters),
        )
        for node in contract.dag.nodes
        for checker in node.checkers
        if checker.checker_id == "visual_state"
    )
    plan = VisualStateProviderPlan(contract_sha256(contract), bindings)
    plan.validate_against(contract)
    return plan


@dataclass(frozen=True)
class VisualStateProviderContext:
    storage: VisualStateEvidenceStorage
    plan: VisualStateProviderPlan

    def validate_against(self, contract: ContractIR) -> None:
        if type(self.storage) is not VisualStateEvidenceStorage:
            raise ValueError("visual-state storage must be concrete local storage")
        if not isinstance(self.plan, VisualStateProviderPlan):
            raise ValueError("visual-state provider plan is invalid")
        self.plan.validate_against(contract)


def composite_evidence_sha256(
    recorded_sha256: str, visual_state_sha256: str, *, identity_kind: str
) -> str:
    _sha256(recorded_sha256, "recorded evidence identity")
    _sha256(visual_state_sha256, "visual-state evidence identity")
    if identity_kind not in {"PROVIDER_CONFIGURATION", "EVIDENCE_STORAGE"}:
        raise ValueError("composite evidence identity kind is invalid")
    return _digest(
        {
            "identity_kind": identity_kind,
            "recorded_ocr_llm_sha256": recorded_sha256,
            "visual_state_sha256": visual_state_sha256,
        }
    )


def evaluate_visual_state_image(
    image_path: Path | str, request: VisualStateRequestIR
) -> VisualStateOutput:
    """Development-only deterministic materializer for the frozen v2 formula."""

    request.validate()
    try:
        import numpy as np
        from PIL import Image

        image = np.asarray(Image.open(image_path).convert("RGB"))
    except Exception as exc:
        raise ValueError("visual-state source image is unreadable") from exc
    if image.ndim == 2:
        rgb = np.stack([image, image, image], axis=2)
    else:
        rgb = image[:, :, :3]
    height = rgb.shape[0]
    top = int(height * max(0.0, min(0.9, float(request.crop_top_ratio))))
    crop = rgb[top:] if top < height else rgb
    max_channel = crop.max(axis=2).astype(float)
    min_channel = crop.min(axis=2).astype(float)
    saturation = max_channel - min_channel
    gray = crop.mean(axis=2)
    metrics = VisualStateMetrics(
        float(((gray > 185) & (saturation < 35)).mean()),
        float((gray < 120).mean()),
        float((saturation > 50).mean()),
    )
    skeleton = (
        metrics.light_low_sat_ratio >= float(request.light_low_sat_threshold)
        and metrics.dark_ratio <= float(request.max_dark_ratio)
        and metrics.color_ratio <= float(request.max_color_ratio)
    )
    decision = (
        VisualStateDecision.LOADING_SKELETON
        if skeleton
        else VisualStateDecision.LOADED_CONTENT
    )
    return VisualStateOutput.create(decision, metrics)


def visual_state_cache_jsonl_bytes(storage: VisualStateEvidenceStorage) -> bytes:
    rows = sorted(
        (entry.payload() for entry in storage.entries),
        key=lambda item: (
            item["key"]["screenshot_sha256"],
            item["key"]["detector_version"],
            item["key"]["request_sha256"],
        ),
    )
    return b"".join(_canonical_bytes(row) + b"\n" for row in rows)


__all__ = [
    "COMPOSITE_EVIDENCE_ACQUISITION_VERSION",
    "COMPOSITE_EVIDENCE_PROVIDER_ID",
    "VISUAL_STATE_ACQUISITION_VERSION",
    "VISUAL_STATE_CACHE_SCHEMA_VERSION",
    "VISUAL_STATE_DETECTOR_VERSION",
    "VISUAL_STATE_PLAN_SCHEMA_VERSION",
    "VISUAL_STATE_PROVIDER_ID",
    "VisualStateCacheEntry",
    "VisualStateCacheKey",
    "VisualStateDecision",
    "VisualStateEvidenceStorage",
    "VisualStateMetrics",
    "VisualStateOutput",
    "VisualStateProviderBindingIR",
    "VisualStateProviderContext",
    "VisualStateProviderPlan",
    "VisualStateRequestIR",
    "composite_evidence_sha256",
    "evaluate_visual_state_image",
    "visual_state_cache_jsonl_bytes",
    "visual_state_provider_plan",
]
