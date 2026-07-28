"""Deterministic, auditable source selection for compiled ContractIR values."""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from enum import Enum
from typing import Any, Tuple

from . import contract_validation as _validation
from . import family_template as _template
from . import jit_contract_compiler as _jit
from .event_log import contract_sha256
from .family_template import (
    FamilyTemplateError,
    FamilyTemplateIR,
    FamilyTemplateParameterKind,
    FamilyTemplateParameterValue,
)
from .frozen_registry import (
    FrozenContractRegistry,
    FrozenRegistryError,
    FrozenRegistryFailureCode,
)
from .legacy_yaml_adapter import AdaptedLegacyContract
from .models import ContractIR, ContractSourceType


CONTRACT_SOURCE_ROUTER_VERSION = "harmony-eval-contract-source-router-v1"


class ContractSelectionDecision(str, Enum):
    MISS = "MISS"
    SELECTED = "SELECTED"
    REJECTED = "REJECTED"
    DISABLED = "DISABLED"


class ContractRouterFailureCode(str, Enum):
    INVALID_REQUEST = "INVALID_REQUEST"
    REGISTRY_REJECTED = "REGISTRY_REJECTED"
    TEMPLATE_REJECTED = "TEMPLATE_REJECTED"
    JIT_DISABLED = "JIT_DISABLED"
    JIT_NOT_IMPLEMENTED = "JIT_NOT_IMPLEMENTED"
    JIT_REJECTED = "JIT_REJECTED"


@dataclass(frozen=True)
class ContractSelectionAttempt:
    source_type: ContractSourceType
    decision: ContractSelectionDecision
    source_id: str
    source_version: str

    def validate(self) -> None:
        if not isinstance(self.source_type, ContractSourceType):
            raise ValueError("selection attempt source_type is invalid")
        if not isinstance(self.decision, ContractSelectionDecision):
            raise ValueError("selection attempt decision is invalid")
        for name, value in (
            ("source_id", self.source_id),
            ("source_version", self.source_version),
        ):
            if (
                not isinstance(value, str)
                or not value.strip()
                or value != value.strip()
            ):
                raise ValueError(f"selection attempt {name} must be canonical")


@dataclass(frozen=True)
class ContractSelectionAudit:
    selection_key: str
    attempts: Tuple[ContractSelectionAttempt, ...]
    router_version: str = CONTRACT_SOURCE_ROUTER_VERSION

    def validate(self, *, require_selected: bool) -> None:
        if (
            not isinstance(self.selection_key, str)
            or not self.selection_key.strip()
            or self.selection_key != self.selection_key.strip()
        ):
            raise ValueError("selection audit key must be canonical and non-empty")
        if self.router_version != CONTRACT_SOURCE_ROUTER_VERSION:
            raise ValueError("selection audit router version is unsupported")
        if not isinstance(self.attempts, tuple) or not self.attempts:
            raise ValueError("selection audit must contain attempts")
        for attempt in self.attempts:
            if not isinstance(attempt, ContractSelectionAttempt):
                raise ValueError("selection audit contains an invalid attempt")
            attempt.validate()
        selected = tuple(
            attempt
            for attempt in self.attempts
            if attempt.decision is ContractSelectionDecision.SELECTED
        )
        if len(selected) > 1:
            raise ValueError("selection audit may contain at most one selected source")
        if require_selected and len(selected) != 1:
            raise ValueError("successful selection audit must contain one selected source")
        if selected and self.attempts[-1] is not selected[0]:
            raise ValueError("source selection must short-circuit immediately after a hit")
        source_order = tuple(attempt.source_type for attempt in self.attempts)
        if source_order == (ContractSourceType.LEGACY,):
            return
        priority = (
            ContractSourceType.REGISTRY,
            ContractSourceType.TEMPLATE,
            ContractSourceType.VALIDATED_JIT,
        )
        if source_order != priority[: len(source_order)]:
            raise ValueError(
                "automatic source attempts must follow Registry -> Template -> Validated JIT"
            )
        if any(
            attempt.decision is not ContractSelectionDecision.MISS
            for attempt in self.attempts[:-1]
        ):
            raise ValueError("only an explicit miss may advance to a lower-priority source")


