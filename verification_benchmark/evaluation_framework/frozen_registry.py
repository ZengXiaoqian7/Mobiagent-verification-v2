"""Strict Frozen Registry serialization and pre-execution contract compilation."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Optional, Tuple

from . import contract_validation as _validation
from .event_log import contract_sha256
from .models import (
    ContractIR,
    ContractProvenanceIR,
    ContractRoiIR,
    ContractSourceType,
    CriterionIR,
    EvidenceCapability,
    G1CheckerKind,
    G1CriterionBindingIR,
    RoiCoordinateSpace,
    TemporalSemantics,
)


FROZEN_REGISTRY_SCHEMA_VERSION = "harmony-eval-frozen-registry-v1"
FROZEN_REGISTRY_SOURCE = "frozen-registry"


class FrozenRegistryFailureCode(str, Enum):
    UNREADABLE = "UNREADABLE"
    INVALID_JSON = "INVALID_JSON"
    INVALID_SCHEMA = "INVALID_SCHEMA"
    DUPLICATE_KEY = "DUPLICATE_KEY"
    MISSING_KEY = "MISSING_KEY"
    HASH_MISMATCH = "HASH_MISMATCH"


class FrozenRegistryError(ValueError):
    """Typed contract compilation failure; never a trace-integrity verdict."""

    def __init__(self, code: FrozenRegistryFailureCode, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class FrozenRegistryProvenance:
    registry_id: str
    revision: str
    source_digest: str
    source: str = FROZEN_REGISTRY_SOURCE


@dataclass(frozen=True)
class FrozenContract:
    registry_key: str
    contract: ContractIR
    contract_sha256: str
    provenance: FrozenRegistryProvenance
    validation_funnel_version: str = _validation.CONTRACT_VALIDATION_FUNNEL_VERSION

    def __post_init__(self) -> None:
        actual = contract_sha256(self.contract)
        if not hmac.compare_digest(actual, self.contract_sha256):
            raise ValueError("frozen contract hash does not match ContractIR")
        if self.validation_funnel_version != _validation.CONTRACT_VALIDATION_FUNNEL_VERSION:
            raise ValueError("frozen contract did not pass the supported validation funnel")
        compiler_provenance = self.contract.compiler_provenance
        if compiler_provenance is None:
            raise ValueError("frozen contract is missing compiler provenance")
        if (
            compiler_provenance.source_id != self.provenance.registry_id
            or compiler_provenance.source_version != self.provenance.revision
            or compiler_provenance.source_digest != self.provenance.source_digest
            or compiler_provenance.source_locator != self.registry_key
        ):
            raise ValueError("frozen contract compiler provenance does not match registry")


@dataclass(frozen=True)
class FrozenContractRegistry:
    provenance: FrozenRegistryProvenance
    contracts: Tuple[FrozenContract, ...]
    schema_version: str = FROZEN_REGISTRY_SCHEMA_VERSION

    def get(self, registry_key: str) -> FrozenContract:
        if not isinstance(registry_key, str) or not registry_key.strip():
            raise FrozenRegistryError(
                FrozenRegistryFailureCode.MISSING_KEY,
                "registry key must be a non-empty string",
            )
        matches = tuple(item for item in self.contracts if item.registry_key == registry_key)
        if not matches:
            raise FrozenRegistryError(
                FrozenRegistryFailureCode.MISSING_KEY,
                f"frozen registry key not found: {registry_key}",
            )
        if len(matches) != 1:
            raise FrozenRegistryError(
                FrozenRegistryFailureCode.DUPLICATE_KEY,
                f"duplicate frozen registry key at lookup: {registry_key}",
            )
        return matches[0]


def _failure(message: str) -> FrozenRegistryError:
    return FrozenRegistryError(FrozenRegistryFailureCode.INVALID_SCHEMA, message)


def _mapping(value: Any, context: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _failure(f"{context} must be an object")
    return value


def _keys(value: Mapping[str, Any], expected: set[str], context: str) -> None:
    actual = set(value)
    if actual != expected:
        raise _failure(
            f"{context} keys mismatch; missing={sorted(expected - actual)}, "
            f"unexpected={sorted(actual - expected)}"
        )


def _string(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise _failure(f"{context} must be a non-empty canonical string")
    return value


def _boolean(value: Any, context: str) -> bool:
    if not isinstance(value, bool):
        raise _failure(f"{context} must be boolean")
    return value


def _list(value: Any, context: str) -> list[Any]:
    if not isinstance(value, list):
        raise _failure(f"{context} must be an array")
    return value


def _enum(enum_type: type[Enum], value: Any, context: str) -> Enum:
    if not isinstance(value, str):
        raise _failure(f"{context} must be a string enum")
    try:
        return enum_type(value)
    except ValueError as exc:
        raise _failure(f"unsupported {context}: {value}") from exc


def _capabilities(value: Any, context: str) -> Tuple[EvidenceCapability, ...]:
    raw = _list(value, context)
    capabilities = tuple(
        _enum(EvidenceCapability, item, f"{context}[{index}]")
        for index, item in enumerate(raw)
    )
    if len(capabilities) != len(set(capabilities)):
        raise _failure(f"{context} must not contain duplicates")
    return capabilities  # type: ignore[return-value]


def _metadata(value: Any) -> Mapping[str, Any]:
    metadata = _mapping(value, "contract.metadata")
    if set(metadata) not in (set(), {"fixture_scope"}):
        raise _failure(
            "contract.metadata supports only the development fixture_scope in this Gate"
        )
    if metadata and metadata["fixture_scope"] != "development-test-only":
        raise _failure(
            "contract.metadata.fixture_scope must be 'development-test-only'"
        )
    return metadata


def _criterion(value: Any, index: int) -> CriterionIR:
    context = f"contract.criteria[{index}]"
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
    description = payload["description"]
    if not isinstance(description, str):
        raise _failure(f"{context}.description must be a string")
    return CriterionIR(
        criterion_id=_string(payload["criterion_id"], f"{context}.criterion_id"),
        temporal_semantics=_enum(
            TemporalSemantics,
            payload["temporal_semantics"],
            f"{context}.temporal_semantics",
        ),  # type: ignore[arg-type]
        required=_boolean(payload["required"], f"{context}.required"),
        allow_obscured_persistence=_boolean(
            payload["allow_obscured_persistence"],
            f"{context}.allow_obscured_persistence",
        ),
        required_capabilities=_capabilities(
            payload["required_capabilities"], f"{context}.required_capabilities"
        ),
        description=description,
    )


def _number(value: Any, context: str) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
    ):
        raise _failure(f"{context} must be a finite number")
    return float(value)


def _reference_size(value: Any, context: str) -> Optional[Tuple[int, int]]:
    if value is None:
        return None
    values = _list(value, context)
    if len(values) != 2 or any(
        not isinstance(item, int) or isinstance(item, bool) or item <= 0 for item in values
    ):
        raise _failure(f"{context} must contain two positive integers")
    return values[0], values[1]


def _roi(value: Any, binding_index: int, roi_index: int) -> ContractRoiIR:
    context = f"contract.g1_bindings[{binding_index}].rois[{roi_index}]"
    payload = _mapping(value, context)
    _keys(
        payload,
        {"roi_id", "bounds", "coordinate_space", "reference_size"},
        context,
    )
    raw_bounds = _list(payload["bounds"], f"{context}.bounds")
    if len(raw_bounds) != 4:
        raise _failure(f"{context}.bounds must contain four numbers")
    return ContractRoiIR(
        roi_id=_string(payload["roi_id"], f"{context}.roi_id"),
        bounds=tuple(
            _number(item, f"{context}.bounds[{index}]")
            for index, item in enumerate(raw_bounds)
        ),  # type: ignore[arg-type]
        coordinate_space=_enum(
            RoiCoordinateSpace,
            payload["coordinate_space"],
            f"{context}.coordinate_space",
        ),  # type: ignore[arg-type]
        reference_size=_reference_size(payload["reference_size"], f"{context}.reference_size"),
    )


def _binding(value: Any, index: int) -> G1CriterionBindingIR:
    context = f"contract.g1_bindings[{index}]"
    payload = _mapping(value, context)
    _keys(payload, {"criterion_id", "checker", "rois"}, context)
    return G1CriterionBindingIR(
        criterion_id=_string(payload["criterion_id"], f"{context}.criterion_id"),
        checker=_enum(G1CheckerKind, payload["checker"], f"{context}.checker"),  # type: ignore[arg-type]
        rois=tuple(
            _roi(item, index, roi_index)
            for roi_index, item in enumerate(_list(payload["rois"], f"{context}.rois"))
        ),
    )


def _contract(
    value: Any,
    *,
    provenance: FrozenRegistryProvenance,
    registry_key: str,
) -> ContractIR:
    payload = _mapping(value, "contract")
    _keys(
        payload,
        {
            "schema_version",
            "contract_id",
            "source",
            "required_capabilities",
            "metadata",
            "criteria",
            "g1_bindings",
        },
        "contract",
    )
    if payload["source"] != FROZEN_REGISTRY_SOURCE:
        raise _failure(f"contract.source must be {FROZEN_REGISTRY_SOURCE!r}")
    criteria = tuple(
        _criterion(item, index)
        for index, item in enumerate(_list(payload["criteria"], "contract.criteria"))
    )
    bindings = tuple(
        _binding(item, index)
        for index, item in enumerate(_list(payload["g1_bindings"], "contract.g1_bindings"))
    )
    try:
        return ContractIR(
            schema_version=_string(payload["schema_version"], "contract.schema_version"),
            contract_id=_string(payload["contract_id"], "contract.contract_id"),
            source=FROZEN_REGISTRY_SOURCE,
            compiler_provenance=ContractProvenanceIR(
                source_type=ContractSourceType.REGISTRY,
                source_id=provenance.registry_id,
                source_version=provenance.revision,
                source_digest=provenance.source_digest,
                source_locator=registry_key,
                selection_key=registry_key,
            ),
            required_capabilities=_capabilities(
                payload["required_capabilities"], "contract.required_capabilities"
            ),
            metadata=_metadata(payload["metadata"]),
            criteria=criteria,
            g1_bindings=bindings,
        )
    except ValueError as exc:
        raise _failure(f"invalid ContractIR: {exc}") from exc


def _provenance(value: Any, *, source_digest: str) -> FrozenRegistryProvenance:
    payload = _mapping(value, "provenance")
    _keys(payload, {"source", "registry_id", "revision"}, "provenance")
    if payload["source"] != FROZEN_REGISTRY_SOURCE:
        raise _failure(f"provenance.source must be {FROZEN_REGISTRY_SOURCE!r}")
    return FrozenRegistryProvenance(
        registry_id=_string(payload["registry_id"], "provenance.registry_id"),
        revision=_string(payload["revision"], "provenance.revision"),
        source_digest=source_digest,
    )


def _registry_source_digest(
    payload: Mapping[str, Any],
    raw_entries: list[Any],
) -> str:
    """Hash source semantics without the self-referential expected contract hashes."""

    contracts = []
    for index, raw_entry in enumerate(raw_entries):
        context = f"contracts[{index}]"
        entry = _mapping(raw_entry, context)
        allowed = {"registry_key", "contract", "expected_contract_sha256"}
        if set(entry) not in ({"registry_key", "contract"}, allowed):
            raise _failure(
                f"{context} keys mismatch; expected registry_key, contract, and optional "
                "expected_contract_sha256"
            )
        contracts.append(
            {
                "registry_key": _string(
                    entry["registry_key"], f"{context}.registry_key"
                ),
                "contract": entry["contract"],
            }
        )
    contracts.sort(key=lambda item: item["registry_key"])
    semantic_payload = {
        "schema_version": payload["schema_version"],
        "provenance": payload["provenance"],
        "contracts": contracts,
    }
    try:
        rendered = json.dumps(
            semantic_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise _failure(f"frozen registry source is not canonical JSON: {exc}") from exc
    return hashlib.sha256(rendered).hexdigest()


def load_frozen_registry_payload(value: Any) -> FrozenContractRegistry:
    """Compile a decoded JSON payload without any fallback execution path."""

    payload = _mapping(value, "frozen registry")
    _keys(payload, {"schema_version", "provenance", "contracts"}, "frozen registry")
    if payload["schema_version"] != FROZEN_REGISTRY_SCHEMA_VERSION:
        raise _failure(f"unsupported frozen registry schema: {payload['schema_version']}")
    raw_entries = _list(payload["contracts"], "contracts")
    if not raw_entries:
        raise _failure("frozen registry must contain at least one contract")
    source_digest = _registry_source_digest(payload, raw_entries)
    provenance = _provenance(payload["provenance"], source_digest=source_digest)

    compiled = []
    seen_keys = set()
    seen_contract_ids = set()
    for index, raw_entry in enumerate(raw_entries):
        context = f"contracts[{index}]"
        entry = _mapping(raw_entry, context)
        allowed = {"registry_key", "contract", "expected_contract_sha256"}
        if set(entry) not in ({"registry_key", "contract"}, allowed):
            raise _failure(
                f"{context} keys mismatch; expected registry_key, contract, and optional "
                "expected_contract_sha256"
            )
        registry_key = _string(entry["registry_key"], f"{context}.registry_key")
        if registry_key in seen_keys:
            raise FrozenRegistryError(
                FrozenRegistryFailureCode.DUPLICATE_KEY,
                f"duplicate frozen registry key: {registry_key}",
            )
        contract = _contract(
            entry["contract"],
            provenance=provenance,
            registry_key=registry_key,
        )
        if contract.contract_id in seen_contract_ids:
            raise FrozenRegistryError(
                FrozenRegistryFailureCode.DUPLICATE_KEY,
                f"duplicate frozen contract id: {contract.contract_id}",
            )
        expected = entry.get("expected_contract_sha256")
        try:
            validated = _validation.validate_and_freeze_contract(
                contract,
                expected_source=FROZEN_REGISTRY_SOURCE,
                expected_contract_sha256=expected,
            )
        except _validation.ContractValidationError as exc:
            if exc.code is _validation.ContractValidationFailureCode.HASH_MISMATCH:
                raise FrozenRegistryError(
                    FrozenRegistryFailureCode.HASH_MISMATCH,
                    f"frozen contract hash mismatch for {registry_key}",
                ) from exc
            raise _failure(str(exc)) from exc
        compiled.append(
            FrozenContract(
                registry_key,
                validated.contract,
                validated.contract_sha256,
                provenance,
                validated.validation_funnel_version,
            )
        )
        seen_keys.add(registry_key)
        seen_contract_ids.add(contract.contract_id)
    return FrozenContractRegistry(
        provenance=provenance,
        contracts=tuple(sorted(compiled, key=lambda item: item.registry_key)),
    )


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise FrozenRegistryError(
                FrozenRegistryFailureCode.DUPLICATE_KEY,
                f"duplicate JSON object key: {key}",
            )
        result[key] = value
    return result


def load_frozen_registry(path: Path | str) -> FrozenContractRegistry:
    """Read and compile one versioned JSON registry; paths never enter contract hashes."""

    source = Path(path)
    try:
        text = source.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise FrozenRegistryError(
            FrozenRegistryFailureCode.UNREADABLE,
            f"frozen registry is unreadable: {exc}",
        ) from exc
    try:
        loaded = json.loads(text, object_pairs_hook=_reject_duplicate_json_keys)
    except FrozenRegistryError:
        raise
    except json.JSONDecodeError as exc:
        raise FrozenRegistryError(
            FrozenRegistryFailureCode.INVALID_JSON,
            f"frozen registry is invalid JSON: {exc}",
        ) from exc
    return load_frozen_registry_payload(loaded)
