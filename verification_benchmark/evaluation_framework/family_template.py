"""Typed, model-free Family Template instantiation into validated ContractIR."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
from dataclasses import dataclass, field
from enum import Enum
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
)


FAMILY_TEMPLATE_SCHEMA_VERSION = "harmony-eval-family-template-v1"
FAMILY_TEMPLATE_SOURCE = "family-template"


class FamilyTemplateParameterKind(str, Enum):
    NORMALIZED_ROI = "NORMALIZED_ROI"


class FamilyTemplateFailureCode(str, Enum):
    INVALID_TEMPLATE = "INVALID_TEMPLATE"
    INVALID_PARAMETERS = "INVALID_PARAMETERS"
    INVALID_CONTRACT = "INVALID_CONTRACT"
    INVALID_EXPECTED_HASH = "INVALID_EXPECTED_HASH"
    HASH_MISMATCH = "HASH_MISMATCH"


class FamilyTemplateError(ValueError):
    def __init__(self, code: FamilyTemplateFailureCode, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class FamilyTemplateParameterIR:
    parameter_id: str
    kind: FamilyTemplateParameterKind

    def validate(self) -> None:
        if (
            not isinstance(self.parameter_id, str)
            or not self.parameter_id.strip()
            or self.parameter_id != self.parameter_id.strip()
        ):
            raise ValueError("template parameter_id must be non-empty")
        if not isinstance(self.kind, FamilyTemplateParameterKind):
            raise ValueError("template parameter kind is unsupported")


@dataclass(frozen=True)
class FamilyTemplateRoiIR:
    roi_id: str
    bounds_parameter_id: str

    def validate(self) -> None:
        if (
            not isinstance(self.roi_id, str)
            or not self.roi_id.strip()
            or self.roi_id != self.roi_id.strip()
        ):
            raise ValueError("template roi_id must be non-empty")
        if (
            not isinstance(self.bounds_parameter_id, str)
            or not self.bounds_parameter_id.strip()
            or self.bounds_parameter_id != self.bounds_parameter_id.strip()
        ):
            raise ValueError("template bounds_parameter_id must be non-empty")


@dataclass(frozen=True)
class FamilyTemplateG1BindingIR:
    criterion_id: str
    checker: G1CheckerKind
    rois: Tuple[FamilyTemplateRoiIR, ...]

    def validate(self) -> None:
        if (
            not isinstance(self.criterion_id, str)
            or not self.criterion_id.strip()
            or self.criterion_id != self.criterion_id.strip()
        ):
            raise ValueError("template binding criterion_id must be non-empty")
        if not isinstance(self.checker, G1CheckerKind):
            raise ValueError("template binding checker is unsupported")
        if not isinstance(self.rois, tuple) or not self.rois:
            raise ValueError("template binding must contain at least one ROI")
        if any(not isinstance(roi, FamilyTemplateRoiIR) for roi in self.rois):
            raise ValueError("template binding rois must contain FamilyTemplateRoiIR values")
        roi_ids = [roi.roi_id for roi in self.rois]
        if len(roi_ids) != len(set(roi_ids)):
            raise ValueError("template binding ROI ids must be unique")
        for roi in self.rois:
            roi.validate()


@dataclass(frozen=True)
class FamilyTemplateIR:
    template_id: str
    version: str
    task_family: str
    parameters: Tuple[FamilyTemplateParameterIR, ...]
    criteria: Tuple[CriterionIR, ...]
    required_capabilities: Tuple[EvidenceCapability, ...]
    g1_bindings: Tuple[FamilyTemplateG1BindingIR, ...]
    metadata: Mapping[str, Any] = field(default_factory=dict)
    schema_version: str = FAMILY_TEMPLATE_SCHEMA_VERSION
    source: str = FAMILY_TEMPLATE_SOURCE

    def validate(self) -> None:
        for name, value in (
            ("template_id", self.template_id),
            ("version", self.version),
            ("task_family", self.task_family),
        ):
            if (
                not isinstance(value, str)
                or not value.strip()
                or value != value.strip()
            ):
                raise ValueError(f"template {name} must be non-empty")
        if self.schema_version != FAMILY_TEMPLATE_SCHEMA_VERSION:
            raise ValueError(f"unsupported family template schema: {self.schema_version}")
        if self.source != FAMILY_TEMPLATE_SOURCE:
            raise ValueError(f"template source must be {FAMILY_TEMPLATE_SOURCE!r}")
        if not isinstance(self.parameters, tuple) or not self.parameters:
            raise ValueError("family template must declare at least one typed parameter")
        if any(not isinstance(item, FamilyTemplateParameterIR) for item in self.parameters):
            raise ValueError("template parameters must contain FamilyTemplateParameterIR values")
        parameter_ids = [item.parameter_id for item in self.parameters]
        if len(parameter_ids) != len(set(parameter_ids)):
            raise ValueError("template parameter ids must be unique")
        for parameter in self.parameters:
            parameter.validate()
        if not isinstance(self.criteria, tuple) or not self.criteria:
            raise ValueError("family template must contain criteria")
        if any(not isinstance(item, CriterionIR) for item in self.criteria):
            raise ValueError("template criteria must contain CriterionIR values")
        criterion_ids = [item.criterion_id for item in self.criteria]
        if len(criterion_ids) != len(set(criterion_ids)):
            raise ValueError("template criterion ids must be unique")
        for criterion in self.criteria:
            criterion.validate()
            if criterion.criterion_id != criterion.criterion_id.strip():
                raise ValueError("template criterion_id values must be canonical strings")
        if not isinstance(self.required_capabilities, tuple) or any(
            not isinstance(item, EvidenceCapability) for item in self.required_capabilities
        ):
            raise ValueError("template required_capabilities are invalid")
        if len(self.required_capabilities) != len(set(self.required_capabilities)):
            raise ValueError("template required_capabilities must be unique")
        if not isinstance(self.g1_bindings, tuple) or not self.g1_bindings:
            raise ValueError("family template must contain G1 bindings")
        if any(not isinstance(item, FamilyTemplateG1BindingIR) for item in self.g1_bindings):
            raise ValueError("template bindings must contain FamilyTemplateG1BindingIR values")
        binding_ids = [item.criterion_id for item in self.g1_bindings]
        if len(binding_ids) != len(set(binding_ids)):
            raise ValueError("a criterion may have at most one template G1 binding")
        unknown_criteria = sorted(set(binding_ids) - set(criterion_ids))
        if unknown_criteria:
            raise ValueError(f"template bindings reference unknown criteria: {unknown_criteria}")
        for binding in self.g1_bindings:
            binding.validate()
        if not isinstance(self.metadata, Mapping):
            raise ValueError("template metadata must be a JSON object")
        try:
            json.dumps(
                self.metadata,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("template metadata must be finite JSON data") from exc
        used_parameters = {
            roi.bounds_parameter_id for binding in self.g1_bindings for roi in binding.rois
        }
        unknown_parameters = sorted(used_parameters - set(parameter_ids))
        if unknown_parameters:
            raise ValueError(
                f"template ROI bindings reference unknown parameters: {unknown_parameters}"
            )
        unused_parameters = sorted(set(parameter_ids) - used_parameters)
        if unused_parameters:
            raise ValueError(
                f"template parameters must affect executable bindings; unused={unused_parameters}"
            )


@dataclass(frozen=True)
class FamilyTemplateParameterValue:
    parameter_id: str
    kind: FamilyTemplateParameterKind
    value: Tuple[float, float, float, float]


@dataclass(frozen=True)
class FamilyTemplateProvenance:
    template_id: str
    version: str
    task_family: str
    template_sha256: str
    instance_sha256: str
    parameters: Tuple[FamilyTemplateParameterValue, ...]
    source: str = FAMILY_TEMPLATE_SOURCE


@dataclass(frozen=True)
class InstantiatedFamilyContract:
    contract: ContractIR
    contract_sha256: str
    provenance: FamilyTemplateProvenance
    validation_funnel_version: str

    def __post_init__(self) -> None:
        actual = contract_sha256(self.contract)
        if not hmac.compare_digest(actual, self.contract_sha256):
            raise ValueError("family contract hash does not match ContractIR")
        if self.validation_funnel_version != _validation.CONTRACT_VALIDATION_FUNNEL_VERSION:
            raise ValueError("family contract did not pass the supported validation funnel")
        if self.contract.source != self.provenance.source:
            raise ValueError("family contract source does not match provenance")
        if self.contract.task_family != self.provenance.task_family:
            raise ValueError("family contract task_family does not match provenance")
        compiler_provenance = self.contract.compiler_provenance
        if compiler_provenance is None:
            raise ValueError("family contract is missing compiler provenance")
        if (
            compiler_provenance.source_type is not ContractSourceType.TEMPLATE
            or compiler_provenance.source_id != self.provenance.template_id
            or compiler_provenance.source_version != self.provenance.version
            or compiler_provenance.source_digest != self.provenance.template_sha256
        ):
            raise ValueError("family contract compiler provenance does not match template")
        expected_id = (
            f"{self.provenance.template_id}@{self.provenance.version}:"
            f"{self.provenance.instance_sha256}"
        )
        if self.contract.contract_id != expected_id:
            raise ValueError("family contract id does not bind template provenance")


def _normalized_roi(value: Any, context: str) -> Tuple[float, float, float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise FamilyTemplateError(
            FamilyTemplateFailureCode.INVALID_PARAMETERS,
            f"{context} must contain four normalized numbers",
        )
    normalized = []
    for index, item in enumerate(value):
        if (
            not isinstance(item, (int, float))
            or isinstance(item, bool)
            or not math.isfinite(item)
        ):
            raise FamilyTemplateError(
                FamilyTemplateFailureCode.INVALID_PARAMETERS,
                f"{context}[{index}] must be a finite number",
            )
        normalized.append(float(item))
    x1, y1, x2, y2 = normalized
    if min(normalized) < 0 or max(normalized) > 1 or x2 <= x1 or y2 <= y1:
        raise FamilyTemplateError(
            FamilyTemplateFailureCode.INVALID_PARAMETERS,
            f"{context} must be a positive rectangle within [0, 1]",
        )
    return x1, y1, x2, y2


def _template_payload(template: FamilyTemplateIR) -> dict[str, Any]:
    return {
        "schema_version": template.schema_version,
        "source": template.source,
        "template_id": template.template_id,
        "version": template.version,
        "task_family": template.task_family,
        "parameter_specs": [
            {"parameter_id": item.parameter_id, "kind": item.kind.value}
            for item in template.parameters
        ],
        "criteria": [
            {
                "criterion_id": item.criterion_id,
                "temporal_semantics": item.temporal_semantics.value,
                "required": item.required,
                "allow_obscured_persistence": item.allow_obscured_persistence,
                "required_capabilities": [
                    capability.value for capability in item.required_capabilities
                ],
                "description": item.description,
            }
            for item in template.criteria
        ],
        "required_capabilities": [
            capability.value for capability in template.required_capabilities
        ],
        "g1_bindings": [
            {
                "criterion_id": binding.criterion_id,
                "checker": binding.checker.value,
                "rois": [
                    {
                        "roi_id": roi.roi_id,
                        "bounds_parameter_id": roi.bounds_parameter_id,
                    }
                    for roi in binding.rois
                ],
            }
            for binding in template.g1_bindings
        ],
        "metadata": dict(template.metadata),
    }


def _instance_payload(
    template_sha256: str,
    parameters: Tuple[FamilyTemplateParameterValue, ...],
) -> dict[str, Any]:
    return {
        "template_sha256": template_sha256,
        "parameters": [
            {
                "parameter_id": item.parameter_id,
                "kind": item.kind.value,
                "value": list(item.value),
            }
            for item in parameters
        ],
    }


def _payload_sha256(payload: Mapping[str, Any]) -> str:
    rendered = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(rendered).hexdigest()


def instantiate_family_template(
    template: FamilyTemplateIR,
    parameter_values: Mapping[str, Any],
    *,
    selection_key: Optional[str] = None,
    expected_contract_sha256: Optional[str] = None,
) -> InstantiatedFamilyContract:
    """Instantiate typed parameters, then unconditionally enter the shared final funnel."""

    if not isinstance(template, FamilyTemplateIR):
        raise FamilyTemplateError(
            FamilyTemplateFailureCode.INVALID_TEMPLATE,
            "template must be a FamilyTemplateIR",
        )
    try:
        template.validate()
    except ValueError as exc:
        raise FamilyTemplateError(
            FamilyTemplateFailureCode.INVALID_TEMPLATE,
            f"invalid family template: {exc}",
        ) from exc
    if not isinstance(parameter_values, Mapping):
        raise FamilyTemplateError(
            FamilyTemplateFailureCode.INVALID_PARAMETERS,
            "template parameter values must be an object",
        )
    route_key = template.template_id if selection_key is None else selection_key
    if (
        not isinstance(route_key, str)
        or not route_key.strip()
        or route_key != route_key.strip()
    ):
        raise FamilyTemplateError(
            FamilyTemplateFailureCode.INVALID_PARAMETERS,
            "selection_key must be a canonical non-empty string",
        )
    if any(not isinstance(key, str) for key in parameter_values):
        raise FamilyTemplateError(
            FamilyTemplateFailureCode.INVALID_PARAMETERS,
            "template parameter keys must be strings",
        )
    expected_ids = {item.parameter_id for item in template.parameters}
    actual_ids = set(parameter_values)
    if actual_ids != expected_ids:
        raise FamilyTemplateError(
            FamilyTemplateFailureCode.INVALID_PARAMETERS,
            f"template parameter keys mismatch; missing={sorted(expected_ids - actual_ids)}, "
            f"unexpected={sorted(actual_ids - expected_ids)}",
        )

    normalized_values = []
    values_by_id = {}
    for parameter in sorted(template.parameters, key=lambda item: item.parameter_id):
        if parameter.kind is not FamilyTemplateParameterKind.NORMALIZED_ROI:
            raise FamilyTemplateError(
                FamilyTemplateFailureCode.INVALID_TEMPLATE,
                f"unsupported template parameter kind: {parameter.kind}",
            )
        value = _normalized_roi(
            parameter_values[parameter.parameter_id],
            f"parameter {parameter.parameter_id!r}",
        )
        values_by_id[parameter.parameter_id] = value
        normalized_values.append(
            FamilyTemplateParameterValue(parameter.parameter_id, parameter.kind, value)
        )
    frozen_values = tuple(normalized_values)
    template_sha256 = _payload_sha256(_template_payload(template))
    instance_sha256 = _payload_sha256(_instance_payload(template_sha256, frozen_values))
    contract_id = f"{template.template_id}@{template.version}:{instance_sha256}"
    bindings = tuple(
        G1CriterionBindingIR(
            criterion_id=binding.criterion_id,
            checker=binding.checker,
            rois=tuple(
                ContractRoiIR(
                    roi_id=roi.roi_id,
                    bounds=values_by_id[roi.bounds_parameter_id],
                    coordinate_space=RoiCoordinateSpace.NORMALIZED,
                )
                for roi in binding.rois
            ),
        )
        for binding in template.g1_bindings
    )
    contract = ContractIR(
        contract_id=contract_id,
        criteria=template.criteria,
        source=FAMILY_TEMPLATE_SOURCE,
        compiler_provenance=ContractProvenanceIR(
            source_type=ContractSourceType.TEMPLATE,
            source_id=template.template_id,
            source_version=template.version,
            source_digest=template_sha256,
            source_locator=template.template_id,
            selection_key=route_key,
        ),
        task_family=template.task_family,
        required_capabilities=template.required_capabilities,
        g1_bindings=bindings,
        metadata=template.metadata,
    )
    try:
        validated = _validation.validate_and_freeze_contract(
            contract,
            expected_source=FAMILY_TEMPLATE_SOURCE,
            expected_contract_sha256=expected_contract_sha256,
        )
    except _validation.ContractValidationError as exc:
        if exc.code is _validation.ContractValidationFailureCode.HASH_MISMATCH:
            code = FamilyTemplateFailureCode.HASH_MISMATCH
        elif exc.code is _validation.ContractValidationFailureCode.INVALID_EXPECTED_HASH:
            code = FamilyTemplateFailureCode.INVALID_EXPECTED_HASH
        else:
            code = FamilyTemplateFailureCode.INVALID_CONTRACT
        raise FamilyTemplateError(code, str(exc)) from exc
    provenance = FamilyTemplateProvenance(
        template_id=template.template_id,
        version=template.version,
        task_family=template.task_family,
        template_sha256=template_sha256,
        instance_sha256=instance_sha256,
        parameters=frozen_values,
    )
    return InstantiatedFamilyContract(
        contract=validated.contract,
        contract_sha256=validated.contract_sha256,
        provenance=provenance,
        validation_funnel_version=validated.validation_funnel_version,
    )