class ContractRouterError(ValueError):
    def __init__(
        self,
        code: ContractRouterFailureCode,
        message: str,
        audit: ContractSelectionAudit,
    ) -> None:
        audit.validate(require_selected=False)
        self.code = code
        self.audit = audit
        super().__init__(message)


@dataclass(frozen=True)
class FamilyTemplateRouteCandidate:
    selection_key: str
    template: FamilyTemplateIR
    parameters: Tuple[FamilyTemplateParameterValue, ...]

    def validate(self) -> None:
        if (
            not isinstance(self.selection_key, str)
            or not self.selection_key.strip()
            or self.selection_key != self.selection_key.strip()
        ):
            raise ValueError("template candidate selection_key must be canonical")
        if not isinstance(self.template, FamilyTemplateIR):
            raise ValueError("template candidate must contain a FamilyTemplateIR")
        self.template.validate()
        if not isinstance(self.parameters, tuple):
            raise ValueError("template candidate parameters must be a tuple")
        parameter_ids = []
        for parameter in self.parameters:
            if not isinstance(parameter, FamilyTemplateParameterValue):
                raise ValueError(
                    "template candidate parameters must contain typed parameter values"
                )
            if (
                not isinstance(parameter.parameter_id, str)
                or not parameter.parameter_id.strip()
                or parameter.parameter_id != parameter.parameter_id.strip()
            ):
                raise ValueError("template candidate parameter_id must be canonical")
            if not isinstance(parameter.kind, FamilyTemplateParameterKind):
                raise ValueError("template candidate parameter kind is invalid")
            if not isinstance(parameter.value, tuple):
                raise ValueError("template candidate parameter value must be immutable")
            parameter_ids.append(parameter.parameter_id)
        if len(parameter_ids) != len(set(parameter_ids)):
            raise ValueError("template candidate parameter ids must be unique")
        expected = {item.parameter_id: item.kind for item in self.template.parameters}
        actual = {item.parameter_id: item.kind for item in self.parameters}
        if actual != expected:
            raise ValueError("template candidate parameters do not match template specs")

    def parameter_mapping(self) -> dict[str, Any]:
        self.validate()
        return {parameter.parameter_id: parameter.value for parameter in self.parameters}


@dataclass(frozen=True)
class RoutedContract:
    contract: ContractIR
    contract_sha256: str
    audit: ContractSelectionAudit
    validation_funnel_version: str

    def __post_init__(self) -> None:
        actual = contract_sha256(self.contract)
        if not hmac.compare_digest(actual, self.contract_sha256):
            raise ValueError("routed contract hash does not match ContractIR")
        self.audit.validate(require_selected=True)
        provenance = self.contract.compiler_provenance
        if provenance is None:
            raise ValueError("routed contract is missing compiler provenance")
        selected = self.audit.attempts[-1]
        if selected.source_type is not provenance.source_type:
            raise ValueError("selection audit source does not match contract provenance")
        if (
            selected.source_id != provenance.source_id
            or selected.source_version != provenance.source_version
        ):
            raise ValueError("selection audit identity does not match contract provenance")
        if provenance.selection_key != self.audit.selection_key:
            raise ValueError("selection audit key does not match contract provenance")
        if self.validation_funnel_version != _validation.CONTRACT_VALIDATION_FUNNEL_VERSION:
            raise ValueError("routed contract did not pass the supported validation funnel")

    @property
    def selection_audit_sha256(self) -> str:
        return contract_selection_audit_sha256(self.audit)


def contract_selection_audit_payload(
    audit: ContractSelectionAudit,
) -> dict[str, Any]:
    if not isinstance(audit, ContractSelectionAudit):
        raise ValueError("audit must be a ContractSelectionAudit")
    audit.validate(require_selected=False)
    return {
        "router_version": audit.router_version,
        "selection_key": audit.selection_key,
        "attempts": [
            {
                "source_type": attempt.source_type.value,
                "decision": attempt.decision.value,
                "source_id": attempt.source_id,
                "source_version": attempt.source_version,
            }
            for attempt in audit.attempts
        ],
    }


