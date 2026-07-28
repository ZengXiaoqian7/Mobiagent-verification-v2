from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, FrozenSet, Iterable, Mapping, Optional, Tuple


def _freeze_json_value(value: Any, *, context: str) -> Any:
    """Copy a JSON value into an immutable representation."""

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{context} must not contain non-finite numbers")
        return value
    if isinstance(value, Mapping):
        frozen = {}
        for key, child in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{context} object keys must be strings")
            frozen[key] = _freeze_json_value(child, context=f"{context}.{key}")
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(
            _freeze_json_value(child, context=f"{context}[{index}]")
            for index, child in enumerate(value)
        )
    raise ValueError(f"{context} must contain only JSON-compatible values")


class CriterionStatus(str, Enum):
    SATISFIED = "SATISFIED"
    VIOLATED = "VIOLATED"
    UNKNOWN_EVIDENCE = "UNKNOWN_EVIDENCE"
    SOURCE_EVIDENCE_MISSING = "SOURCE_EVIDENCE_MISSING"
    UNSUPPORTED_CAPABILITY = "UNSUPPORTED_CAPABILITY"


class ObservationState(str, Enum):
    STABLE_SEMANTIC = "STABLE_SEMANTIC"
    STABLE_LOADING = "STABLE_LOADING"
    UNSTABLE_TRANSITION = "UNSTABLE_TRANSITION"
    OBSCURED_BUT_PERSISTENT = "OBSCURED_BUT_PERSISTENT"
    DEGRADED = "DEGRADED"
    UNKNOWN = "UNKNOWN"


class OverlayKind(str, Enum):
    NONE = "NONE"
    SYSTEM_DIALOG = "SYSTEM_DIALOG"
    APP_MODAL = "APP_MODAL"
    UNKNOWN_OVERLAY = "UNKNOWN_OVERLAY"


class TemporalSemantics(str, Enum):
    LATCHED_EVENT = "LATCHED_EVENT"
    PERSISTENT_STATE = "PERSISTENT_STATE"
    EVENTUAL_STATE = "EVENTUAL_STATE"
    PROCESS_OBLIGATION = "PROCESS_OBLIGATION"


class EvidenceCapability(str, Enum):
    SCREENSHOT = "SCREENSHOT"
    HIERARCHY_RAW_JSON = "HIERARCHY_RAW_JSON"
    HIERARCHY_XML = "HIERARCHY_XML"
    ACTIONS = "ACTIONS"
    REACT_DIAGNOSTIC = "REACT_DIAGNOSTIC"
    TIMESTAMPS = "TIMESTAMPS"
    LEGACY_AVDAG_EXECUTION = "LEGACY_AVDAG_EXECUTION"


class ContractSourceType(str, Enum):
    REGISTRY = "registry"
    TEMPLATE = "template"
    VALIDATED_JIT = "validated-jit"
    LEGACY = "legacy"


class TraceIntegrity(str, Enum):
    VALID = "VALID"
    DEGRADED = "DEGRADED"
    INVALID = "INVALID"


