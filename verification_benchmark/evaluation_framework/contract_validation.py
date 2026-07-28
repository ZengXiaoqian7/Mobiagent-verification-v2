"""Single final validation and hash-freeze funnel for every contract source."""

from __future__ import annotations

import hmac
import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from .event_log import contract_sha256
from .models import ContractIR, EvidenceCapability, TemporalSemantics


CONTRACT_VALIDATION_FUNNEL_VERSION = "harmony-eval-contract-validation-v1"
SUPPORTED_DAG_CHECKER_IDS = frozenset(
    {
        "action",
        "dynamic_match",
        "icons",
        "llm",
        "ocr",
        "regex",
        "text",
        "ui",
        "visual_state",
        "xml",
    }
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ContractValidationFailureCode(str, Enum):
    INVALID_CONTRACT = "INVALID_CONTRACT"
    SOURCE_MISMATCH = "SOURCE_MISMATCH"
    INVALID_EXPECTED_HASH = "INVALID_EXPECTED_HASH"
    HASH_MISMATCH = "HASH_MISMATCH"


class ContractValidationError(ValueError):
    def __init__(self, code: ContractValidationFailureCode, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class ValidatedContract:
    contract: ContractIR
    contract_sha256: str
    validation_funnel_version: str = CONTRACT_VALIDATION_FUNNEL_VERSION

    def __post_init__(self) -> None:
        if not hmac.compare_digest(contract_sha256(self.contract), self.contract_sha256):
            raise ValueError("validated contract hash does not match ContractIR")
        if self.validation_funnel_version != CONTRACT_VALIDATION_FUNNEL_VERSION:
            raise ValueError("validated contract funnel version is unsupported")


def validate_and_freeze_contract(
    contract: ContractIR,
    *,
    expected_source: str,
    expected_contract_sha256: Optional[str] = None,
) -> ValidatedContract:
    """Apply the sole source-independent semantic validator and canonical hash freeze."""

    if not isinstance(contract, ContractIR):
        raise ContractValidationError(
            ContractValidationFailureCode.INVALID_CONTRACT,
            "contract must be a ContractIR",
        )
    if not isinstance(expected_source, str) or not expected_source.strip():
        raise ContractValidationError(
            ContractValidationFailureCode.SOURCE_MISMATCH,
            "expected_source must be non-empty",
        )
    if contract.source != expected_source:
        raise ContractValidationError(
            ContractValidationFailureCode.SOURCE_MISMATCH,
            f"contract source must be {expected_source!r}",
        )
    compiler_sources = {
        "frozen-registry",
        "family-template",
        "validated-jit",
        "legacy-yaml-adapter",
    }
    if contract.source in compiler_sources and contract.compiler_provenance is None:
        raise ContractValidationError(
            ContractValidationFailureCode.INVALID_CONTRACT,
            "compiler-produced ContractIR must contain typed compiler provenance",
        )
    try:
        contract.validate()
    except ValueError as exc:
        raise ContractValidationError(
            ContractValidationFailureCode.INVALID_CONTRACT,
            f"invalid ContractIR: {exc}",
        ) from exc
    if not any(
        criterion.required
        and criterion.temporal_semantics is not TemporalSemantics.PROCESS_OBLIGATION
        for criterion in contract.criteria
    ):
        raise ContractValidationError(
            ContractValidationFailureCode.INVALID_CONTRACT,
            "contract must contain a required outcome success path",
        )
    criteria_by_id = {criterion.criterion_id: criterion for criterion in contract.criteria}
    for binding in contract.g1_bindings:
        criterion = criteria_by_id[binding.criterion_id]
        declared_capabilities = set(contract.required_capabilities).union(
            criterion.required_capabilities
        )
        if EvidenceCapability.SCREENSHOT not in declared_capabilities:
            raise ContractValidationError(
                ContractValidationFailureCode.INVALID_CONTRACT,
                f"G1 binding {binding.criterion_id!r} requires declared SCREENSHOT capability",
            )
    if contract.dag is not None:
        unsupported = sorted(
            {
                checker.checker_id
                for node in contract.dag.nodes
                for checker in node.checkers
                if checker.checker_id not in SUPPORTED_DAG_CHECKER_IDS
            }
        )
        if unsupported:
            raise ContractValidationError(
                ContractValidationFailureCode.INVALID_CONTRACT,
                f"contract DAG contains unsupported checkers: {unsupported}",
            )

    digest = contract_sha256(contract)
    if expected_contract_sha256 is not None:
        if (
            not isinstance(expected_contract_sha256, str)
            or not _SHA256.fullmatch(expected_contract_sha256)
        ):
            raise ContractValidationError(
                ContractValidationFailureCode.INVALID_EXPECTED_HASH,
                "expected_contract_sha256 must be a lowercase SHA-256",
            )
        if not hmac.compare_digest(digest, expected_contract_sha256):
            raise ContractValidationError(
                ContractValidationFailureCode.HASH_MISMATCH,
                "contract canonical hash does not match expected_contract_sha256",
            )
    return ValidatedContract(contract=contract, contract_sha256=digest)


__all__ = [
    "CONTRACT_VALIDATION_FUNNEL_VERSION",
    "SUPPORTED_DAG_CHECKER_IDS",
    "ContractValidationError",
    "ContractValidationFailureCode",
    "ValidatedContract",
    "validate_and_freeze_contract",
]
