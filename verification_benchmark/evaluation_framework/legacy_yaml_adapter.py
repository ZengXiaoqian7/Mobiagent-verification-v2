"""Read-only, fail-closed conversion of legacy avdag YAML into ContractIR."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import warnings
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Optional, Tuple

import yaml

from . import contract_validation as _validation
from .event_log import contract_sha256
from .legacy_checker_lowering import LEGACY_CHECKER_LOWERING_VERSION
from .models import (
    ContractDagEdgeIR,
    ContractDagIR,
    ContractDagNodeIR,
    ContractDagSuccessIR,
    ContractCheckerIR,
    ContractIR,
    ContractProvenanceIR,
    CriterionIR,
    ContractSourceType,
    DagDependencyMode,
    DagEdgeKind,
    DagLogicalOperator,
    TemporalSemantics,
)


LEGACY_YAML_ADAPTER_VERSION = "harmony-eval-legacy-yaml-adapter-v1"
LEGACY_YAML_SOURCE = "legacy-yaml-adapter"
LEGACY_PIPELINE_VERSION = (
    f"{LEGACY_YAML_ADAPTER_VERSION}+{LEGACY_CHECKER_LOWERING_VERSION}"
)

_TOP_LEVEL_KEYS = {"task_id", "app_id", "task_type", "description", "nodes", "success"}
_NODE_REQUIRED_KEYS = {"id", "condition"}
_NODE_OPTIONAL_KEYS = {"name", "deps", "next", "score"}
_CONDITION_KEYS = {"type", "params"}
_ESCALATE_CHECKERS = (
    "text",
    "regex",
    "ui",
    "action",
    "dynamic_match",
    "icons",
    "ocr",
    "llm",
)
_JUXTAPOSITION_CHECKERS = (
    "text",
    "regex",
    "ui",
    "action",
    "visual_state",
    "xml",
    "dynamic_match",
    "icons",
    "ocr",
    "llm",
)
_PARAMETER_MODIFIERS = {"llm_optional"}


class LegacyYamlAdapterFailureCode(str, Enum):
    SOURCE_UNREADABLE = "SOURCE_UNREADABLE"
    SOURCE_CHANGED = "SOURCE_CHANGED"
    INVALID_SOURCE_REF = "INVALID_SOURCE_REF"
    INVALID_YAML = "INVALID_YAML"
    INVALID_SCHEMA = "INVALID_SCHEMA"
    UNSUPPORTED_CONDITION = "UNSUPPORTED_CONDITION"
    UNSUPPORTED_CHECKER = "UNSUPPORTED_CHECKER"
    INVALID_TOPOLOGY = "INVALID_TOPOLOGY"
    TOPOLOGY_MISMATCH = "TOPOLOGY_MISMATCH"
    INVALID_CONTRACT = "INVALID_CONTRACT"
    INVALID_EXPECTED_HASH = "INVALID_EXPECTED_HASH"
    HASH_MISMATCH = "HASH_MISMATCH"


class LegacyYamlAdapterError(ValueError):
    def __init__(self, code: LegacyYamlAdapterFailureCode, message: str) -> None:
        self.code = code
        super().__init__(message)


class _UniqueKeySafeLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(
    loader: _UniqueKeySafeLoader,
    node: yaml.MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in result
        except TypeError as exc:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found an unhashable mapping key",
                key_node.start_mark,
            ) from exc
        if duplicate:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueKeySafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


@dataclass(frozen=True)
class LegacyNodeDependency:
    node_id: str
    mode: DagDependencyMode
    parent_ids: Tuple[str, ...]


@dataclass(frozen=True)
class LegacyNodeCondition:
    node_id: str
    operator: DagLogicalOperator
    checker_ids: Tuple[str, ...]


@dataclass(frozen=True)
class LegacyTopologySnapshot:
    node_ids: Tuple[str, ...]
    deps_edges: Tuple[Tuple[str, str], ...]
    next_edges: Tuple[Tuple[str, str], ...]
    effective_dependencies: Tuple[LegacyNodeDependency, ...]
    node_conditions: Tuple[LegacyNodeCondition, ...]
    topological_order: Tuple[str, ...]
    sinks: Tuple[str, ...]
    success_operator: DagLogicalOperator
    success_node_ids: Tuple[str, ...]


@dataclass(frozen=True)
class LegacyYamlProvenance:
    source_ref: str
    source_sha256: str
    semantic_sha256: str
    task_id: str
    app_id: str
    task_type: str
    source: str = LEGACY_YAML_SOURCE
    adapter_version: str = LEGACY_YAML_ADAPTER_VERSION
    lowering_version: str = LEGACY_CHECKER_LOWERING_VERSION


@dataclass(frozen=True)
class AdaptedLegacyContract:
    contract: ContractIR
    contract_sha256: str
    provenance: LegacyYamlProvenance
    topology: LegacyTopologySnapshot
    validation_funnel_version: str

    def __post_init__(self) -> None:
        if not hmac.compare_digest(contract_sha256(self.contract), self.contract_sha256):
            raise ValueError("adapted legacy contract hash does not match ContractIR")
        if self.validation_funnel_version != _validation.CONTRACT_VALIDATION_FUNNEL_VERSION:
            raise ValueError("legacy contract did not pass the supported validation funnel")
        if self.contract.source != self.provenance.source:
            raise ValueError("legacy contract source does not match provenance")
        if (
            self.provenance.adapter_version != LEGACY_YAML_ADAPTER_VERSION
            or self.provenance.lowering_version != LEGACY_CHECKER_LOWERING_VERSION
        ):
            raise ValueError("legacy provenance pipeline version is unsupported")
        compiler_provenance = self.contract.compiler_provenance
        if compiler_provenance is None:
            raise ValueError("legacy contract is missing compiler provenance")
        if (
            compiler_provenance.source_type is not ContractSourceType.LEGACY
            or compiler_provenance.source_id != self.provenance.task_id
            or compiler_provenance.source_version != LEGACY_PIPELINE_VERSION
            or compiler_provenance.source_digest != self.provenance.semantic_sha256
        ):
            raise ValueError("legacy contract compiler provenance does not match source")
        expected_id = (
            f"legacy:{self.provenance.task_id}:{self.provenance.semantic_sha256}"
        )
        if self.contract.contract_id != expected_id:
            raise ValueError("legacy contract id does not bind semantic provenance")
        if contract_dag_topology(self.contract.dag) != self.topology:
            raise ValueError("adapted legacy contract topology does not match provenance")


def _canonical_sha256(value: Mapping[str, Any]) -> str:
    rendered = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(rendered).hexdigest()


def _schema_error(message: str) -> LegacyYamlAdapterError:
    return LegacyYamlAdapterError(LegacyYamlAdapterFailureCode.INVALID_SCHEMA, message)


def _canonical_string(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise _schema_error(f"{context} must be a canonical non-empty string")
    return value


def _expect_exact_keys(value: Mapping[str, Any], expected: set[str], context: str) -> None:
    actual = set(value)
    if actual != expected:
        raise _schema_error(
            f"{context} keys mismatch; missing={sorted(expected - actual)}, "
            f"unexpected={sorted(actual - expected)}"
        )


def _validate_json_value(value: Any, context: str) -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise _schema_error(f"{context} contains a non-finite number")
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            _validate_json_value(child, f"{context}[{index}]")
        return
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                raise _schema_error(f"{context} object keys must be strings")
            _validate_json_value(child, f"{context}.{key}")
        return
    raise _schema_error(f"{context} must contain only JSON-compatible values")


def _string_list(value: Any, context: str) -> Tuple[str, ...]:
    if not isinstance(value, list):
        raise _schema_error(f"{context} must be a list")
    result = tuple(_canonical_string(item, f"{context} item") for item in value)
    if len(result) != len(set(result)):
        raise _schema_error(f"{context} must not contain duplicates")
    return result


def _validate_condition(value: Any, context: str) -> None:
    if not isinstance(value, Mapping):
        raise _schema_error(f"{context} must be an object")
    _expect_exact_keys(value, _CONDITION_KEYS, context)
    condition_type = _canonical_string(value["type"], f"{context}.type")
    if condition_type == "escalate":
        supported = _ESCALATE_CHECKERS
    elif condition_type == "juxtaposition":
        supported = _JUXTAPOSITION_CHECKERS
    else:
        raise LegacyYamlAdapterError(
            LegacyYamlAdapterFailureCode.UNSUPPORTED_CONDITION,
            f"{context} has unsupported condition type {condition_type!r}",
        )
    params = value["params"]
    if not isinstance(params, Mapping):
        raise _schema_error(f"{context}.params must be an object")
    _validate_json_value(params, f"{context}.params")
    unexpected = set(params) - set(supported) - _PARAMETER_MODIFIERS
    if unexpected:
        raise LegacyYamlAdapterError(
            LegacyYamlAdapterFailureCode.UNSUPPORTED_CHECKER,
            f"{context} has unsupported checker parameters: {sorted(unexpected)}",
        )
    checker_ids = tuple(name for name in supported if params.get(name) is not None)
    if not checker_ids:
        raise _schema_error(f"{context} must configure at least one executable checker")
    for checker_id in checker_ids:
        if not isinstance(params[checker_id], Mapping):
            raise _schema_error(
                f"{context}.params.{checker_id} must be an object"
            )
    if "llm_optional" in params:
        if condition_type != "juxtaposition" or params["llm_optional"] is not True:
            raise _schema_error(
                f"{context}.params.llm_optional is only supported as true for juxtaposition"
            )
        if "llm" not in checker_ids:
            raise _schema_error(f"{context}.params.llm_optional requires an llm checker")


def _validate_legacy_payload(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _schema_error("legacy YAML root must be an object")
    if any(not isinstance(key, str) for key in value):
        raise _schema_error("legacy YAML root keys must be strings")
    _expect_exact_keys(value, _TOP_LEVEL_KEYS, "legacy YAML root")
    for key in ("task_id", "app_id", "task_type", "description"):
        _canonical_string(value[key], key)
    nodes = value["nodes"]
    if not isinstance(nodes, list) or not nodes:
        raise _schema_error("nodes must be a non-empty list")
    node_ids = []
    for index, node in enumerate(nodes):
        context = f"nodes[{index}]"
        if not isinstance(node, Mapping):
            raise _schema_error(f"{context} must be an object")
        actual = set(node)
        allowed = _NODE_REQUIRED_KEYS | _NODE_OPTIONAL_KEYS
        if not _NODE_REQUIRED_KEYS.issubset(actual) or not actual.issubset(allowed):
            raise _schema_error(
                f"{context} keys invalid; missing={sorted(_NODE_REQUIRED_KEYS - actual)}, "
                f"unexpected={sorted(actual - allowed)}"
            )
        node_ids.append(_canonical_string(node["id"], f"{context}.id"))
        if "name" in node:
            _canonical_string(node["name"], f"{context}.name")
        for key in ("deps", "next"):
            if key in node:
                _string_list(node[key], f"{context}.{key}")
        if "score" in node and (
            not isinstance(node["score"], int)
            or isinstance(node["score"], bool)
            or node["score"] < 0
        ):
            raise _schema_error(f"{context}.score must be a non-negative integer")
        _validate_condition(node["condition"], f"{context}.condition")
    if len(node_ids) != len(set(node_ids)):
        raise _schema_error("node id values must be unique")
    success = value["success"]
    if not isinstance(success, Mapping):
        raise _schema_error("success must be an object")
    if set(success) not in ({"any_of"}, {"all_of"}):
        raise _schema_error("success must declare exactly one of any_of or all_of")
    success_key = next(iter(success))
    success_nodes = _string_list(success[success_key], f"success.{success_key}")
    if not success_nodes:
        raise _schema_error(f"success.{success_key} must be non-empty")
    _validate_json_value(value, "legacy YAML")
    return value


def _condition_parts(condition: Any) -> Tuple[DagLogicalOperator, Tuple[str, ...], str]:
    operator = (
        DagLogicalOperator.ANY_OF
        if condition.type == "escalate"
        else DagLogicalOperator.ALL_OF
    )
    supported = (
        _ESCALATE_CHECKERS if condition.type == "escalate" else _JUXTAPOSITION_CHECKERS
    )
    checker_ids = tuple(name for name in supported if condition.params.get(name) is not None)
    if (
        condition.type == "juxtaposition"
        and condition.params.get("llm_optional") is True
    ):
        checker_ids = tuple(name for name in checker_ids if name != "llm")
    digest = _canonical_sha256({"type": condition.type, "params": condition.params})
    return operator, checker_ids, digest


def _contract_dag_from_legacy_task(task: Any) -> ContractDagIR:
    nodes = []
    edges = []
    for node in task.nodes:
        operator, checker_ids, condition_digest = _condition_parts(node.condition)
        nodes.append(
            ContractDagNodeIR(
                node_id=node.id,
                condition_operator=operator,
                checker_ids=checker_ids,
                condition_sha256=condition_digest,
                score=node.score,
                checkers=tuple(
                    ContractCheckerIR(checker_id, node.condition.params[checker_id])
                    for checker_id in checker_ids
                ),
            )
        )
        for parent_id in node.deps or ():
            edges.append(ContractDagEdgeIR(parent_id, node.id, DagEdgeKind.DEPS_AND))
        for child_id in node.next or ():
            edges.append(ContractDagEdgeIR(node.id, child_id, DagEdgeKind.NEXT_OR))
    if task.success.any_of is not None:
        success = ContractDagSuccessIR(
            DagLogicalOperator.ANY_OF, tuple(task.success.any_of)
        )
    else:
        success = ContractDagSuccessIR(
            DagLogicalOperator.ALL_OF, tuple(task.success.all_of)
        )
    return ContractDagIR(tuple(nodes), tuple(edges), success)


def legacy_avdag_topology(task: Any, dag: Any) -> LegacyTopologySnapshot:
    dependencies = []
    conditions = []
    for node_id in dag.nodes:
        deps = tuple(dag.parents_from_deps.get(node_id, ()))
        next_parents = tuple(dag.parents_from_next.get(node_id, ()))
        if deps:
            mode, parents = DagDependencyMode.ALL_OF, deps
        elif next_parents:
            mode, parents = DagDependencyMode.ANY_OF, next_parents
        else:
            mode, parents = DagDependencyMode.ROOT, ()
        dependencies.append(LegacyNodeDependency(node_id, mode, parents))
        operator, checker_ids, _ = _condition_parts(dag.nodes[node_id].condition)
        conditions.append(LegacyNodeCondition(node_id, operator, checker_ids))
    success_operator = (
        DagLogicalOperator.ANY_OF
        if task.success.any_of is not None
        else DagLogicalOperator.ALL_OF
    )
    success_nodes = (
        tuple(task.success.any_of)
        if task.success.any_of is not None
        else tuple(task.success.all_of)
    )
    return LegacyTopologySnapshot(
        node_ids=tuple(dag.nodes),
        deps_edges=tuple(
            (parent_id, node.id)
            for node in task.nodes
            for parent_id in (node.deps or ())
        ),
        next_edges=tuple(
            (node.id, child_id)
            for node in task.nodes
            for child_id in (node.next or ())
        ),
        effective_dependencies=tuple(dependencies),
        node_conditions=tuple(conditions),
        topological_order=tuple(dag.topo_order()),
        sinks=tuple(dag.sinks()),
        success_operator=success_operator,
        success_node_ids=success_nodes,
    )


def contract_dag_topology(dag: Optional[ContractDagIR]) -> LegacyTopologySnapshot:
    if not isinstance(dag, ContractDagIR):
        raise ValueError("legacy ContractIR must contain a typed DAG")
    dag.validate()
    return LegacyTopologySnapshot(
        node_ids=tuple(node.node_id for node in dag.nodes),
        deps_edges=tuple(
            (edge.parent_id, edge.child_id)
            for edge in dag.edges
            if edge.kind is DagEdgeKind.DEPS_AND
        ),
        next_edges=tuple(
            (edge.parent_id, edge.child_id)
            for edge in dag.edges
            if edge.kind is DagEdgeKind.NEXT_OR
        ),
        effective_dependencies=tuple(
            LegacyNodeDependency(node.node_id, *dag.effective_dependency(node.node_id))
            for node in dag.nodes
        ),
        node_conditions=tuple(
            LegacyNodeCondition(node.node_id, node.condition_operator, node.checker_ids)
            for node in dag.nodes
        ),
        topological_order=dag.topological_order(),
        sinks=dag.sinks(),
        success_operator=dag.success.operator,
        success_node_ids=dag.success.node_ids,
    )


def assert_topology_equivalent(
    legacy: LegacyTopologySnapshot,
    converted: LegacyTopologySnapshot,
) -> None:
    if legacy != converted:
        raise LegacyYamlAdapterError(
            LegacyYamlAdapterFailureCode.TOPOLOGY_MISMATCH,
            "converted ContractIR DAG is not topologically equivalent to legacy avdag",
        )


def _source_ref(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\\" in value:
        raise LegacyYamlAdapterError(
            LegacyYamlAdapterFailureCode.INVALID_SOURCE_REF,
            "source_ref must be a non-empty POSIX relative reference",
        )
    ref = PurePosixPath(value)
    if ref.is_absolute() or ".." in ref.parts:
        raise LegacyYamlAdapterError(
            LegacyYamlAdapterFailureCode.INVALID_SOURCE_REF,
            "source_ref must not escape its logical root",
        )
    return value


def _read_source(path: Path) -> bytes:
    if path.suffix.lower() not in {".yaml", ".yml"}:
        raise LegacyYamlAdapterError(
            LegacyYamlAdapterFailureCode.SOURCE_UNREADABLE,
            "legacy source must be a .yaml or .yml file",
        )
    try:
        return path.read_bytes()
    except OSError as exc:
        raise LegacyYamlAdapterError(
            LegacyYamlAdapterFailureCode.SOURCE_UNREADABLE,
            f"legacy YAML source is unreadable: {exc}",
        ) from exc


def _assert_source_unchanged(path: Path, initial: bytes) -> None:
    current = _read_source(path)
    if not hmac.compare_digest(hashlib.sha256(initial).digest(), hashlib.sha256(current).digest()):
        raise LegacyYamlAdapterError(
            LegacyYamlAdapterFailureCode.SOURCE_CHANGED,
            "legacy YAML source changed while the read-only adapter was running",
        )


def adapt_legacy_yaml(
    path: Path | str,
    *,
    source_ref: Optional[str] = None,
    expected_contract_sha256: Optional[str] = None,
) -> AdaptedLegacyContract:
    """Read a legacy YAML without mutation and freeze a topology-equivalent ContractIR."""

    source_path = Path(path)
    logical_ref = _source_ref(source_ref if source_ref is not None else source_path.name)
    initial_bytes = _read_source(source_path)
    source_sha256 = hashlib.sha256(initial_bytes).hexdigest()
    try:
        source_text = initial_bytes.decode("utf-8")
    except UnicodeError as exc:
        raise LegacyYamlAdapterError(
            LegacyYamlAdapterFailureCode.INVALID_YAML,
            f"legacy YAML must be UTF-8: {exc}",
        ) from exc
    try:
        raw = yaml.load(source_text, Loader=_UniqueKeySafeLoader)
    except yaml.YAMLError as exc:
        raise LegacyYamlAdapterError(
            LegacyYamlAdapterFailureCode.INVALID_YAML,
            f"legacy YAML parse failed: {exc}",
        ) from exc
    raw = _validate_legacy_payload(raw)
    semantic_sha256 = _canonical_sha256(raw)

    try:
        from MobiFlow.avdag.dag import DAG
        from MobiFlow.avdag.loader import load_task

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            legacy_task = load_task(str(source_path))
            legacy_dag = DAG(legacy_task.nodes)
    except (OSError, UnicodeError, yaml.YAMLError, KeyError, TypeError, ValueError) as exc:
        _assert_source_unchanged(source_path, initial_bytes)
        raise LegacyYamlAdapterError(
            LegacyYamlAdapterFailureCode.INVALID_TOPOLOGY,
            f"legacy avdag rejected the source: {exc}",
        ) from exc
    _assert_source_unchanged(source_path, initial_bytes)

    contract_dag = _contract_dag_from_legacy_task(legacy_task)
    try:
        converted_topology = contract_dag_topology(contract_dag)
    except ValueError as exc:
        raise LegacyYamlAdapterError(
            LegacyYamlAdapterFailureCode.INVALID_TOPOLOGY,
            f"legacy topology cannot be represented as a valid ContractIR DAG: {exc}",
        ) from exc
    legacy_topology = legacy_avdag_topology(legacy_task, legacy_dag)
    assert_topology_equivalent(legacy_topology, converted_topology)

    contract = ContractIR(
        contract_id=f"legacy:{raw['task_id']}:{semantic_sha256}",
        criteria=(
            CriterionIR(
                criterion_id="legacy.avdag_execution",
                temporal_semantics=TemporalSemantics.EVENTUAL_STATE,
                required=True,
                description=(
                    "Lowered legacy DAG outcome evaluated by the local four-valued kernel; "
                    "checker outcomes must be supplied as immutable evidence signals."
                ),
            ),
        ),
        source=LEGACY_YAML_SOURCE,
        compiler_provenance=ContractProvenanceIR(
            source_type=ContractSourceType.LEGACY,
            source_id=raw["task_id"],
            source_version=LEGACY_PIPELINE_VERSION,
            source_digest=semantic_sha256,
            source_locator=raw["task_id"],
            selection_key=raw["task_id"],
        ),
        task_family=raw["task_type"],
        dag=contract_dag,
    )
    try:
        validated = _validation.validate_and_freeze_contract(
            contract,
            expected_source=LEGACY_YAML_SOURCE,
            expected_contract_sha256=expected_contract_sha256,
        )
    except _validation.ContractValidationError as exc:
        if exc.code is _validation.ContractValidationFailureCode.HASH_MISMATCH:
            code = LegacyYamlAdapterFailureCode.HASH_MISMATCH
        elif exc.code is _validation.ContractValidationFailureCode.INVALID_EXPECTED_HASH:
            code = LegacyYamlAdapterFailureCode.INVALID_EXPECTED_HASH
        else:
            code = LegacyYamlAdapterFailureCode.INVALID_CONTRACT
        raise LegacyYamlAdapterError(code, str(exc)) from exc
    provenance = LegacyYamlProvenance(
        source_ref=logical_ref,
        source_sha256=source_sha256,
        semantic_sha256=semantic_sha256,
        task_id=raw["task_id"],
        app_id=raw["app_id"],
        task_type=raw["task_type"],
    )
    return AdaptedLegacyContract(
        contract=validated.contract,
        contract_sha256=validated.contract_sha256,
        provenance=provenance,
        topology=converted_topology,
        validation_funnel_version=validated.validation_funnel_version,
    )


__all__ = [
    "AdaptedLegacyContract",
    "LEGACY_YAML_ADAPTER_VERSION",
    "LEGACY_PIPELINE_VERSION",
    "LEGACY_YAML_SOURCE",
    "LegacyNodeCondition",
    "LegacyNodeDependency",
    "LegacyTopologySnapshot",
    "LegacyYamlAdapterError",
    "LegacyYamlAdapterFailureCode",
    "LegacyYamlProvenance",
    "adapt_legacy_yaml",
    "assert_topology_equivalent",
    "contract_dag_topology",
    "legacy_avdag_topology",
]