class RunVerdict(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    ABSTAIN = "ABSTAIN"
    INVALID_TRACE = "INVALID_TRACE"
    UNSUPPORTED = "UNSUPPORTED"


class RunMode(str, Enum):
    AUDIT_BENCHMARK = "AUDIT_BENCHMARK"
    ONLINE_GUARDRAIL = "ONLINE_GUARDRAIL"


class TerminationQuality(str, Enum):
    ON_TIME = "ON_TIME"
    PREMATURE_DONE = "PREMATURE_DONE"
    TIMEOUT = "TIMEOUT"
    UNKNOWN = "UNKNOWN"


class RoiCoordinateSpace(str, Enum):
    NORMALIZED = "NORMALIZED"
    REFERENCE_PIXELS = "REFERENCE_PIXELS"


class G1CheckerKind(str, Enum):
    ROI_STABILITY = "ROI_STABILITY"
    NO_BLOCKING_OVERLAY = "NO_BLOCKING_OVERLAY"
    NOT_LOADING = "NOT_LOADING"


class DagLogicalOperator(str, Enum):
    ALL_OF = "ALL_OF"
    ANY_OF = "ANY_OF"


class DagEdgeKind(str, Enum):
    DEPS_AND = "DEPS_AND"
    NEXT_OR = "NEXT_OR"


class DagDependencyMode(str, Enum):
    ROOT = "ROOT"
    ALL_OF = "ALL_OF"
    ANY_OF = "ANY_OF"


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _validate_canonical_id(value: str, *, context: str) -> None:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{context} must be a canonical non-empty string")


@dataclass(frozen=True)
class ContractCheckerIR:
    checker_id: str
    parameters: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.parameters, Mapping):
            raise ValueError("checker parameters must be a JSON object")
        object.__setattr__(
            self,
            "parameters",
            _freeze_json_value(self.parameters, context="checker parameters"),
        )

    def validate(self) -> None:
        _validate_canonical_id(self.checker_id, context="checker_id")
        if not isinstance(self.parameters, Mapping):
            raise ValueError("checker parameters must remain an immutable JSON object")


@dataclass(frozen=True)
class ContractDagNodeIR:
    node_id: str
    condition_operator: DagLogicalOperator
    checker_ids: Tuple[str, ...]
    condition_sha256: str
    checkers: Tuple[ContractCheckerIR, ...]
    score: int = 10

    def validate(self) -> None:
        _validate_canonical_id(self.node_id, context="DAG node_id")
        if not isinstance(self.condition_operator, DagLogicalOperator):
            raise ValueError("DAG condition_operator must be a DagLogicalOperator")
        if not isinstance(self.checker_ids, tuple) or not self.checker_ids:
            raise ValueError("DAG checker_ids must be a non-empty tuple")
        for checker_id in self.checker_ids:
            _validate_canonical_id(checker_id, context="DAG checker_id")
        if len(self.checker_ids) != len(set(self.checker_ids)):
            raise ValueError("DAG checker_ids must be unique")
        if not isinstance(self.checkers, tuple) or any(
            not isinstance(checker, ContractCheckerIR) for checker in self.checkers
        ):
            raise ValueError("DAG checkers must contain ContractCheckerIR values")
        for checker in self.checkers:
            checker.validate()
        if tuple(checker.checker_id for checker in self.checkers) != self.checker_ids:
            raise ValueError(
                "DAG checker specs must exactly match checker_ids and order"
            )
        if not isinstance(self.condition_sha256, str) or not _SHA256.fullmatch(
            self.condition_sha256
        ):
            raise ValueError("DAG condition_sha256 must be a lowercase SHA-256")
        if (
            not isinstance(self.score, int)
            or isinstance(self.score, bool)
            or self.score < 0
        ):
            raise ValueError("DAG node score must be a non-negative integer")


@dataclass(frozen=True)
class ContractDagEdgeIR:
    parent_id: str
    child_id: str
    kind: DagEdgeKind

    def validate(self) -> None:
        _validate_canonical_id(self.parent_id, context="DAG edge parent_id")
        _validate_canonical_id(self.child_id, context="DAG edge child_id")
        if self.parent_id == self.child_id:
            raise ValueError("DAG self-edges are forbidden")
        if not isinstance(self.kind, DagEdgeKind):
            raise ValueError("DAG edge kind must be a DagEdgeKind")


@dataclass(frozen=True)
class ContractDagSuccessIR:
    operator: DagLogicalOperator
    node_ids: Tuple[str, ...]

    def validate(self) -> None:
        if not isinstance(self.operator, DagLogicalOperator):
            raise ValueError("DAG success operator must be a DagLogicalOperator")
        if not isinstance(self.node_ids, tuple) or not self.node_ids:
            raise ValueError("DAG success node_ids must be a non-empty tuple")
        for node_id in self.node_ids:
            _validate_canonical_id(node_id, context="DAG success node_id")
        if len(self.node_ids) != len(set(self.node_ids)):
            raise ValueError("DAG success node_ids must be unique")


