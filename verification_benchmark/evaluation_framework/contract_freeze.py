"""Create-once, task-only ContractIR freeze artifacts for Runner orchestration."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from . import contract_validation as _validation
from .contract_router import (
    ContractSelectionAttempt,
    ContractSelectionAudit,
    ContractSelectionDecision,
    RoutedContract,
    contract_selection_audit_payload,
)
from .event_log import contract_payload
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
from .phase5_intake import Phase5IntakeError, strict_json_bytes
from .task_spec import TaskSpec


CONTRACT_FREEZE_SCHEMA_VERSION = "mobiagent-contract-freeze-v1"


def _mapping(value: Any, context: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise Phase5IntakeError(f"{context} must be an object")
    return value


def _sequence(value: Any, context: str) -> list[Any]:
    if not isinstance(value, list):
        raise Phase5IntakeError(f"{context} must be an array")
    return value


def _contract_from_payload(value: Any) -> ContractIR:
    """Rehydrate the exact validated ContractIR stored before Runner execution."""

    payload = _mapping(value, "Contract freeze contract")
    try:
        criteria = tuple(
            CriterionIR(
                criterion_id=str(item["criterion_id"]),
                temporal_semantics=TemporalSemantics(item["temporal_semantics"]),
                required=item["required"],
                allow_obscured_persistence=item["allow_obscured_persistence"],
                required_capabilities=tuple(
                    EvidenceCapability(capability)
                    for capability in _sequence(
                        item["required_capabilities"],
                        "criterion required_capabilities",
                    )
                ),
                description=item["description"],
            )
            for raw in _sequence(payload["criteria"], "contract criteria")
            for item in (_mapping(raw, "contract criterion"),)
        )
        bindings = tuple(
            G1CriterionBindingIR(
                criterion_id=str(item["criterion_id"]),
                checker=G1CheckerKind(item["checker"]),
                rois=tuple(
                    ContractRoiIR(
                        roi_id=str(roi["roi_id"]),
                        bounds=tuple(float(number) for number in roi["bounds"]),
                        coordinate_space=RoiCoordinateSpace(
                            roi["coordinate_space"]
                        ),
                        reference_size=(
                            tuple(int(number) for number in roi["reference_size"])
                            if roi["reference_size"] is not None
                            else None
                        ),
                    )
                    for raw_roi in _sequence(item["rois"], "binding rois")
                    for roi in (_mapping(raw_roi, "binding ROI"),)
                ),
            )
            for raw in _sequence(payload.get("g1_bindings", []), "g1_bindings")
            for item in (_mapping(raw, "g1 binding"),)
        )
        raw_dag = payload.get("dag")
        dag = None
        if raw_dag is not None:
            dag_payload = _mapping(raw_dag, "contract DAG")
            nodes = tuple(
                ContractDagNodeIR(
                    node_id=str(node["node_id"]),
                    condition_operator=DagLogicalOperator(
                        node["condition_operator"]
                    ),
                    checker_ids=tuple(str(value) for value in node["checker_ids"]),
                    condition_sha256=str(node["condition_sha256"]),
                    checkers=tuple(
                        ContractCheckerIR(
                            checker_id=str(checker["checker_id"]),
                            parameters=_mapping(
                                checker["parameters"], "checker parameters"
                            ),
                        )
                        for raw_checker in _sequence(
                            node["checkers"], "DAG checkers"
                        )
                        for checker in (_mapping(raw_checker, "DAG checker"),)
                    ),
                    score=node["score"],
                )
                for raw_node in _sequence(dag_payload["nodes"], "DAG nodes")
                for node in (_mapping(raw_node, "DAG node"),)
            )
            edges = tuple(
                ContractDagEdgeIR(
                    parent_id=str(edge["parent_id"]),
                    child_id=str(edge["child_id"]),
                    kind=DagEdgeKind(edge["kind"]),
                )
                for raw_edge in _sequence(dag_payload["edges"], "DAG edges")
                for edge in (_mapping(raw_edge, "DAG edge"),)
            )
            success = _mapping(dag_payload["success"], "DAG success")
            dag = ContractDagIR(
                nodes=nodes,
                edges=edges,
                success=ContractDagSuccessIR(
                    operator=DagLogicalOperator(success["operator"]),
                    node_ids=tuple(str(value) for value in success["node_ids"]),
                ),
            )
        provenance_payload = _mapping(
            payload["compiler_provenance"], "contract compiler_provenance"
        )
        contract = ContractIR(
            contract_id=str(payload["contract_id"]),
            criteria=criteria,
            schema_version=str(payload["schema_version"]),
            source=str(payload["source"]),
            task_family=(
                str(payload["task_family"])
                if payload.get("task_family") is not None
                else None
            ),
            required_capabilities=tuple(
                EvidenceCapability(capability)
                for capability in _sequence(
                    payload["required_capabilities"], "required_capabilities"
                )
            ),
            g1_bindings=bindings,
            dag=dag,
            compiler_provenance=ContractProvenanceIR(
                source_type=ContractSourceType(provenance_payload["source_type"]),
                source_id=str(provenance_payload["source_id"]),
                source_version=str(provenance_payload["source_version"]),
                source_digest=str(provenance_payload["source_digest"]),
                source_locator=str(provenance_payload["source_locator"]),
                selection_key=str(provenance_payload["selection_key"]),
            ),
            metadata=_mapping(payload.get("metadata", {}), "contract metadata"),
        )
        contract.validate()
        return contract
    except (KeyError, TypeError, ValueError) as exc:
        raise Phase5IntakeError(f"invalid frozen ContractIR: {exc}") from exc


def _selection_audit_from_payload(value: Any) -> ContractSelectionAudit:
    payload = _mapping(value, "Contract freeze selection_audit")
    try:
        audit = ContractSelectionAudit(
            selection_key=str(payload["selection_key"]),
            router_version=str(payload["router_version"]),
            attempts=tuple(
                ContractSelectionAttempt(
                    source_type=ContractSourceType(item["source_type"]),
                    decision=ContractSelectionDecision(item["decision"]),
                    source_id=str(item["source_id"]),
                    source_version=str(item["source_version"]),
                )
                for raw in _sequence(payload["attempts"], "selection attempts")
                for item in (_mapping(raw, "selection attempt"),)
            ),
        )
        audit.validate(require_selected=True)
        return audit
    except (KeyError, TypeError, ValueError) as exc:
        raise Phase5IntakeError(f"invalid frozen Contract selection audit: {exc}") from exc


def task_spec_from_contract_freeze(path: Path) -> TaskSpec:
    payload = strict_json_bytes(path.resolve(strict=True).read_bytes(), context="Contract freeze")
    if payload.get("schema_version") != CONTRACT_FREEZE_SCHEMA_VERSION:
        raise Phase5IntakeError("unsupported Contract freeze schema")
    value = payload.get("task_spec")
    if not isinstance(value, Mapping):
        raise Phase5IntakeError("Contract freeze lacks task_spec")
    apps = value.get("target_apps")
    if not isinstance(apps, list):
        raise Phase5IntakeError("Contract freeze task_spec target_apps is invalid")
    task = TaskSpec(
        task_id=str(value.get("task_id") or ""),
        task_text=str(value.get("task_text") or ""),
        task_family=str(value.get("task_family") or ""),
        initial_app=str(value.get("initial_app") or ""),
        target_apps=tuple(str(item) for item in apps),
        risk_level=str(value.get("risk_level") or ""),
        parsed_intent=(
            _mapping(value.get("parsed_intent"), "Contract freeze task_spec parsed_intent")
            if value.get("parsed_intent") is not None
            else {}
        ),
        schema_version=str(value.get("schema_version") or ""),
    )
    task.validate()
    if payload.get("task_spec_sha256") != task.sha256:
        raise Phase5IntakeError("Contract freeze TaskSpec hash mismatch")
    return task


def routed_contract_from_freeze(
    path: Path, *, expected_task: TaskSpec | None = None
) -> RoutedContract:
    """Load the create-once pre-run Contract without recompiling after the trace."""

    source = path.resolve(strict=True)
    payload = strict_json_bytes(source.read_bytes(), context="Contract freeze")
    if payload.get("schema_version") != CONTRACT_FREEZE_SCHEMA_VERSION:
        raise Phase5IntakeError("unsupported Contract freeze schema")
    if payload.get("trace_evidence_consumed") is not False:
        raise Phase5IntakeError("Contract freeze is not trace-blind")
    frozen_task = task_spec_from_contract_freeze(source)
    if expected_task is not None and expected_task.sha256 != frozen_task.sha256:
        raise Phase5IntakeError("Contract freeze does not bind the requested TaskSpec")
    contract = _contract_from_payload(payload.get("contract"))
    expected_hash = payload.get("contract_sha256")
    if not isinstance(expected_hash, str):
        raise Phase5IntakeError("Contract freeze lacks contract_sha256")
    try:
        validated = _validation.validate_and_freeze_contract(
            contract,
            expected_source=contract.source,
            expected_contract_sha256=expected_hash,
        )
    except _validation.ContractValidationError as exc:
        raise Phase5IntakeError(f"frozen ContractIR validation failed: {exc}") from exc
    audit = _selection_audit_from_payload(payload.get("selection_audit"))
    if payload.get("selection_key") != audit.selection_key:
        raise Phase5IntakeError("Contract freeze selection key mismatch")
    if payload.get("validation_funnel_version") != validated.validation_funnel_version:
        raise Phase5IntakeError("Contract freeze validation funnel mismatch")
    try:
        return RoutedContract(
            contract=validated.contract,
            contract_sha256=validated.contract_sha256,
            audit=audit,
            validation_funnel_version=validated.validation_funnel_version,
        )
    except ValueError as exc:
        raise Phase5IntakeError(f"invalid routed Contract freeze: {exc}") from exc


def contract_freeze_payload(
    task: TaskSpec, routed: RoutedContract
) -> Mapping[str, Any]:
    task.validate()
    return {
        "schema_version": CONTRACT_FREEZE_SCHEMA_VERSION,
        "task_spec": task.payload(),
        "task_spec_sha256": task.sha256,
        "selection_key": routed.audit.selection_key,
        "contract_sha256": routed.contract_sha256,
        "contract": contract_payload(routed.contract),
        "selection_audit": contract_selection_audit_payload(routed.audit),
        "validation_funnel_version": routed.validation_funnel_version,
        "trace_evidence_consumed": False,
    }


def _bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def write_contract_freeze(
    path: Path, task: TaskSpec, routed: RoutedContract
) -> Mapping[str, Any]:
    payload = contract_freeze_payload(task, routed)
    rendered = _bytes(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as stream:
            stream.write(rendered)
    except FileExistsError:
        if path.read_bytes() != rendered:
            raise Phase5IntakeError(
                f"refusing to overwrite a different Contract freeze: {path}"
            )
    return payload


def validate_contract_freeze(
    path: Path, task: TaskSpec, routed: RoutedContract
) -> Mapping[str, Any]:
    payload = strict_json_bytes(path.resolve(strict=True).read_bytes(), context="Contract freeze")
    expected = contract_freeze_payload(task, routed)
    if payload != expected:
        raise Phase5IntakeError("frozen ContractIR artifact does not match routed task Contract")
    return {
        "path": str(path.resolve(strict=True)),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "contract_sha256": routed.contract_sha256,
        "task_spec_sha256": task.sha256,
    }


__all__ = [
    "CONTRACT_FREEZE_SCHEMA_VERSION",
    "contract_freeze_payload",
    "routed_contract_from_freeze",
    "task_spec_from_contract_freeze",
    "validate_contract_freeze",
    "write_contract_freeze",
]