def contract_selection_audit_sha256(audit: ContractSelectionAudit) -> str:
    rendered = json.dumps(
        contract_selection_audit_payload(audit),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(rendered).hexdigest()


def _audit(
    selection_key: str,
    attempts: list[ContractSelectionAttempt],
) -> ContractSelectionAudit:
    return ContractSelectionAudit(selection_key=selection_key, attempts=tuple(attempts))


def _selection_key(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or value != value.strip()
    ):
        audit = ContractSelectionAudit(
            selection_key="invalid-selection-key",
            attempts=(
                ContractSelectionAttempt(
                    ContractSourceType.REGISTRY,
                    ContractSelectionDecision.REJECTED,
                    "source-router-request",
                    CONTRACT_SOURCE_ROUTER_VERSION,
                ),
            ),
        )
        raise ContractRouterError(
            ContractRouterFailureCode.INVALID_REQUEST,
            "selection_key must be a canonical non-empty string",
            audit,
        )
    return value


def route_contract(
    selection_key: str,
    registry: FrozenContractRegistry,
    *,
    template_candidates: Tuple[FamilyTemplateRouteCandidate, ...] = (),
    enable_validated_jit: bool = False,
    jit_request: _jit.JitCompileRequest | None = None,
    jit_proposer: _jit.JitProposer | None = None,
) -> RoutedContract:
    """Select Registry -> Template -> Validated JIT, stopping at the first hit."""

    key = _selection_key(selection_key)
    if not isinstance(registry, FrozenContractRegistry):
        audit = _audit(
            key,
            [
                ContractSelectionAttempt(
                    ContractSourceType.REGISTRY,
                    ContractSelectionDecision.REJECTED,
                    "frozen-registry",
                    "invalid",
                )
            ],
        )
        raise ContractRouterError(
            ContractRouterFailureCode.INVALID_REQUEST,
            "registry must be a FrozenContractRegistry",
            audit,
        )
    attempts = []
    registry_attempt = (
        registry.provenance.registry_id,
        registry.provenance.revision,
    )
    try:
        frozen = registry.get(key)
    except FrozenRegistryError as exc:
        if exc.code is not FrozenRegistryFailureCode.MISSING_KEY:
            attempts.append(
                ContractSelectionAttempt(
                    ContractSourceType.REGISTRY,
                    ContractSelectionDecision.REJECTED,
                    *registry_attempt,
                )
            )
            raise ContractRouterError(
                ContractRouterFailureCode.REGISTRY_REJECTED,
                f"frozen registry lookup rejected: {exc}",
                _audit(key, attempts),
            ) from exc
        attempts.append(
            ContractSelectionAttempt(
                ContractSourceType.REGISTRY,
                ContractSelectionDecision.MISS,
                *registry_attempt,
            )
        )
    else:
        attempts.append(
            ContractSelectionAttempt(
                ContractSourceType.REGISTRY,
                ContractSelectionDecision.SELECTED,
                frozen.provenance.registry_id,
                frozen.provenance.revision,
            )
        )
        return RoutedContract(
            contract=frozen.contract,
            contract_sha256=frozen.contract_sha256,
            audit=_audit(key, attempts),
            validation_funnel_version=frozen.validation_funnel_version,
        )

    if not isinstance(template_candidates, tuple):
        attempts.append(
            ContractSelectionAttempt(
                ContractSourceType.TEMPLATE,
                ContractSelectionDecision.REJECTED,
                "template-catalog",
                "invalid",
            )
        )
        raise ContractRouterError(
            ContractRouterFailureCode.INVALID_REQUEST,
            "template_candidates must be an immutable tuple",
            _audit(key, attempts),
        )
    try:
        for candidate in template_candidates:
            if not isinstance(candidate, FamilyTemplateRouteCandidate):
                raise ValueError("template catalog contains a non-candidate value")
            candidate.validate()
    except ValueError as exc:
        attempts.append(
            ContractSelectionAttempt(
                ContractSourceType.TEMPLATE,
                ContractSelectionDecision.REJECTED,
                "template-catalog",
                "invalid",
            )
        )
        raise ContractRouterError(
            ContractRouterFailureCode.TEMPLATE_REJECTED,
            f"template catalog rejected: {exc}",
            _audit(key, attempts),
        ) from exc
    matches = tuple(
        candidate for candidate in template_candidates if candidate.selection_key == key
    )
    if len(matches) > 1:
        attempts.append(
            ContractSelectionAttempt(
                ContractSourceType.TEMPLATE,
                ContractSelectionDecision.REJECTED,
                "template-catalog",
                "duplicate-route-key",
            )
        )
        raise ContractRouterError(
            ContractRouterFailureCode.TEMPLATE_REJECTED,
            f"duplicate template candidates for selection key: {key}",
            _audit(key, attempts),
        )
    if matches:
        candidate = matches[0]
        try:
            instantiated = _template.instantiate_family_template(
                candidate.template,
                candidate.parameter_mapping(),
                selection_key=key,
            )
        except FamilyTemplateError as exc:
            attempts.append(
                ContractSelectionAttempt(
                    ContractSourceType.TEMPLATE,
                    ContractSelectionDecision.REJECTED,
                    candidate.template.template_id,
                    candidate.template.version,
                )
            )
            raise ContractRouterError(
                ContractRouterFailureCode.TEMPLATE_REJECTED,
                f"matched family template failed compilation: {exc}",
                _audit(key, attempts),
            ) from exc
        attempts.append(
            ContractSelectionAttempt(
                ContractSourceType.TEMPLATE,
                ContractSelectionDecision.SELECTED,
                candidate.template.template_id,
                candidate.template.version,
            )
        )
        return RoutedContract(
            contract=instantiated.contract,
            contract_sha256=instantiated.contract_sha256,
            audit=_audit(key, attempts),
            validation_funnel_version=instantiated.validation_funnel_version,
        )
    attempts.append(
        ContractSelectionAttempt(
            ContractSourceType.TEMPLATE,
            ContractSelectionDecision.MISS,
            "template-catalog",
            CONTRACT_SOURCE_ROUTER_VERSION,
        )
    )

    if not isinstance(enable_validated_jit, bool):
        attempts.append(
            ContractSelectionAttempt(
                ContractSourceType.VALIDATED_JIT,
                ContractSelectionDecision.REJECTED,
                "validated-jit",
                "invalid-enable-flag",
            )
        )
        raise ContractRouterError(
            ContractRouterFailureCode.INVALID_REQUEST,
            "enable_validated_jit must be boolean",
            _audit(key, attempts),
        )
    if not enable_validated_jit:
        attempts.append(
            ContractSelectionAttempt(
                ContractSourceType.VALIDATED_JIT,
                ContractSelectionDecision.DISABLED,
                "validated-jit",
                _jit.JIT_COMPILER_VERSION,
            )
        )
        raise ContractRouterError(
            ContractRouterFailureCode.JIT_DISABLED,
            "no Registry or Template match and Validated JIT is disabled",
            _audit(key, attempts),
        )
    if jit_request is None and jit_proposer is None:
        attempts.append(
            ContractSelectionAttempt(
                ContractSourceType.VALIDATED_JIT,
                ContractSelectionDecision.REJECTED,
                "validated-jit",
                "missing-compiler-inputs",
            )
        )
        raise ContractRouterError(
            ContractRouterFailureCode.JIT_NOT_IMPLEMENTED,
            "Validated JIT requires an explicit task-only request and proposer",
            _audit(key, attempts),
        )
    if not isinstance(jit_request, _jit.JitCompileRequest) or jit_proposer is None:
        attempts.append(
            ContractSelectionAttempt(
                ContractSourceType.VALIDATED_JIT,
                ContractSelectionDecision.REJECTED,
                "validated-jit",
                "invalid-compiler-inputs",
            )
        )
        raise ContractRouterError(
            ContractRouterFailureCode.INVALID_REQUEST,
            "Validated JIT inputs are incomplete or invalid",
            _audit(key, attempts),
        )
    try:
        request_key = jit_request.selection_key
    except ValueError as exc:
        attempts.append(
            ContractSelectionAttempt(
                ContractSourceType.VALIDATED_JIT,
                ContractSelectionDecision.REJECTED,
                "validated-jit",
                "invalid-task-only-input",
            )
        )
        raise ContractRouterError(
            ContractRouterFailureCode.INVALID_REQUEST,
            "Validated JIT task-only input is invalid",
            _audit(key, attempts),
        ) from exc
    if key != request_key:
        attempts.append(
            ContractSelectionAttempt(
                ContractSourceType.VALIDATED_JIT,
                ContractSelectionDecision.REJECTED,
                "validated-jit",
                "selection-key-mismatch",
            )
        )
        raise ContractRouterError(
            ContractRouterFailureCode.INVALID_REQUEST,
            "Validated JIT selection key must derive from its task-only input",
            _audit(key, attempts),
        )
    proposer_id = getattr(jit_proposer, "proposer_id", "validated-jit")
    proposer_version = getattr(jit_proposer, "proposer_version", "invalid-proposer")
    if not isinstance(proposer_id, str) or not proposer_id.strip():
        proposer_id = "validated-jit"
    if not isinstance(proposer_version, str) or not proposer_version.strip():
        proposer_version = "invalid-proposer"
    try:
        compiled = _jit.compile_jit_contract(jit_request, jit_proposer)
    except _jit.JitCompilationError as exc:
        attempts.append(
            ContractSelectionAttempt(
                ContractSourceType.VALIDATED_JIT,
                ContractSelectionDecision.REJECTED,
                proposer_id,
                proposer_version,
            )
        )
        raise ContractRouterError(
            ContractRouterFailureCode.JIT_REJECTED,
            f"Validated JIT failed closed: {exc.code.value}",
            _audit(key, attempts),
        ) from exc
    attempts.append(
        ContractSelectionAttempt(
            ContractSourceType.VALIDATED_JIT,
            ContractSelectionDecision.SELECTED,
            compiled.proposer_id,
            compiled.proposer_version,
        )
    )
    return RoutedContract(
        contract=compiled.contract,
        contract_sha256=compiled.contract_sha256,
        audit=_audit(key, attempts),
        validation_funnel_version=compiled.validation_funnel_version,
    )


def route_explicit_legacy(adapted: AdaptedLegacyContract) -> RoutedContract:
    """Audit an explicit Legacy import; Legacy is never part of automatic fallback."""

    if not isinstance(adapted, AdaptedLegacyContract):
        audit = ContractSelectionAudit(
            selection_key="invalid-legacy-import",
            attempts=(
                ContractSelectionAttempt(
                    ContractSourceType.LEGACY,
                    ContractSelectionDecision.REJECTED,
                    "legacy-import",
                    "invalid",
                ),
            ),
        )
        raise ContractRouterError(
            ContractRouterFailureCode.INVALID_REQUEST,
            "adapted must be an AdaptedLegacyContract",
            audit,
        )
    provenance = adapted.contract.compiler_provenance
    if provenance is None or provenance.source_type is not ContractSourceType.LEGACY:
        raise ValueError("adapted legacy contract has invalid compiler provenance")
    attempt = ContractSelectionAttempt(
        ContractSourceType.LEGACY,
        ContractSelectionDecision.SELECTED,
        provenance.source_id,
        provenance.source_version,
    )
    return RoutedContract(
        contract=adapted.contract,
        contract_sha256=adapted.contract_sha256,
        audit=ContractSelectionAudit(provenance.selection_key, (attempt,)),
        validation_funnel_version=adapted.validation_funnel_version,
    )


__all__ = [
    "CONTRACT_SOURCE_ROUTER_VERSION",
    "ContractRouterError",
    "ContractRouterFailureCode",
    "ContractSelectionAttempt",
    "ContractSelectionAudit",
    "ContractSelectionDecision",
    "FamilyTemplateRouteCandidate",
    "RoutedContract",
    "contract_selection_audit_payload",
    "contract_selection_audit_sha256",
    "route_contract",
    "route_explicit_legacy",
]