@dataclass(frozen=True)
class ContractDagIR:
    nodes: Tuple[ContractDagNodeIR, ...]
    edges: Tuple[ContractDagEdgeIR, ...]
    success: ContractDagSuccessIR

    def _topological_order_unchecked(self) -> Tuple[str, ...]:
        node_ids = tuple(node.node_id for node in self.nodes)
        indegree = {node_id: 0 for node_id in node_ids}
        children = {node_id: [] for node_id in node_ids}
        for edge in self.edges:
            indegree[edge.child_id] += 1
            children[edge.parent_id].append(edge.child_id)
        queue = [node_id for node_id in node_ids if indegree[node_id] == 0]
        order = []
        while queue:
            node_id = queue.pop(0)
            order.append(node_id)
            for child_id in children[node_id]:
                indegree[child_id] -= 1
                if indegree[child_id] == 0:
                    queue.append(child_id)
        return tuple(order)

    def validate(self) -> None:
        if not isinstance(self.nodes, tuple) or not self.nodes:
            raise ValueError("contract DAG must contain a non-empty nodes tuple")
        if any(not isinstance(node, ContractDagNodeIR) for node in self.nodes):
            raise ValueError("contract DAG nodes must contain ContractDagNodeIR values")
        for node in self.nodes:
            node.validate()
        node_ids = tuple(node.node_id for node in self.nodes)
        if len(node_ids) != len(set(node_ids)):
            raise ValueError("contract DAG node_id values must be unique")
        if not isinstance(self.edges, tuple) or any(
            not isinstance(edge, ContractDagEdgeIR) for edge in self.edges
        ):
            raise ValueError("contract DAG edges must contain ContractDagEdgeIR values")
        known_nodes = set(node_ids)
        edge_keys = []
        for edge in self.edges:
            edge.validate()
            if edge.parent_id not in known_nodes or edge.child_id not in known_nodes:
                raise ValueError("contract DAG edge references an unknown node")
            edge_keys.append((edge.parent_id, edge.child_id, edge.kind))
        if len(edge_keys) != len(set(edge_keys)):
            raise ValueError(
                "contract DAG edges must be unique by parent, child, and kind"
            )
        if not isinstance(self.success, ContractDagSuccessIR):
            raise ValueError("contract DAG success must be a ContractDagSuccessIR")
        self.success.validate()
        if any(node_id not in known_nodes for node_id in self.success.node_ids):
            raise ValueError("contract DAG success references an unknown node")
        if len(self._topological_order_unchecked()) != len(self.nodes):
            raise ValueError("contract DAG must be acyclic")

    def topological_order(self) -> Tuple[str, ...]:
        self.validate()
        return self._topological_order_unchecked()

    def sinks(self) -> Tuple[str, ...]:
        self.validate()
        parents = {edge.parent_id for edge in self.edges}
        return tuple(node.node_id for node in self.nodes if node.node_id not in parents)

    def effective_dependency(
        self, node_id: str
    ) -> Tuple[DagDependencyMode, Tuple[str, ...]]:
        self.validate()
        if node_id not in {node.node_id for node in self.nodes}:
            raise ValueError(f"unknown contract DAG node: {node_id}")
        deps = tuple(
            edge.parent_id
            for edge in self.edges
            if edge.child_id == node_id and edge.kind is DagEdgeKind.DEPS_AND
        )
        if deps:
            return DagDependencyMode.ALL_OF, deps
        next_parents = tuple(
            edge.parent_id
            for edge in self.edges
            if edge.child_id == node_id and edge.kind is DagEdgeKind.NEXT_OR
        )
        if next_parents:
            return DagDependencyMode.ANY_OF, next_parents
        return DagDependencyMode.ROOT, ()


