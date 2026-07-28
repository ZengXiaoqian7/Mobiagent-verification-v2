"""Trace-blind Structured-Output JIT compiler for validated ContractIR values."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping, Optional, Protocol, Sequence, Tuple

from . import contract_validation as _validation
from .event_log import contract_sha256
from .models import (
    ContractCheckerIR,
    ContractDagEdgeIR,
    ContractDagIR,
    ContractDagNodeIR,
    ContractDagSuccessIR,
    ContractIR,
    ContractProvenanceIR,
    ContractRoiIR,
    ContractSourceType,
    CriterionIR,
    DagEdgeKind,
    DagLogicalOperator,
    EvidenceCapability,
    G1CheckerKind,
    G1CriterionBindingIR,
    RoiCoordinateSpace,
    TemporalSemantics,
)


JIT_COMPILER_VERSION = "harmony-eval-validated-jit-compiler-v1"
JIT_CONTRACT_SOURCE = "validated-jit"
JIT_PROPOSAL_SCHEMA_VERSION = "harmony-eval-jit-contract-proposal-v1"
JIT_STRUCTURED_OUTPUT_NAME = "harmony_eval_jit_contract_proposal_v1"
MAX_TASK_DESCRIPTION_CHARS = 2000
MAX_JUSTIFICATION_CHARS = 300
MAX_PROPOSAL_BYTES = 128 * 1024
MAX_CRITERIA = 32
MAX_BINDINGS = 32
MAX_DAG_NODES = 32
MAX_DAG_EDGES = 128
MAX_JIT_COMPILER_FEEDBACK_ATTEMPTS = 2
_CANONICAL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_JIT_CAPABILITIES = (
    "ACTIONS",
    "HIERARCHY_RAW_JSON",
    "HIERARCHY_XML",
    "SCREENSHOT",
    "TIMESTAMPS",
)
_JIT_DAG_CHECKERS = ("llm", "ocr", "regex", "text", "xml")


def _plain_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain_json(child) for key, child in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain_json(child) for child in value]
    return value


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {key: _freeze_json(child) for key, child in value.items()}
        )
    if isinstance(value, (tuple, list)):
        return tuple(_freeze_json(child) for child in value)
    return value


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        _plain_json(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _object_schema(properties: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": dict(properties),
        "required": list(properties),
        "additionalProperties": False,
    }


def jit_contract_proposal_json_schema() -> dict[str, Any]:
    """Return the single generated schema used by transports and CI."""

    string_id = {"type": "string"}
    string_list = {
        "type": "array",
        "items": {"type": "string"},
    }
    capability_list = {
        "type": "array",
        "items": {"type": "string", "enum": list(_JIT_CAPABILITIES)},
    }
    criterion = _object_schema(
        {
            "criterion_id": string_id,
            "temporal_semantics": {
                "type": "string",
                "enum": [item.value for item in TemporalSemantics],
            },
            "required": {"type": "boolean"},
            "allow_obscured_persistence": {"type": "boolean"},
            "required_capabilities": capability_list,
            "description": {"type": "string"},
        }
    )
    roi = _object_schema(
        {
            "roi_id": string_id,
            "bounds": {
                "type": "array",
                "items": {"type": "number"},
            },
            "coordinate_space": {
                "type": "string",
                "enum": [item.value for item in RoiCoordinateSpace],
            },
            "reference_size": {
                "type": ["array", "null"],
                "items": {"type": "integer"},
            },
        }
    )
    binding = _object_schema(
        {
            "criterion_id": string_id,
            "checker": {
                "type": "string",
                "enum": [item.value for item in G1CheckerKind],
            },
            "rois": {
                "type": "array",
                "items": roi,
            },
        }
    )
    empty_string_list = {
        "type": "array",
        "items": {"type": "string"},
        "maxItems": 0,
    }
    null_value = {"type": "null"}

    def checker_schema(
        checker_id: str, parameters: Mapping[str, Any]
    ) -> dict[str, Any]:
        return _object_schema(
            {
                "checker_id": {"type": "string", "const": checker_id},
                "parameters": _object_schema(parameters),
            }
        )

    checker = {
        "oneOf": [
            checker_schema(
                "text",
                {
                    "any": string_list,
                    "all": string_list,
                    "none": empty_string_list,
                    "pattern": null_value,
                    "ignore_case": null_value,
                    "prompt": null_value,
                    "expected_true": null_value,
                },
            ),
            checker_schema(
                "xml",
                {
                    "any": string_list,
                    "all": string_list,
                    "none": string_list,
                    "pattern": null_value,
                    "ignore_case": null_value,
                    "prompt": null_value,
                    "expected_true": null_value,
                },
            ),
            checker_schema(
                "regex",
                {
                    "any": empty_string_list,
                    "all": empty_string_list,
                    "none": empty_string_list,
                    "pattern": {"type": "string"},
                    "ignore_case": {"type": "boolean"},
                    "prompt": null_value,
                    "expected_true": null_value,
                },
            ),
            checker_schema(
                "ocr",
                {
                    "any": string_list,
                    "all": string_list,
                    "none": empty_string_list,
                    "pattern": {"type": ["string", "null"]},
                    "ignore_case": {"type": "boolean"},
                    "prompt": null_value,
                    "expected_true": null_value,
                },
            ),
            checker_schema(
                "llm",
                {
                    "any": empty_string_list,
                    "all": empty_string_list,
                    "none": empty_string_list,
                    "pattern": null_value,
                    "ignore_case": null_value,
                    "prompt": {"type": "string"},
                    "expected_true": {"type": "boolean"},
                },
            ),
        ]
    }
    node = _object_schema(
        {
            "node_id": string_id,
            "condition_operator": {
                "type": "string",
                "enum": [item.value for item in DagLogicalOperator],
            },
            "score": {"type": "integer"},
            "checkers": {
                "type": "array",
                "items": checker,
            },
        }
    )
    edge = _object_schema(
        {
            "parent_id": string_id,
            "child_id": string_id,
            "kind": {
                "type": "string",
                "enum": [item.value for item in DagEdgeKind],
            },
        }
    )
    success = _object_schema(
        {
            "operator": {
                "type": "string",
                "enum": [item.value for item in DagLogicalOperator],
            },
            "node_ids": {
                "type": "array",
                "items": string_id,
            },
        }
    )
    dag = _object_schema(
        {
            "nodes": {
                "type": "array",
                "items": node,
            },
            "edges": {
                "type": "array",
                "items": edge,
            },
            "success": success,
        }
    )
    return {
        **_object_schema(
            {
                "schema_version": {
                    "type": "string",
                    "const": JIT_PROPOSAL_SCHEMA_VERSION,
                },
                "task_family": {
                    "type": ["string", "null"],
                },
                "justification": {
                    "type": "string",
                    "maxLength": MAX_JUSTIFICATION_CHARS,
                    "pattern": r"^[^.!?。！？\r\n]+[.!?。！？]$",
                    "description": (
                        "Exactly one sentence explaining why the proposed success "
                        "node or success criterion represents completion of the task."
                    ),
                },
                "required_capabilities": capability_list,
                "criteria": {
                    "type": "array",
                    "items": criterion,
                },
                "g1_bindings": {
                    "type": "array",
                    "items": binding,
                },
                "dag": {"anyOf": [dag, {"type": "null"}]},
            }
        ),
    }


@dataclass(frozen=True)
class JitAppMetadata:
    app_id: str
    app_name: str
    platform: str
    app_version: Optional[str] = None
    task_family: Optional[str] = None
    risk_tier: str = "MEDIUM"

    def validate(self) -> None:
        for name, value in (
            ("app_id", self.app_id),
            ("app_name", self.app_name),
            ("platform", self.platform),
        ):
            if not isinstance(value, str) or not value.strip() or value != value.strip():
                raise ValueError(f"JIT app metadata {name} must be canonical")
        for name, value in (
            ("app_version", self.app_version),
            ("task_family", self.task_family),
        ):
            if value is not None and (
                not isinstance(value, str)
                or not value.strip()
                or value != value.strip()
            ):
                raise ValueError(f"JIT app metadata {name} must be null or canonical")
        if self.risk_tier not in {"LOW", "MEDIUM", "HIGH"}:
            raise ValueError("JIT app metadata risk_tier is invalid")

    def payload(self) -> dict[str, Any]:
        self.validate()
        return {
            "app_id": self.app_id,
            "app_name": self.app_name,
            "platform": self.platform,
            "app_version": self.app_version,
            "task_family": self.task_family,
            "risk_tier": self.risk_tier,
        }


@dataclass(frozen=True)
class JitCompileRequest:
    task_description: str
    app_metadata: JitAppMetadata

    def validate(self) -> None:
        if (
            not isinstance(self.task_description, str)
            or not self.task_description.strip()
            or self.task_description != self.task_description.strip()
            or len(self.task_description) > MAX_TASK_DESCRIPTION_CHARS
        ):
            raise ValueError("JIT task_description must be canonical and within its limit")
        if not isinstance(self.app_metadata, JitAppMetadata):
            raise ValueError("JIT app_metadata must be a JitAppMetadata")
        self.app_metadata.validate()

    def proposer_payload(self) -> Mapping[str, Any]:
        self.validate()
        return MappingProxyType(
            {
                "task_description": self.task_description,
                "app_metadata": MappingProxyType(self.app_metadata.payload()),
            }
        )

    @property
    def input_sha256(self) -> str:
        return _digest(dict(self.proposer_payload()))

    @property
    def selection_key(self) -> str:
        return f"jit:{self.input_sha256}"


@dataclass(frozen=True)
class JitStructuredOutputSpec:
    name: str
    schema: Mapping[str, Any]
    schema_sha256: str
    strict: bool = True

    def openai_text_format(self) -> dict[str, Any]:
        return {
            "type": "json_schema",
            "name": self.name,
            "strict": self.strict,
            "schema": _plain_json(self.schema),
        }


def jit_structured_output_spec() -> JitStructuredOutputSpec:
    schema = jit_contract_proposal_json_schema()
    return JitStructuredOutputSpec(
        JIT_STRUCTURED_OUTPUT_NAME,
        _freeze_json(schema),
        _digest(schema),
    )


@dataclass(frozen=True)
class JitProposalResponse:
    json_bytes: Optional[bytes] = None
    refusal: Optional[str] = None

    def validate(self) -> None:
        if (self.json_bytes is None) == (self.refusal is None):
            raise ValueError("JIT response must contain exactly one of JSON or refusal")
        if self.json_bytes is not None and not isinstance(self.json_bytes, bytes):
            raise ValueError("JIT response JSON must be bytes")
        if self.refusal is not None and (
            not isinstance(self.refusal, str) or not self.refusal.strip()
        ):
            raise ValueError("JIT refusal must be a non-empty string")


class JitProposer(Protocol):
    proposer_id: str
    proposer_version: str

    def propose(
        self,
        request: Mapping[str, Any],
        *,
        response_format: JitStructuredOutputSpec,
    ) -> JitProposalResponse:
        ...


def _feedback_message(error: JitCompilationError) -> str:
    message = f"{error.code.value}: {error}"
    if len(message) > 1200:
        return message[:1197] + "..."
    return message


class JitCompilationFailureCode(str, Enum):
    INVALID_INPUT = "INVALID_INPUT"
    INVALID_PROPOSER = "INVALID_PROPOSER"
    PROPOSER_FAILURE = "PROPOSER_FAILURE"
    REFUSED = "REFUSED"
    INVALID_JSON = "INVALID_JSON"
    SCHEMA_VIOLATION = "SCHEMA_VIOLATION"
    VALIDATION_REJECTED = "VALIDATION_REJECTED"


class JitCompilationError(ValueError):
    def __init__(
        self,
        code: JitCompilationFailureCode,
        message: str,
        *,
        validation_code: Optional[_validation.ContractValidationFailureCode] = None,
    ) -> None:
        self.code = code
        self.validation_code = validation_code
        super().__init__(message)


@dataclass(frozen=True)
class CompiledJitContract:
    contract: ContractIR
    contract_sha256: str
    input_sha256: str
    proposal_sha256: str
    justification: str
    structured_output_schema_sha256: str
    proposer_id: str
    proposer_version: str
    validation_funnel_version: str

    def __post_init__(self) -> None:
        if not hmac.compare_digest(contract_sha256(self.contract), self.contract_sha256):
            raise ValueError("compiled JIT hash does not match ContractIR")
        for value in (
            self.input_sha256,
            self.proposal_sha256,
            self.structured_output_schema_sha256,
        ):
            if not isinstance(value, str) or not _SHA256.fullmatch(value):
                raise ValueError("compiled JIT digest is invalid")
        _justification(self.justification)
        if self.validation_funnel_version != (
            _validation.CONTRACT_VALIDATION_FUNNEL_VERSION
        ):
            raise ValueError("compiled JIT did not pass the shared validation funnel")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _mapping(value: Any, context: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{context} must be an object")
    return value


def _keys(value: Mapping[str, Any], expected: set[str], context: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ValueError(
            f"{context} fields differ from the strict schema; "
            f"missing={missing}, extra={extra}"
        )


def _list(value: Any, context: str, *, minimum: int = 0, maximum: int) -> list[Any]:
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise ValueError(f"{context} list size is outside the strict schema")
    return value


def _string(value: Any, context: str, *, maximum: int = 128) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or value != value.strip()
        or len(value) > maximum
    ):
        raise ValueError(f"{context} must be a canonical bounded string")
    return value


def _justification(value: Any) -> str:
    result = _string(
        value,
        "proposal.justification",
        maximum=MAX_JUSTIFICATION_CHARS,
    )
    sentence_terminators = ".!?。！？"
    if "\n" in result or "\r" in result:
        raise ValueError("proposal.justification must be exactly one line")
    if result[-1] not in sentence_terminators or sum(
        result.count(item) for item in sentence_terminators
    ) != 1:
        raise ValueError("proposal.justification must be exactly one sentence")
    return result


def _id(value: Any, context: str) -> str:
    result = _string(value, context)
    if not _CANONICAL_ID.fullmatch(result):
        raise ValueError(f"{context} contains unsupported characters")
    return result


def _enum(enum_type: type[Enum], value: Any, context: str) -> Any:
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        allowed = [item.value for item in enum_type]
        raise ValueError(
            f"{context} enum value is invalid; "
            f"actual={value!r}, allowed={allowed!r}"
        ) from exc


def _string_array(value: Any, context: str) -> Tuple[str, ...]:
    items = _list(value, context, maximum=32)
    result = tuple(_string(item, f"{context} item", maximum=256) for item in items)
    if len(result) != len(set(result)):
        raise ValueError(f"{context} must not contain duplicates")
    return result


def _capabilities(value: Any, context: str) -> Tuple[EvidenceCapability, ...]:
    raw = _string_array(value, context)
    forbidden = tuple(sorted({item for item in raw if item not in _JIT_CAPABILITIES}))
    if forbidden:
        raise ValueError(
            f"{context} contains JIT-forbidden capabilities; "
            f"forbidden={list(forbidden)!r}; allowed={list(_JIT_CAPABILITIES)!r}"
        )
    return tuple(EvidenceCapability(item) for item in raw)


def _criterion(value: Any, index: int) -> CriterionIR:
    context = f"proposal.criteria[{index}]"
    payload = _mapping(value, context)
    _keys(
        payload,
        {
            "criterion_id",
            "temporal_semantics",
            "required",
            "allow_obscured_persistence",
            "required_capabilities",
            "description",
        },
        context,
    )
    if not isinstance(payload["required"], bool) or not isinstance(
        payload["allow_obscured_persistence"], bool
    ):
        raise ValueError(f"{context} flags must be boolean")
    description = payload["description"]
    if not isinstance(description, str) or len(description) > 1000:
        raise ValueError(f"{context}.description must be a bounded string")
    return CriterionIR(
        criterion_id=_id(payload["criterion_id"], f"{context}.criterion_id"),
        temporal_semantics=_enum(
            TemporalSemantics,
            payload["temporal_semantics"],
            f"{context}.temporal_semantics",
        ),
        required=payload["required"],
        allow_obscured_persistence=payload["allow_obscured_persistence"],
        required_capabilities=_capabilities(
            payload["required_capabilities"], f"{context}.required_capabilities"
        ),
        description=description,
    )


def _roi(value: Any, binding_index: int, roi_index: int) -> ContractRoiIR:
    context = f"proposal.g1_bindings[{binding_index}].rois[{roi_index}]"
    payload = _mapping(value, context)
    _keys(
        payload,
        {"roi_id", "bounds", "coordinate_space", "reference_size"},
        context,
    )
    raw_bounds = _list(payload["bounds"], f"{context}.bounds", minimum=4, maximum=4)
    bounds = []
    for item in raw_bounds:
        if (
            not isinstance(item, (int, float))
            or isinstance(item, bool)
            or not math.isfinite(item)
        ):
            raise ValueError(f"{context}.bounds must contain finite numbers")
        bounds.append(float(item))
    raw_reference = payload["reference_size"]
    reference_size = None
    if raw_reference is not None:
        values = _list(
            raw_reference, f"{context}.reference_size", minimum=2, maximum=2
        )
        if any(
            not isinstance(item, int) or isinstance(item, bool) or item <= 0
            for item in values
        ):
            raise ValueError(f"{context}.reference_size must contain positive integers")
        reference_size = tuple(values)
    return ContractRoiIR(
        roi_id=_id(payload["roi_id"], f"{context}.roi_id"),
        bounds=tuple(bounds),  # type: ignore[arg-type]
        coordinate_space=_enum(
            RoiCoordinateSpace,
            payload["coordinate_space"],
            f"{context}.coordinate_space",
        ),
        reference_size=reference_size,  # type: ignore[arg-type]
    )


def _binding(value: Any, index: int) -> G1CriterionBindingIR:
    context = f"proposal.g1_bindings[{index}]"
    payload = _mapping(value, context)
    _keys(payload, {"criterion_id", "checker", "rois"}, context)
    return G1CriterionBindingIR(
        criterion_id=_id(payload["criterion_id"], f"{context}.criterion_id"),
        checker=_enum(G1CheckerKind, payload["checker"], f"{context}.checker"),
        rois=tuple(
            _roi(item, index, roi_index)
            for roi_index, item in enumerate(
                _list(payload["rois"], f"{context}.rois", minimum=1, maximum=16)
            )
        ),
    )


def _checker_parameters(
    checker_id: str, value: Any, context: str
) -> Mapping[str, Any]:
    payload = _mapping(value, context)
    _keys(
        payload,
        {"any", "all", "none", "pattern", "ignore_case", "prompt", "expected_true"},
        context,
    )
    any_values = _string_array(payload["any"], f"{context}.any")
    all_values = _string_array(payload["all"], f"{context}.all")
    none_values = _string_array(payload["none"], f"{context}.none")
    pattern = payload["pattern"]
    prompt = payload["prompt"]
    ignore_case = payload["ignore_case"]
    expected_true = payload["expected_true"]
    if pattern is not None and (
        not isinstance(pattern, str) or not pattern or len(pattern) > 1000
    ):
        raise ValueError(f"{context}.pattern is invalid")
    if prompt is not None and (
        not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 2000
    ):
        raise ValueError(f"{context}.prompt is invalid")
    if ignore_case is not None and not isinstance(ignore_case, bool):
        raise ValueError(f"{context}.ignore_case is invalid")
    if expected_true is not None and not isinstance(expected_true, bool):
        raise ValueError(f"{context}.expected_true is invalid")
    actual_state = {
        "any_count": len(any_values),
        "all_count": len(all_values),
        "none_count": len(none_values),
        "pattern": None if pattern is None else type(pattern).__name__,
        "ignore_case": None if ignore_case is None else type(ignore_case).__name__,
        "prompt": None if prompt is None else type(prompt).__name__,
        "expected_true": (
            None if expected_true is None else type(expected_true).__name__
        ),
    }
    common_allowed = (
        "parameters must contain exactly keys any/all/none/pattern/"
        "ignore_case/prompt/expected_true"
    )
    if checker_id == "text":
        if not (any_values or all_values) or any(
            item is not None for item in (pattern, ignore_case, prompt, expected_true)
        ) or none_values:
            raise ValueError(
                "text checker parameters violate the JIT subset; "
                f"actual={actual_state!r}; allowed={common_allowed}; "
                "constraints=any or all must be non-empty, none must be [], "
                "pattern/ignore_case/prompt/expected_true must be null"
            )
        return {"any": any_values, "all": all_values}
    if checker_id == "xml":
        if not (any_values or all_values or none_values) or any(
            item is not None for item in (pattern, ignore_case, prompt, expected_true)
        ):
            raise ValueError(
                "xml checker parameters violate the JIT subset; "
                f"actual={actual_state!r}; allowed={common_allowed}; "
                "constraints=at least one of any/all/none must be non-empty, "
                "pattern/ignore_case/prompt/expected_true must be null"
            )
        return {"any": any_values, "all": all_values, "none": none_values}
    if checker_id == "regex":
        if (
            pattern is None
            or ignore_case is None
            or any_values
            or all_values
            or none_values
            or prompt is not None
            or expected_true is not None
        ):
            raise ValueError(
                "regex checker parameters violate the JIT subset; "
                f"actual={actual_state!r}; allowed={common_allowed}; "
                "constraints=pattern must be a non-empty string, ignore_case "
                "must be boolean, any/all/none must be [], prompt/expected_true "
                "must be null"
            )
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ValueError("regex checker pattern is invalid") from exc
        return {"pattern": pattern, "ignore_case": ignore_case}
    if checker_id == "ocr":
        if (
            not (any_values or all_values or pattern)
            or ignore_case is None
            or none_values
            or prompt is not None
            or expected_true is not None
        ):
            raise ValueError(
                "ocr checker parameters violate the JIT subset; "
                f"actual={actual_state!r}; allowed={common_allowed}; "
                "constraints=any or all or pattern must be non-empty, none "
                "must be [], ignore_case must be boolean, prompt/expected_true "
                "must be null"
            )
        return {
            "any": any_values,
            "all": all_values,
            "pattern": pattern,
            "ignore_case": ignore_case,
        }
    if checker_id == "llm":
        if (
            prompt is None
            or expected_true is None
            or any_values
            or all_values
            or none_values
            or pattern is not None
            or ignore_case is not None
        ):
            raise ValueError(
                "llm checker parameters violate the JIT subset; "
                f"actual={actual_state!r}; allowed={common_allowed}; "
                "constraints=prompt must be a non-empty string, expected_true "
                "must be boolean, any/all/none must be [], pattern/ignore_case "
                "must be null"
            )
        return {"prompt": prompt, "expected_true": expected_true}
    raise ValueError(
        "checker_id is outside the JIT checker subset; "
        f"actual={checker_id!r}, allowed={list(_JIT_DAG_CHECKERS)!r}"
    )


def _dag(value: Any) -> Optional[ContractDagIR]:
    if value is None:
        return None
    payload = _mapping(value, "proposal.dag")
    _keys(payload, {"nodes", "edges", "success"}, "proposal.dag")
    nodes = []
    for index, raw_node in enumerate(
        _list(payload["nodes"], "proposal.dag.nodes", minimum=1, maximum=MAX_DAG_NODES)
    ):
        context = f"proposal.dag.nodes[{index}]"
        node = _mapping(raw_node, context)
        _keys(node, {"node_id", "condition_operator", "score", "checkers"}, context)
        score = node["score"]
        if (
            not isinstance(score, int)
            or isinstance(score, bool)
            or not 0 <= score <= 100
        ):
            raise ValueError(
                f"{context}.score is invalid; actual={score!r}, "
                f"actual_type={type(score).__name__!r}, "
                "allowed=JSON integer from 0 through 100 and not boolean"
            )
        checkers = []
        for checker_index, raw_checker in enumerate(
            _list(node["checkers"], f"{context}.checkers", minimum=1, maximum=10)
        ):
            checker_context = f"{context}.checkers[{checker_index}]"
            checker = _mapping(raw_checker, checker_context)
            _keys(checker, {"checker_id", "parameters"}, checker_context)
            checker_id = _string(checker["checker_id"], f"{checker_context}.checker_id")
            if checker_id not in _JIT_DAG_CHECKERS:
                raise ValueError(
                    f"{checker_context}.checker_id is outside the JIT checker "
                    f"subset; actual={checker_id!r}, "
                    f"allowed={list(_JIT_DAG_CHECKERS)!r}"
                )
            checkers.append(
                ContractCheckerIR(
                    checker_id,
                    _checker_parameters(
                        checker_id,
                        checker["parameters"],
                        f"{checker_context}.parameters",
                    ),
                )
            )
        checker_ids = tuple(checker.checker_id for checker in checkers)
        duplicates = sorted(
            {
                checker_id
                for checker_id in checker_ids
                if checker_ids.count(checker_id) > 1
            }
        )
        if duplicates:
            raise ValueError(
                f"{context}.checkers contains duplicate checker_id values; "
                f"duplicates={duplicates!r}; actual={list(checker_ids)!r}; "
                "allowed=each DAG node may use each checker_id at most once; "
                "combine same-type conditions into one checker parameters object "
                "or split them into separate DAG nodes"
            )
        operator = _enum(
            DagLogicalOperator,
            node["condition_operator"],
            f"{context}.condition_operator",
        )
        condition_payload = {
            "condition_operator": operator.value,
            "checkers": [
                {
                    "checker_id": checker.checker_id,
                    "parameters": _plain_json(checker.parameters),
                }
                for checker in checkers
            ],
        }
        nodes.append(
            ContractDagNodeIR(
                node_id=_id(node["node_id"], f"{context}.node_id"),
                condition_operator=operator,
                checker_ids=tuple(checker.checker_id for checker in checkers),
                condition_sha256=_digest(condition_payload),
                checkers=tuple(checkers),
                score=score,
            )
        )
    edges = []
    for index, raw_edge in enumerate(
        _list(payload["edges"], "proposal.dag.edges", maximum=MAX_DAG_EDGES)
    ):
        context = f"proposal.dag.edges[{index}]"
        edge = _mapping(raw_edge, context)
        _keys(edge, {"parent_id", "child_id", "kind"}, context)
        edges.append(
            ContractDagEdgeIR(
                parent_id=_id(edge["parent_id"], f"{context}.parent_id"),
                child_id=_id(edge["child_id"], f"{context}.child_id"),
                kind=_enum(DagEdgeKind, edge["kind"], f"{context}.kind"),
            )
        )
    success_payload = _mapping(payload["success"], "proposal.dag.success")
    _keys(success_payload, {"operator", "node_ids"}, "proposal.dag.success")
    success_ids = tuple(
        _id(item, "proposal.dag.success.node_ids item")
        for item in _list(
            success_payload["node_ids"],
            "proposal.dag.success.node_ids",
            minimum=1,
            maximum=MAX_DAG_NODES,
        )
    )
    return ContractDagIR(
        nodes=tuple(nodes),
        edges=tuple(edges),
        success=ContractDagSuccessIR(
            _enum(
                DagLogicalOperator,
                success_payload["operator"],
                "proposal.dag.success.operator",
            ),
            success_ids,
        ),
    )


def _parse_proposal(raw: bytes) -> tuple[Mapping[str, Any], str]:
    if len(raw) > MAX_PROPOSAL_BYTES:
        raise JitCompilationError(
            JitCompilationFailureCode.INVALID_JSON,
            "JIT proposal exceeds its byte limit",
        )
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=lambda token: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON number: {token}")
            ),
        )
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise JitCompilationError(
            JitCompilationFailureCode.INVALID_JSON,
            "JIT proposal is not strict UTF-8 JSON",
        ) from exc
    try:
        payload = _mapping(value, "proposal")
        _keys(
            payload,
            {
                "schema_version",
                "task_family",
                "justification",
                "required_capabilities",
                "criteria",
                "g1_bindings",
                "dag",
            },
            "proposal",
        )
        if payload["schema_version"] != JIT_PROPOSAL_SCHEMA_VERSION:
            raise ValueError(
                "proposal schema_version is unsupported; "
                f"expected={JIT_PROPOSAL_SCHEMA_VERSION!r}, "
                f"actual={payload['schema_version']!r}"
            )
    except ValueError as exc:
        raise JitCompilationError(
            JitCompilationFailureCode.SCHEMA_VIOLATION,
            f"JIT proposal violates the strict schema: {exc}",
        ) from exc
    return payload, _digest(payload)


def _proposer_identity(proposer: Any) -> tuple[str, str]:
    values = []
    for name in ("proposer_id", "proposer_version"):
        value = getattr(proposer, name, None)
        if (
            not isinstance(value, str)
            or not value.strip()
            or value != value.strip()
            or len(value) > 128
        ):
            raise JitCompilationError(
                JitCompilationFailureCode.INVALID_PROPOSER,
                f"JIT proposer {name} is invalid",
            )
        values.append(value)
    if not callable(getattr(proposer, "propose", None)):
        raise JitCompilationError(
            JitCompilationFailureCode.INVALID_PROPOSER,
            "JIT proposer has no propose method",
        )
    return values[0], values[1]


def _proposer_payload(
    request: JitCompileRequest, feedback: Sequence[str]
) -> Mapping[str, Any]:
    payload = _plain_json(request.proposer_payload())
    if feedback:
        payload["compiler_feedback"] = list(feedback)
    frozen = _freeze_json(payload)
    if not isinstance(frozen, Mapping):
        raise AssertionError("JIT proposer payload must be a mapping")
    return frozen


def _compile_jit_contract_once(
    request: JitCompileRequest,
    proposer: JitProposer,
    *,
    proposer_id: str,
    proposer_version: str,
    spec: JitStructuredOutputSpec,
    feedback: Sequence[str],
) -> CompiledJitContract:
    try:
        response = proposer.propose(
            _proposer_payload(request, feedback), response_format=spec
        )
    except Exception as exc:
        raise JitCompilationError(
            JitCompilationFailureCode.PROPOSER_FAILURE,
            "JIT proposer failed without producing a contract",
        ) from exc
    if not isinstance(response, JitProposalResponse):
        raise JitCompilationError(
            JitCompilationFailureCode.PROPOSER_FAILURE,
            "JIT proposer returned an invalid response envelope",
        )
    try:
        response.validate()
    except ValueError as exc:
        raise JitCompilationError(
            JitCompilationFailureCode.PROPOSER_FAILURE,
            "JIT proposer returned an invalid response envelope",
        ) from exc
    if response.refusal is not None:
        raise JitCompilationError(
            JitCompilationFailureCode.REFUSED,
            "JIT proposer refused; no ContractIR was created",
        )
    assert response.json_bytes is not None
    payload, proposal_sha256 = _parse_proposal(response.json_bytes)
    try:
        justification = _justification(payload["justification"])
        task_family = payload["task_family"]
        if task_family is not None:
            task_family = _string(task_family, "proposal.task_family")
        if (
            request.app_metadata.task_family is not None
            and task_family != request.app_metadata.task_family
        ):
            raise ValueError("proposal task_family differs from app metadata")
        criteria = tuple(
            _criterion(item, index)
            for index, item in enumerate(
                _list(
                    payload["criteria"],
                    "proposal.criteria",
                    minimum=1,
                    maximum=MAX_CRITERIA,
                )
            )
        )
        bindings = tuple(
            _binding(item, index)
            for index, item in enumerate(
                _list(
                    payload["g1_bindings"],
                    "proposal.g1_bindings",
                    maximum=MAX_BINDINGS,
                )
            )
        )
        dag = _dag(payload["dag"])
        if dag is not None and not any(
            criterion.criterion_id == "jit.dag_execution" and criterion.required
            for criterion in criteria
        ):
            raise ValueError(
                "a JIT DAG requires the required criterion jit.dag_execution"
            )
        capabilities = _capabilities(
            payload["required_capabilities"],
            "proposal.required_capabilities",
        )
    except ValueError as exc:
        raise JitCompilationError(
            JitCompilationFailureCode.SCHEMA_VIOLATION,
            f"JIT proposal violates typed ContractIR construction: {exc}",
        ) from exc
    input_sha256 = request.input_sha256
    source_digest = _digest(
        {
            "compiler_version": JIT_COMPILER_VERSION,
            "input_sha256": input_sha256,
            "proposal_sha256": proposal_sha256,
            "structured_output_schema_sha256": spec.schema_sha256,
            "proposer_id": proposer_id,
            "proposer_version": proposer_version,
        }
    )
    contract = ContractIR(
        contract_id=(
            f"jit:{request.app_metadata.app_id}:{proposal_sha256[:24]}"
        ),
        criteria=criteria,
        source=JIT_CONTRACT_SOURCE,
        task_family=task_family,
        required_capabilities=capabilities,
        g1_bindings=bindings,
        dag=dag,
        compiler_provenance=ContractProvenanceIR(
            source_type=ContractSourceType.VALIDATED_JIT,
            source_id=proposer_id,
            source_version=proposer_version,
            source_digest=source_digest,
            source_locator=f"jit-proposal:{proposal_sha256}",
            selection_key=request.selection_key,
        ),
        metadata={
            "app_id": request.app_metadata.app_id,
            "jit_input_sha256": input_sha256,
            "jit_proposal_sha256": proposal_sha256,
            "jit_structured_output_schema_sha256": spec.schema_sha256,
        },
    )
    try:
        validated = _validation.validate_and_freeze_contract(
            contract,
            expected_source=JIT_CONTRACT_SOURCE,
        )
    except _validation.ContractValidationError as exc:
        raise JitCompilationError(
            JitCompilationFailureCode.VALIDATION_REJECTED,
            f"JIT ContractIR rejected by the shared validator: {exc}",
            validation_code=exc.code,
        ) from exc
    return CompiledJitContract(
        validated.contract,
        validated.contract_sha256,
        input_sha256,
        proposal_sha256,
        justification,
        spec.schema_sha256,
        proposer_id,
        proposer_version,
        validated.validation_funnel_version,
    )


def compile_jit_contract(
    request: JitCompileRequest,
    proposer: JitProposer,
) -> CompiledJitContract:
    """Propose from task-only input, parse strict JSON, then enter the sole funnel."""

    if not isinstance(request, JitCompileRequest):
        raise JitCompilationError(
            JitCompilationFailureCode.INVALID_INPUT,
            "JIT request must be a JitCompileRequest",
        )
    try:
        request.validate()
    except ValueError as exc:
        raise JitCompilationError(
            JitCompilationFailureCode.INVALID_INPUT,
            f"invalid task-only JIT input: {exc}",
        ) from exc
    proposer_id, proposer_version = _proposer_identity(proposer)
    spec = jit_structured_output_spec()
    supports_feedback = getattr(proposer, "supports_compiler_feedback", False) is True
    max_attempts = 1 + (
        MAX_JIT_COMPILER_FEEDBACK_ATTEMPTS if supports_feedback else 0
    )
    feedback: Tuple[str, ...] = ()
    for attempt_index in range(max_attempts):
        try:
            return _compile_jit_contract_once(
                request,
                proposer,
                proposer_id=proposer_id,
                proposer_version=proposer_version,
                spec=spec,
                feedback=feedback,
            )
        except JitCompilationError as exc:
            retryable = exc.code in {
                JitCompilationFailureCode.INVALID_JSON,
                JitCompilationFailureCode.SCHEMA_VIOLATION,
            }
            if (
                not supports_feedback
                or not retryable
                or attempt_index >= max_attempts - 1
            ):
                raise
            feedback = (*feedback, _feedback_message(exc))
    raise AssertionError("unreachable JIT compilation attempt loop exit")


__all__ = [
    "JIT_COMPILER_VERSION",
    "JIT_CONTRACT_SOURCE",
    "JIT_PROPOSAL_SCHEMA_VERSION",
    "JIT_STRUCTURED_OUTPUT_NAME",
    "CompiledJitContract",
    "JitAppMetadata",
    "JitCompilationError",
    "JitCompilationFailureCode",
    "JitCompileRequest",
    "JitProposalResponse",
    "JitProposer",
    "JitStructuredOutputSpec",
    "compile_jit_contract",
    "jit_contract_proposal_json_schema",
    "jit_structured_output_spec",
]
