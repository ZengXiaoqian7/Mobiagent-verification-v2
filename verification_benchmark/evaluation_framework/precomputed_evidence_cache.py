"""Strict, immutable JSONL replay contract for pre-computed OCR/LLM evidence."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Optional, Tuple, Union

from .event_log import contract_sha256
from .models import ContractIR, ContractRoiIR, RoiCoordinateSpace


PRECOMPUTED_EVIDENCE_CACHE_SCHEMA_VERSION = "harmony-eval-precomputed-evidence-cache-v1"
RECORDED_PROVIDER_PLAN_SCHEMA_VERSION = "harmony-eval-recorded-provider-plan-v1"
RECORDED_PROVIDER_ACQUISITION_VERSION = "harmony-eval-recorded-provider-acquisition-v1"
RECORDED_PROVIDER_ID = "local-precomputed-evidence-cache-v1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class RecordedProviderKind(str, Enum):
    OCR = "OCR"
    LLM = "LLM"


class RecordedLlmDecision(str, Enum):
    TRUE = "TRUE"
    FALSE = "FALSE"
    UNKNOWN = "UNKNOWN"


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


def _canonical_id(value: str, *, context: str) -> None:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{context} must be a canonical non-empty string")


def _sha256(value: str, *, context: str) -> None:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError(f"{context} must be a lowercase SHA-256")


def _exact_keys(value: Mapping[str, Any], expected: set[str], *, context: str) -> None:
    actual = set(value)
    if actual != expected:
        raise ValueError(
            f"{context} fields must be exactly {sorted(expected)}; got {sorted(actual)}"
        )


@dataclass(frozen=True)
class EvidenceCacheKey:
    """The mandatory VCR composite key. No field is optional or inferred."""

    screenshot_sha256: str
    model_version: str
    request_sha256: str

    def validate(self) -> None:
        _sha256(self.screenshot_sha256, context="cache key screenshot_sha256")
        _canonical_id(self.model_version, context="cache key model_version")
        _sha256(self.request_sha256, context="cache key request_sha256")

    def payload(self) -> dict[str, str]:
        self.validate()
        return {
            "screenshot_sha256": self.screenshot_sha256,
            "model_version": self.model_version,
            "request_sha256": self.request_sha256,
        }


@dataclass(frozen=True)
class RecordedOcrOutput:
    text: str
    response_sha256: str

    def validate(self) -> None:
        if not isinstance(self.text, str):
            raise ValueError("recorded OCR text must be a string")
        _sha256(self.response_sha256, context="recorded OCR response_sha256")

    def payload(self) -> dict[str, str]:
        self.validate()
        return {"text": self.text, "response_sha256": self.response_sha256}


@dataclass(frozen=True)
class RecordedLlmOutput:
    decision: RecordedLlmDecision
    response_sha256: str

    def validate(self) -> None:
        if not isinstance(self.decision, RecordedLlmDecision):
            raise ValueError("recorded LLM decision is invalid")
        _sha256(self.response_sha256, context="recorded LLM response_sha256")

    def payload(self) -> dict[str, str]:
        self.validate()
        return {
            "decision": self.decision.value,
            "response_sha256": self.response_sha256,
        }


RecordedOutput = Union[RecordedOcrOutput, RecordedLlmOutput]


@dataclass(frozen=True)
class RecordedEvidenceEntry:
    provider_kind: RecordedProviderKind
    key: EvidenceCacheKey
    output: RecordedOutput
    schema_version: str = PRECOMPUTED_EVIDENCE_CACHE_SCHEMA_VERSION

    def validate(self) -> None:
        if self.schema_version != PRECOMPUTED_EVIDENCE_CACHE_SCHEMA_VERSION:
            raise ValueError("unsupported pre-computed evidence cache schema")
        if not isinstance(self.provider_kind, RecordedProviderKind):
            raise ValueError("recorded provider_kind is invalid")
        if not isinstance(self.key, EvidenceCacheKey):
            raise ValueError("recorded cache key is invalid")
        self.key.validate()
        expected = (
            RecordedOcrOutput
            if self.provider_kind is RecordedProviderKind.OCR
            else RecordedLlmOutput
        )
        if not isinstance(self.output, expected):
            raise ValueError("recorded output schema does not match provider_kind")
        self.output.validate()

    def payload(self) -> dict[str, Any]:
        self.validate()
        return {
            "schema_version": self.schema_version,
            "provider_kind": self.provider_kind.value,
            "key": self.key.payload(),
            "output": self.output.payload(),
        }


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _parse_entry(value: Any) -> RecordedEvidenceEntry:
    if not isinstance(value, Mapping):
        raise ValueError("cache line must be a JSON object")
    _exact_keys(
        value,
        {"schema_version", "provider_kind", "key", "output"},
        context="cache entry",
    )
    if value["schema_version"] != PRECOMPUTED_EVIDENCE_CACHE_SCHEMA_VERSION:
        raise ValueError("unsupported pre-computed evidence cache schema")
    try:
        kind = RecordedProviderKind(value["provider_kind"])
    except (TypeError, ValueError) as exc:
        raise ValueError("cache provider_kind must be OCR or LLM") from exc
    raw_key = value["key"]
    if not isinstance(raw_key, Mapping):
        raise ValueError("cache key must be a JSON object")
    _exact_keys(
        raw_key,
        {"screenshot_sha256", "model_version", "request_sha256"},
        context="cache key",
    )
    key = EvidenceCacheKey(
        screenshot_sha256=raw_key["screenshot_sha256"],
        model_version=raw_key["model_version"],
        request_sha256=raw_key["request_sha256"],
    )
    raw_output = value["output"]
    if not isinstance(raw_output, Mapping):
        raise ValueError("cache output must be a JSON object")
    if kind is RecordedProviderKind.OCR:
        _exact_keys(raw_output, {"text", "response_sha256"}, context="OCR output")
        output: RecordedOutput = RecordedOcrOutput(
            text=raw_output["text"], response_sha256=raw_output["response_sha256"]
        )
    else:
        _exact_keys(
            raw_output, {"decision", "response_sha256"}, context="LLM output"
        )
        try:
            decision = RecordedLlmDecision(raw_output["decision"])
        except (TypeError, ValueError) as exc:
            raise ValueError("LLM decision must be TRUE, FALSE, or UNKNOWN") from exc
        output = RecordedLlmOutput(
            decision=decision, response_sha256=raw_output["response_sha256"]
        )
    entry = RecordedEvidenceEntry(kind, key, output)
    entry.validate()
    return entry


class PrecomputedEvidenceStorage:
    """An eagerly loaded, immutable local dictionary; lookup performs no I/O."""

    __slots__ = ("_entries", "_storage_sha256")

    def __init__(self, entries: Tuple[RecordedEvidenceEntry, ...]) -> None:
        if not isinstance(entries, tuple):
            raise ValueError("cache entries must be an immutable tuple")
        index: dict[tuple[RecordedProviderKind, EvidenceCacheKey], RecordedEvidenceEntry] = {}
        for entry in entries:
            if not isinstance(entry, RecordedEvidenceEntry):
                raise ValueError("cache contains an invalid entry")
            entry.validate()
            identity = (entry.provider_kind, entry.key)
            if identity in index:
                raise ValueError("duplicate composite cache key is forbidden")
            index[identity] = entry
        payloads = sorted(
            (entry.payload() for entry in entries),
            key=lambda item: (
                item["provider_kind"],
                item["key"]["screenshot_sha256"],
                item["key"]["model_version"],
                item["key"]["request_sha256"],
            ),
        )
        self._entries = MappingProxyType(index)
        self._storage_sha256 = _digest(
            {
                "schema_version": PRECOMPUTED_EVIDENCE_CACHE_SCHEMA_VERSION,
                "entries": payloads,
            }
        )

    @classmethod
    def from_jsonl(cls, path: Path | str) -> "PrecomputedEvidenceStorage":
        source = Path(path)
        try:
            lines = source.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as exc:
            raise ValueError("pre-computed evidence cache is unreadable") from exc
        entries = []
        for line_number, line in enumerate(lines, 1):
            if not line.strip():
                raise ValueError(f"blank JSONL line is forbidden at line {line_number}")
            try:
                value = json.loads(line, object_pairs_hook=_reject_duplicate_json_keys)
                entries.append(_parse_entry(value))
            except (json.JSONDecodeError, ValueError) as exc:
                raise ValueError(f"invalid cache entry at line {line_number}: {exc}") from exc
        return cls(tuple(entries))

    @property
    def storage_sha256(self) -> str:
        return self._storage_sha256

    @property
    def entry_count(self) -> int:
        return len(self._entries)

    def lookup(
        self, provider_kind: RecordedProviderKind, key: EvidenceCacheKey
    ) -> Optional[RecordedEvidenceEntry]:
        if not isinstance(provider_kind, RecordedProviderKind):
            raise ValueError("lookup provider_kind is invalid")
        if not isinstance(key, EvidenceCacheKey):
            raise ValueError("lookup key is invalid")
        key.validate()
        return self._entries.get((provider_kind, key))


@dataclass(frozen=True)
class RecordedOcrRequestIR:
    roi: ContractRoiIR = ContractRoiIR(
        roi_id="full-screen",
        bounds=(0.0, 0.0, 1.0, 1.0),
        coordinate_space=RoiCoordinateSpace.NORMALIZED,
    )

    def validate(self) -> None:
        if not isinstance(self.roi, ContractRoiIR):
            raise ValueError("recorded OCR request requires a ContractRoiIR")
        self.roi.validate()

    @property
    def request_sha256(self) -> str:
        self.validate()
        return _digest(
            {
                "request_kind": "OCR_ROI",
                "coordinate_space": self.roi.coordinate_space.value,
                "bounds": list(self.roi.bounds),
                "reference_size": None
                if self.roi.reference_size is None
                else list(self.roi.reference_size),
            }
        )


@dataclass(frozen=True)
class RecordedLlmRequestIR:
    prompt: str
    prompt_template_version: str

    def validate(self) -> None:
        if not isinstance(self.prompt, str) or not self.prompt.strip():
            raise ValueError("recorded LLM prompt must be non-empty")
        _canonical_id(
            self.prompt_template_version, context="LLM prompt_template_version"
        )

    @property
    def request_sha256(self) -> str:
        self.validate()
        return _digest(
            {
                "request_kind": "LLM_PROMPT",
                "prompt_template_version": self.prompt_template_version,
                "prompt": self.prompt,
            }
        )


RecordedRequest = Union[RecordedOcrRequestIR, RecordedLlmRequestIR]


@dataclass(frozen=True)
class RecordedProviderBindingIR:
    node_id: str
    checker_id: str
    provider_kind: RecordedProviderKind
    model_version: str
    request: RecordedRequest

    def validate(self) -> None:
        _canonical_id(self.node_id, context="provider binding node_id")
        _canonical_id(self.checker_id, context="provider binding checker_id")
        _canonical_id(self.model_version, context="provider binding model_version")
        if self.provider_kind is RecordedProviderKind.OCR:
            if self.checker_id != "ocr" or not isinstance(
                self.request, RecordedOcrRequestIR
            ):
                raise ValueError("OCR binding must target an ocr checker and OCR request")
        elif self.provider_kind is RecordedProviderKind.LLM:
            if self.checker_id != "llm" or not isinstance(
                self.request, RecordedLlmRequestIR
            ):
                raise ValueError("LLM binding must target an llm checker and LLM request")
        else:
            raise ValueError("provider binding kind is invalid")
        self.request.validate()

    @property
    def request_sha256(self) -> str:
        self.validate()
        return self.request.request_sha256

    def payload(self) -> dict[str, Any]:
        self.validate()
        request_payload: dict[str, Any]
        if isinstance(self.request, RecordedOcrRequestIR):
            request_payload = {
                "kind": "OCR_ROI",
                "coordinate_space": self.request.roi.coordinate_space.value,
                "bounds": list(self.request.roi.bounds),
                "reference_size": None
                if self.request.roi.reference_size is None
                else list(self.request.roi.reference_size),
            }
        else:
            request_payload = {
                "kind": "LLM_PROMPT",
                "prompt_template_version": self.request.prompt_template_version,
                "prompt": self.request.prompt,
            }
        return {
            "node_id": self.node_id,
            "checker_id": self.checker_id,
            "provider_kind": self.provider_kind.value,
            "model_version": self.model_version,
            "request_sha256": self.request_sha256,
            "request": request_payload,
        }


@dataclass(frozen=True)
class RecordedProviderPlan:
    contract_sha256: str
    bindings: Tuple[RecordedProviderBindingIR, ...]
    schema_version: str = RECORDED_PROVIDER_PLAN_SCHEMA_VERSION

    def validate(self) -> None:
        if self.schema_version != RECORDED_PROVIDER_PLAN_SCHEMA_VERSION:
            raise ValueError("unsupported recorded provider plan schema")
        _sha256(self.contract_sha256, context="provider plan contract_sha256")
        if not isinstance(self.bindings, tuple):
            raise ValueError("provider plan bindings must be an immutable tuple")
        keys = []
        for binding in self.bindings:
            if not isinstance(binding, RecordedProviderBindingIR):
                raise ValueError("provider plan contains an invalid binding")
            binding.validate()
            keys.append((binding.node_id, binding.checker_id))
        if len(keys) != len(set(keys)):
            raise ValueError("provider plan binding keys must be unique")

    def validate_against(self, contract: ContractIR) -> None:
        self.validate()
        contract.validate()
        if self.contract_sha256 != contract_sha256(contract):
            raise ValueError("recorded provider plan is bound to a different ContractIR")
        if contract.dag is None:
            raise ValueError("recorded provider plan requires a contract DAG")
        actual = {
            (node.node_id, checker.checker_id): checker
            for node in contract.dag.nodes
            for checker in node.checkers
            if checker.checker_id in {"ocr", "llm"}
        }
        for binding in self.bindings:
            checker = actual.get((binding.node_id, binding.checker_id))
            if checker is None:
                raise ValueError("provider plan binding does not target an OCR/LLM checker")
            if binding.checker_id == "llm":
                prompt = checker.parameters.get("prompt")
                if not isinstance(binding.request, RecordedLlmRequestIR) or (
                    binding.request.prompt != prompt
                ):
                    raise ValueError("LLM provider binding prompt differs from ContractIR")

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
    ) -> Optional[RecordedProviderBindingIR]:
        self.validate()
        return next(
            (
                binding
                for binding in self.bindings
                if binding.node_id == node_id and binding.checker_id == checker_id
            ),
            None,
        )


@dataclass(frozen=True)
class RecordedProviderContext:
    storage: PrecomputedEvidenceStorage
    plan: RecordedProviderPlan

    def validate_against(self, contract: ContractIR) -> None:
        if type(self.storage) is not PrecomputedEvidenceStorage:
            raise ValueError("recorded provider storage must be the concrete local storage")
        if not isinstance(self.plan, RecordedProviderPlan):
            raise ValueError("recorded provider plan is invalid")
        self.plan.validate_against(contract)


__all__ = [
    "PRECOMPUTED_EVIDENCE_CACHE_SCHEMA_VERSION",
    "RECORDED_PROVIDER_ACQUISITION_VERSION",
    "RECORDED_PROVIDER_ID",
    "RECORDED_PROVIDER_PLAN_SCHEMA_VERSION",
    "EvidenceCacheKey",
    "PrecomputedEvidenceStorage",
    "RecordedEvidenceEntry",
    "RecordedLlmDecision",
    "RecordedLlmOutput",
    "RecordedLlmRequestIR",
    "RecordedOcrOutput",
    "RecordedOcrRequestIR",
    "RecordedProviderBindingIR",
    "RecordedProviderContext",
    "RecordedProviderKind",
    "RecordedProviderPlan",
]