@dataclass(frozen=True)
class ContractRoiIR:
    roi_id: str
    bounds: Tuple[float, float, float, float]
    coordinate_space: RoiCoordinateSpace = RoiCoordinateSpace.NORMALIZED
    reference_size: Optional[Tuple[int, int]] = None

    def validate(self) -> None:
        if (
            not isinstance(self.roi_id, str)
            or not self.roi_id.strip()
            or self.roi_id != self.roi_id.strip()
        ):
            raise ValueError("roi_id must be non-empty")
        if (
            not isinstance(self.bounds, tuple)
            or len(self.bounds) != 4
            or any(
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(value)
                for value in self.bounds
            )
        ):
            raise ValueError("ROI bounds must contain four finite numbers")
        x1, y1, x2, y2 = self.bounds
        if x2 <= x1 or y2 <= y1:
            raise ValueError("ROI bounds must define a positive rectangle")
        if self.coordinate_space is RoiCoordinateSpace.NORMALIZED:
            if self.reference_size is not None:
                raise ValueError("normalized ROI must not declare reference_size")
            if min(self.bounds) < 0 or max(self.bounds) > 1:
                raise ValueError("normalized ROI bounds must stay within [0, 1]")
        elif self.coordinate_space is RoiCoordinateSpace.REFERENCE_PIXELS:
            if (
                not isinstance(self.reference_size, tuple)
                or len(self.reference_size) != 2
                or any(
                    not isinstance(value, int) or isinstance(value, bool) or value <= 0
                    for value in self.reference_size
                )
            ):
                raise ValueError(
                    "reference-pixel ROI requires a positive reference_size"
                )
            width, height = self.reference_size
            if min(self.bounds) < 0 or x2 > width or y2 > height:
                raise ValueError(
                    "reference-pixel ROI bounds must stay within reference_size"
                )
        else:
            raise ValueError("coordinate_space must be a RoiCoordinateSpace")


@dataclass(frozen=True)
class G1CriterionBindingIR:
    criterion_id: str
    checker: G1CheckerKind
    rois: Tuple[ContractRoiIR, ...]

    def validate(self) -> None:
        if not isinstance(self.criterion_id, str) or not self.criterion_id.strip():
            raise ValueError("G1 binding criterion_id must be non-empty")
        if not isinstance(self.checker, G1CheckerKind):
            raise ValueError("G1 binding checker must be a G1CheckerKind")
        if not isinstance(self.rois, tuple) or not self.rois:
            raise ValueError("G1 binding must declare at least one ROI")
        if any(not isinstance(roi, ContractRoiIR) for roi in self.rois):
            raise ValueError("G1 binding rois must contain ContractRoiIR values")
        roi_ids = [roi.roi_id for roi in self.rois]
        if len(roi_ids) != len(set(roi_ids)):
            raise ValueError("G1 binding ROI ids must be unique")
        for roi in self.rois:
            roi.validate()


@dataclass(frozen=True)
class EvidencePointer:
    frame_index: int
    source: str
    timestamp: Optional[float] = None
    detail: Optional[str] = None


@dataclass(frozen=True)
class CriterionIR:
    criterion_id: str
    temporal_semantics: TemporalSemantics
    required: bool = True
    allow_obscured_persistence: bool = False
    required_capabilities: Tuple[EvidenceCapability, ...] = ()
    description: str = ""

    def validate(self) -> None:
        if not isinstance(self.criterion_id, str) or not self.criterion_id.strip():
            raise ValueError("criterion_id must be non-empty")
        if not isinstance(self.temporal_semantics, TemporalSemantics):
            raise ValueError("temporal_semantics must be a TemporalSemantics")
        if not isinstance(self.required, bool):
            raise ValueError("criterion required must be boolean")
        if not isinstance(self.allow_obscured_persistence, bool):
            raise ValueError("allow_obscured_persistence must be boolean")
        if not isinstance(self.required_capabilities, tuple) or any(
            not isinstance(item, EvidenceCapability)
            for item in self.required_capabilities
        ):
            raise ValueError(
                "criterion required_capabilities must contain EvidenceCapability values"
            )
        if len(self.required_capabilities) != len(set(self.required_capabilities)):
            raise ValueError("criterion required_capabilities must be unique")
        if not isinstance(self.description, str):
            raise ValueError("criterion description must be a string")
        if (
            self.allow_obscured_persistence
            and self.temporal_semantics is not TemporalSemantics.PERSISTENT_STATE
        ):
            raise ValueError(
                "allow_obscured_persistence is valid only for PERSISTENT_STATE"
            )


@dataclass(frozen=True)
class ContractProvenanceIR:
    source_type: ContractSourceType
    source_id: str
    source_version: str
    source_digest: str
    source_locator: str
    selection_key: str

    def validate(self, *, contract_source: str) -> None:
        if not isinstance(self.source_type, ContractSourceType):
            raise ValueError(
                "contract provenance source_type must be a ContractSourceType"
            )
        expected_sources = {
            ContractSourceType.REGISTRY: "frozen-registry",
            ContractSourceType.TEMPLATE: "family-template",
            ContractSourceType.VALIDATED_JIT: "validated-jit",
            ContractSourceType.LEGACY: "legacy-yaml-adapter",
        }
        if contract_source != expected_sources[self.source_type]:
            raise ValueError(
                "contract provenance source_type does not match contract source"
            )
        for name, value in (
            ("source_id", self.source_id),
            ("source_version", self.source_version),
            ("source_locator", self.source_locator),
            ("selection_key", self.selection_key),
        ):
            _validate_canonical_id(value, context=f"contract provenance {name}")
        if not isinstance(self.source_digest, str) or not _SHA256.fullmatch(
            self.source_digest
        ):
            raise ValueError(
                "contract provenance source_digest must be a lowercase SHA-256"
            )


@dataclass(frozen=True)
class CheckerEvidenceIdentityIR:
    """Canonical identity of the evidence window used by checker acquisition."""

    trace_id: str
    trace_sha256: str
    evidence_sha256: str
    frame_start: int
    frame_end_exclusive: int

    @property
    def frame_count(self) -> int:
        return self.frame_end_exclusive - self.frame_start

    def validate(self) -> None:
        _validate_canonical_id(self.trace_id, context="checker evidence trace_id")
        for name, value in (
            ("trace_sha256", self.trace_sha256),
            ("evidence_sha256", self.evidence_sha256),
        ):
            if not isinstance(value, str) or not _SHA256.fullmatch(value):
                raise ValueError(f"checker evidence {name} must be a lowercase SHA-256")
        for name, value in (
            ("frame_start", self.frame_start),
            ("frame_end_exclusive", self.frame_end_exclusive),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"checker evidence {name} must be non-negative")
        if self.frame_start != 0:
            raise ValueError("checker evidence v1 windows must start at frame zero")
        if self.frame_end_exclusive < self.frame_start:
            raise ValueError("checker evidence frame window is reversed")


@dataclass(frozen=True)
class CheckerAcquisitionProvenanceIR:
    """Hash-bound provenance for a deterministic checker outcome table."""

    contract_sha256: str
    evidence: CheckerEvidenceIdentityIR
    outcomes_sha256: str
    provider_id: str
    acquisition_version: str
    provider_configuration_sha256: Optional[str] = None
    evidence_storage_sha256: Optional[str] = None

    def validate(self) -> None:
        if not isinstance(self.contract_sha256, str) or not _SHA256.fullmatch(
            self.contract_sha256
        ):
            raise ValueError(
                "checker acquisition contract_sha256 must be a lowercase SHA-256"
            )
        if not isinstance(self.evidence, CheckerEvidenceIdentityIR):
            raise ValueError("checker acquisition evidence identity is invalid")
        self.evidence.validate()
        if not isinstance(self.outcomes_sha256, str) or not _SHA256.fullmatch(
            self.outcomes_sha256
        ):
            raise ValueError(
                "checker acquisition outcomes_sha256 must be a lowercase SHA-256"
            )
        _validate_canonical_id(
            self.provider_id, context="checker acquisition provider_id"
        )
        _validate_canonical_id(
            self.acquisition_version,
            context="checker acquisition acquisition_version",
        )
        if (self.provider_configuration_sha256 is None) != (
            self.evidence_storage_sha256 is None
        ):
            raise ValueError(
                "checker acquisition recorded-provider digests must be declared together"
            )
        for name, value in (
            ("provider_configuration_sha256", self.provider_configuration_sha256),
            ("evidence_storage_sha256", self.evidence_storage_sha256),
        ):
            if value is not None and (
                not isinstance(value, str) or not _SHA256.fullmatch(value)
            ):
                raise ValueError(
                    f"checker acquisition {name} must be a lowercase SHA-256"
                )


@dataclass(frozen=True)
class ContractIR:
    contract_id: str
    criteria: Tuple[CriterionIR, ...]
    schema_version: str = "harmony-eval-contract-v1"
    source: str = "registry"
    task_family: Optional[str] = None
    required_capabilities: Tuple[EvidenceCapability, ...] = ()
    g1_bindings: Tuple[G1CriterionBindingIR, ...] = ()
    dag: Optional[ContractDagIR] = None
    compiler_provenance: Optional[ContractProvenanceIR] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.metadata, Mapping):
            raise ValueError("contract metadata must be a JSON object")
        object.__setattr__(
            self,
            "metadata",
            _freeze_json_value(self.metadata, context="contract metadata"),
        )

    @classmethod
    def from_criteria(
        cls,
        contract_id: str,
        criteria: Iterable[CriterionIR],
        **kwargs: Any,
    ) -> "ContractIR":
        return cls(contract_id=contract_id, criteria=tuple(criteria), **kwargs)

    def validate(self) -> None:
        if not isinstance(self.contract_id, str) or not self.contract_id.strip():
            raise ValueError("contract_id must be non-empty")
        if self.schema_version != "harmony-eval-contract-v1":
            raise ValueError(f"unsupported schema_version: {self.schema_version}")
        if not isinstance(self.source, str) or not self.source.strip():
            raise ValueError("contract source must be non-empty")
        if self.task_family is not None and (
            not isinstance(self.task_family, str) or not self.task_family.strip()
        ):
            raise ValueError("task_family must be null or a non-empty string")
        if not isinstance(self.criteria, tuple) or not self.criteria:
            raise ValueError("contract must contain at least one criterion")
        if any(not isinstance(criterion, CriterionIR) for criterion in self.criteria):
            raise ValueError("criteria must contain CriterionIR values")
        ids = [criterion.criterion_id for criterion in self.criteria]
        if len(ids) != len(set(ids)):
            raise ValueError("criterion_id values must be unique")
        for criterion in self.criteria:
            criterion.validate()
        if not any(criterion.required for criterion in self.criteria):
            raise ValueError("contract must contain at least one required criterion")
        if not isinstance(self.required_capabilities, tuple) or any(
            not isinstance(item, EvidenceCapability)
            for item in self.required_capabilities
        ):
            raise ValueError(
                "required_capabilities must contain EvidenceCapability values"
            )
        if len(self.required_capabilities) != len(set(self.required_capabilities)):
            raise ValueError("required_capabilities must be unique")
        if not isinstance(self.g1_bindings, tuple):
            raise ValueError("g1_bindings must be a tuple")
        if any(
            not isinstance(binding, G1CriterionBindingIR)
            for binding in self.g1_bindings
        ):
            raise ValueError("g1_bindings must contain G1CriterionBindingIR values")
        binding_ids = [binding.criterion_id for binding in self.g1_bindings]
        if len(binding_ids) != len(set(binding_ids)):
            raise ValueError("a criterion may have at most one G1 binding")
        unknown_bindings = sorted(set(binding_ids) - set(ids))
        if unknown_bindings:
            raise ValueError(
                f"G1 bindings reference unknown criteria: {unknown_bindings}"
            )
        for binding in self.g1_bindings:
            binding.validate()
        if self.dag is not None:
            if not isinstance(self.dag, ContractDagIR):
                raise ValueError("contract dag must be null or a ContractDagIR")
            self.dag.validate()
        if self.compiler_provenance is not None:
            if not isinstance(self.compiler_provenance, ContractProvenanceIR):
                raise ValueError(
                    "contract compiler_provenance must be null or a ContractProvenanceIR"
                )
            self.compiler_provenance.validate(contract_source=self.source)


@dataclass(frozen=True)
class CriterionObservation:
    criterion_id: str
    status: CriterionStatus
    frame_index: int
    observation_state: ObservationState
    overlay_kind: OverlayKind = OverlayKind.NONE
    evidence: Optional[EvidencePointer] = None
    explicit_revocation: bool = False

    def __post_init__(self) -> None:
        if self.frame_index < 0:
            raise ValueError("frame_index must be non-negative")
        if self.evidence is not None and self.evidence.frame_index != self.frame_index:
            raise ValueError("evidence frame_index must match observation frame_index")


@dataclass(frozen=True)
class CriterionResult:
    criterion_id: str
    temporal_semantics: TemporalSemantics
    status: CriterionStatus
    evidence: Tuple[EvidencePointer, ...] = ()
    reason: str = ""
    first_satisfied_frame: Optional[int] = None
    last_evaluated_frame: Optional[int] = None
    obscured_but_persistent: bool = False


@dataclass(frozen=True)
class EvidenceCapabilityProfile:
    screenshot_frames: Tuple[int, ...] = ()
    hierarchy_raw_json_frames: Tuple[int, ...] = ()
    hierarchy_xml_frames: Tuple[int, ...] = ()
    action_count: int = 0
    react_count: int = 0
    timestamp_sources: Tuple[str, ...] = ()
    integrity: TraceIntegrity = TraceIntegrity.DEGRADED
    corrupt_artifacts: Tuple[str, ...] = ()
    warnings: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.action_count < 0 or self.react_count < 0:
            raise ValueError("action_count and react_count must be non-negative")

    @property
    def available(self) -> FrozenSet[EvidenceCapability]:
        capabilities = set()
        if self.screenshot_frames:
            capabilities.add(EvidenceCapability.SCREENSHOT)
        if self.hierarchy_raw_json_frames:
            capabilities.add(EvidenceCapability.HIERARCHY_RAW_JSON)
        if self.hierarchy_xml_frames:
            capabilities.add(EvidenceCapability.HIERARCHY_XML)
        if self.action_count:
            capabilities.add(EvidenceCapability.ACTIONS)
        if self.react_count:
            capabilities.add(EvidenceCapability.REACT_DIAGNOSTIC)
        if self.timestamp_sources:
            capabilities.add(EvidenceCapability.TIMESTAMPS)
        return frozenset(capabilities)

    def missing(
        self, required: Iterable[EvidenceCapability]
    ) -> Tuple[EvidenceCapability, ...]:
        return tuple(
            capability for capability in required if capability not in self.available
        )


@dataclass(frozen=True)
class RunReport:
    contract_id: str
    verdict: RunVerdict
    outcome_verdict: RunVerdict
    process_verdict: Optional[RunVerdict]
    termination_quality: TerminationQuality
    trace_integrity: TraceIntegrity
    capability_profile: EvidenceCapabilityProfile
    criterion_results: Tuple[CriterionResult, ...]
    mode: RunMode = RunMode.AUDIT_BENCHMARK
    outcome_at_declared_done: Optional[RunVerdict] = None
    outcome_after_grace: Optional[RunVerdict] = None
    declared_done_frame: Optional[int] = None
    reason: str = ""
    compiler_provenance: Optional[ContractProvenanceIR] = None
    checker_acquisition_provenance: Optional[CheckerAcquisitionProvenanceIR] = None
