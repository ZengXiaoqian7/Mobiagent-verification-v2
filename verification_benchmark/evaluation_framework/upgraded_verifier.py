"""Integrated MobiFlow-compatible verifier orchestrator.

The public result is intentionally small. Contract routing, canonical trace
adaptation, G0/G1 evidence quality, checker observations and temporal replay are
kept in optional diagnostics rather than exposed as the default return value.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping, Optional, Protocol, Sequence, Tuple

from PIL import Image

from .audit_envelope import (
    AuditMeasurements,
    audit_report_envelope_payload,
    build_audit_report_envelope,
)
from .contract_router import (
    ContractRouterError,
    FamilyTemplateRouteCandidate,
    RoutedContract,
    contract_selection_audit_payload,
    route_contract,
)
from .contract_catalog import (
    PHASE5_CROSS_APP_SELECTION_KEY,
    resolve_catalog_selection_key,
)
from .contract_freeze import (
    routed_contract_from_freeze,
    task_spec_from_contract_freeze,
    validate_contract_freeze,
)
from .checker_registry import CriterionCheckerRegistry
from .event_log import (
    CriterionObservationEvent,
    DurableEventTrace,
    TerminationEvent,
    attach_criterion_observations,
    contract_sha256,
    event_trace_sha256,
    trace_bundle_to_event_trace,
)
from .frozen_registry import (
    FrozenContract,
    FrozenContractRegistry,
    FrozenRegistryProvenance,
    load_frozen_registry,
)
from .g1_assembly import (
    G1ObservationAssembly,
    assemble_contract_g1_observations,
    attach_g1_observations,
)
from .jit_contract_compiler import JitCompileRequest, JitProposer
from .models import (
    ContractIR,
    ContractProvenanceIR,
    ContractRoiIR,
    ContractSourceType,
    CriterionIR,
    CriterionObservation,
    CriterionStatus,
    EvidenceCapability,
    EvidencePointer,
    G1CheckerKind,
    G1CriterionBindingIR,
    ObservationState,
    OverlayKind,
    RoiCoordinateSpace,
    RunReport,
    RunVerdict,
    TemporalSemantics,
    TerminationQuality,
    TraceIntegrity,
)
from .phase1_audit import run_report_payload, run_report_sha256
from .phase5_full_verifier_comparison import (
    FULL_VERIFIER_VERSION,
    ProviderConfig,
    VisionCallRecorder,
    evaluate_full_case,
)
from .phase5_development_verifier_smoke import verify_case_without_ground_truth
from .phase5_intake import (
    CLAIM_BOUNDARY,
    Phase5IntakeError,
    semantic_sha256,
    source_file_manifest,
    strict_json_bytes,
)
from .phase5_trace_case import (
    CasePaths,
    find_run_manifest,
    load_actions,
    trace_dir,
)
from .replay import replay_event_trace
from .runner_source import G1FrameContext
from .trace_adapter import TraceEvidenceBundle, load_trace_directory
from .task_family_catalog import route_candidate as builtin_family_route_candidate
from .task_spec import SUPPORTED_TASK_FAMILIES, TaskSpec
from .state_evidence import (
    STATE_EVIDENCE_SCHEMA_VERSION,
    evaluate_contract_state_evidence,
)


UPGRADED_VERIFIER_VERSION = "mobiagent-mobiflow-upgraded-verifier-v5"
PHASE5_CONTRACT_SELECTION_KEY = PHASE5_CROSS_APP_SELECTION_KEY
_BUILTIN_REGISTRY_ID = "mobiagent-upgraded-verifier-builtins"
_BUILTIN_REGISTRY_REVISION = "v4"


@dataclass(frozen=True)
class SimpleVerifierResult:
    """Small public result compatible with the way MobiFlow exposes ``ok``."""

    ok: bool
    verdict: str
    reason: str
    failed_criteria: Tuple[str, ...] = ()
    evidence_frames: Tuple[int, ...] = ()
    needs_review: bool = False
    matched: Tuple[Any, ...] = field(default=(), repr=False, compare=False)
    logs: Tuple[Any, ...] = field(default=(), repr=False, compare=False)
    total_score: int = field(default=0, repr=False, compare=False)

    @property
    def manual_review_needed(self) -> bool:
        return self.needs_review

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "verdict": self.verdict,
            "reason": self.reason,
            "failed_criteria": list(self.failed_criteria),
            "evidence_frames": list(self.evidence_frames),
            "needs_review": self.needs_review,
        }


@dataclass(frozen=True)
class UpgradedVerification:
    result: SimpleVerifierResult
    diagnostics: Mapping[str, Any]


class Phase5CheckerBackend(Protocol):
    def evaluate(
        self, case: CasePaths, recorder: Optional[VisionCallRecorder]
    ) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class FullPhase5CheckerBackend:
    def evaluate(
        self, case: CasePaths, recorder: Optional[VisionCallRecorder]
    ) -> Mapping[str, Any]:
        if recorder is None:
            raise ValueError("full Phase 5 backend requires a VLM recorder")
        return evaluate_full_case(case, recorder)


@dataclass(frozen=True)
class SelectivePhase5CheckerBackend:
    """Run deterministic evidence first and call VLM only when still needed."""

    @staticmethod
    def _mapped_deterministic(row: Mapping[str, Any]) -> dict[str, Any]:
        criteria = row["criteria"]

        def record(
            source_id: str | None,
            *,
            status: str = "UNKNOWN_EVIDENCE",
            reason: str,
        ) -> Mapping[str, Any]:
            if source_id is None:
                return {"status": status, "reason": reason, "evidence": None}
            return criteria[source_id]

        mapped = {
            "trace.integrity": record(
                "trace.integrity", reason="trace integrity unavailable"
            ),
            "process.source_query_visible": record(
                None,
                reason="input action exists but visible source query requires semantic evidence",
            ),
            "process.sales_sort_activated": record(
                "process.sales_sort_visual_activation",
                reason="sales sort evidence unavailable",
            ),
            "process.transfer_phrase_source_supported": record(
                None,
                reason="source card identity requires semantic evidence",
            ),
            "process.target_app_open": record(
                "process.target_app_open", reason="target app action unavailable"
            ),
            "process.target_query_visible": record(
                None,
                reason="input action exists but visible target query requires semantic evidence",
            ),
            "outcome.same_product_target_evidence": record(
                None,
                reason="same-product outcome requires semantic evidence",
            ),
            "termination.done_after_target": record(
                "termination.done_after_target",
                reason="termination evidence unavailable",
            ),
            "process.source_selection_rule": record(
                "process.source_selection_rule",
                reason="source selection evidence unavailable",
            ),
        }
        return {
            "run_id": row["run_id"],
            "task_id": row["task_id"],
            "verifier": "DETERMINISTIC_THEN_SELECTIVE_VLM",
            "verifier_version": UPGRADED_VERIFIER_VERSION,
            "verdict": row["verdict"],
            "criteria": mapped,
            "evidence_frames": row["evidence_frames"],
            "request_count": 0,
            "used_selective_vlm": False,
        }

    def evaluate(
        self,
        case: CasePaths,
        recorder: Optional[VisionCallRecorder],
        *,
        criterion_overrides: Optional[Mapping[str, Mapping[str, Any]]] = None,
    ) -> Mapping[str, Any]:
        deterministic = self._mapped_deterministic(
            verify_case_without_ground_truth(case)
        )
        if criterion_overrides:
            merged = dict(deterministic["criteria"])
            merged.update(criterion_overrides)
            deterministic["criteria"] = merged
        strong_gate_ids = (
            "process.sales_sort_activated",
            "process.target_app_open",
            "termination.done_after_target",
            "process.source_selection_rule",
        )
        if any(
            deterministic["criteria"][criterion_id]["status"] == "VIOLATED"
            for criterion_id in strong_gate_ids
        ):
            return deterministic
        if recorder is None:
            return deterministic

        before_calls = len(recorder.calls)
        layered_ids = set(criterion_overrides or ())
        learned = dict(
            evaluate_full_case(
                case,
                recorder,
                skip_sales_sort_state="process.sales_sort_activated" in layered_ids,
            )
        )
        learned_criteria = dict(learned["criteria"])
        deterministic_criteria = deterministic["criteria"]
        # Strong deterministic action facts and violations cannot be overridden.
        for criterion_id in (
            "trace.integrity",
            "process.target_app_open",
            "termination.done_after_target",
        ):
            learned_criteria[criterion_id] = deterministic_criteria[criterion_id]
        for criterion_id in (
            "process.sales_sort_activated",
            "process.source_selection_rule",
        ):
            deterministic_record = deterministic_criteria[criterion_id]
            learned_record = learned_criteria[criterion_id]
            if deterministic_record["status"] == "VIOLATED":
                learned_criteria[criterion_id] = deterministic_record
            elif (
                deterministic_record["status"] == "SATISFIED"
                and learned_record["status"] == "VIOLATED"
            ):
                learned_criteria[criterion_id] = {
                    "status": "UNKNOWN_EVIDENCE",
                    "reason": "deterministic visual evidence conflicts with VLM judgment",
                    "evidence": {
                        "deterministic": deterministic_record,
                        "vlm": learned_record,
                    },
                }
        for criterion_id in layered_ids:
            if criterion_id in deterministic_criteria:
                # The layered checker already fused deterministic evidence and
                # its own final VLM fallback.  Do not let a legacy bundled model
                # prompt re-judge the same state criterion.
                learned_criteria[criterion_id] = deterministic_criteria[criterion_id]
        learned["criteria"] = learned_criteria
        learned["used_selective_vlm"] = len(recorder.calls) > before_calls
        learned["deterministic_precheck"] = deterministic
        return learned


def _apply_criterion_dependencies(
    contract: ContractIR, checker_result: Mapping[str, Any]
) -> Mapping[str, Any]:
    """Compose independent checker conclusions with Contract dependencies.

    Checkers judge the child condition itself.  Dependency composition happens
    once, after all deterministic/layered/model evidence has been merged, so a
    second checker cannot silently re-judge a prerequisite.  A direct child
    violation remains decisive; a positive child conclusion cannot become
    satisfied while any prerequisite is unresolved.
    """

    configured = contract.metadata.get("criterion_dependencies")
    if configured is None:
        return checker_result
    if not isinstance(configured, Mapping):
        raise ValueError("criterion_dependencies metadata must be an object")
    criteria_value = checker_result.get("criteria")
    if not isinstance(criteria_value, Mapping):
        raise ValueError("checker backend did not return criteria")

    raw = {
        str(criterion_id): dict(record)
        for criterion_id, record in criteria_value.items()
        if isinstance(record, Mapping)
    }
    dependencies: dict[str, tuple[str, ...]] = {}
    for child_value, parents_value in configured.items():
        child = str(child_value)
        if (
            not isinstance(parents_value, Sequence)
            or isinstance(parents_value, (str, bytes))
            or not parents_value
        ):
            raise ValueError(
                f"criterion dependency {child!r} must declare a non-empty array"
            )
        parents = tuple(str(parent) for parent in parents_value)
        if child not in raw or any(parent not in raw for parent in parents):
            raise ValueError(
                f"criterion dependency {child!r} references unknown checker criteria"
            )
        if child in parents or len(parents) != len(set(parents)):
            raise ValueError(f"criterion dependency {child!r} is invalid")
        dependencies[child] = parents

    resolved: dict[str, dict[str, Any]] = {}
    resolving: set[str] = set()
    audit: dict[str, Any] = {}

    def resolve(criterion_id: str) -> dict[str, Any]:
        if criterion_id in resolved:
            return resolved[criterion_id]
        if criterion_id in resolving:
            raise ValueError("criterion dependency graph must be acyclic")
        resolving.add(criterion_id)
        record = dict(raw[criterion_id])
        parents = dependencies.get(criterion_id, ())
        parent_records = [resolve(parent) for parent in parents]
        parent_statuses = {
            parent: _status(parent_record.get("status"))
            for parent, parent_record in zip(parents, parent_records)
        }
        own_status = _status(record.get("status"))

        if not parents or own_status is CriterionStatus.VIOLATED:
            final = record
        elif any(
            status is CriterionStatus.VIOLATED
            for status in parent_statuses.values()
        ):
            final = {
                "status": CriterionStatus.VIOLATED.value,
                "reason": "one or more prerequisite criteria are violated",
                "evidence": {
                    "independent_condition": record,
                    "prerequisite_statuses": {
                        key: value.value for key, value in parent_statuses.items()
                    },
                },
            }
        elif all(
            status is CriterionStatus.SATISFIED
            for status in parent_statuses.values()
        ):
            final = record
        else:
            final = {
                "status": CriterionStatus.UNKNOWN_EVIDENCE.value,
                "reason": "prerequisite evidence is unresolved",
                "evidence": {
                    "independent_condition": record,
                    "prerequisite_statuses": {
                        key: value.value for key, value in parent_statuses.items()
                    },
                },
            }

        resolving.remove(criterion_id)
        resolved[criterion_id] = final
        if parents:
            audit[criterion_id] = {
                "independent_status": own_status.value,
                "prerequisites": {
                    key: value.value for key, value in parent_statuses.items()
                },
                "resolved_status": _status(final.get("status")).value,
            }
        return final

    for criterion_id in raw:
        resolve(criterion_id)
    merged = dict(checker_result)
    merged["criteria"] = resolved
    if audit:
        merged["criterion_dependency_resolution"] = audit
    return merged


@dataclass(frozen=True)
class UpgradedVerifierConfig:
    provider: Optional[ProviderConfig] = None
    registry: FrozenContractRegistry = field(default_factory=lambda: builtin_registry())
    template_candidates: Tuple[FamilyTemplateRouteCandidate, ...] = ()
    enable_validated_jit: bool = False
    jit_request: Optional[JitCompileRequest] = None
    jit_proposer: Optional[JitProposer] = None
    selection_key: Optional[str] = None
    checker_backend: Optional[Phase5CheckerBackend] = None
    checker_registry: CriterionCheckerRegistry = field(
        default_factory=CriterionCheckerRegistry
    )
    task_spec: Optional[TaskSpec] = None
    cache_dir: Optional[Path] = None
    include_diagnostics: bool = False
    continue_on_error: bool = True


@dataclass(frozen=True)
class UpgradedMobiflowOptions:
    """Compatibility options for the legacy ``verify(frames, task, options)`` shape."""

    trace_case: CasePaths
    verifier_config: UpgradedVerifierConfig


def _phase5_contract() -> ContractIR:
    registry_descriptor = (
        f"{_BUILTIN_REGISTRY_ID}:{_BUILTIN_REGISTRY_REVISION}:"
        f"{PHASE5_CONTRACT_SELECTION_KEY}"
    )
    source_digest = hashlib.sha256(registry_descriptor.encode("utf-8")).hexdigest()
    criteria = (
        CriterionIR(
            "trace.integrity",
            TemporalSemantics.PROCESS_OBLIGATION,
            required=True,
            description="Trace intake and source-tree integrity",
        ),
        CriterionIR(
            "process.source_query_visible",
            TemporalSemantics.PROCESS_OBLIGATION,
            required=True,
            required_capabilities=(EvidenceCapability.SCREENSHOT,),
        ),
        CriterionIR(
            "process.sales_sort_activated",
            TemporalSemantics.PROCESS_OBLIGATION,
            required=True,
            required_capabilities=(EvidenceCapability.SCREENSHOT,),
        ),
        CriterionIR(
            "process.transfer_phrase_source_supported",
            TemporalSemantics.PROCESS_OBLIGATION,
            required=True,
            required_capabilities=(EvidenceCapability.SCREENSHOT,),
        ),
        CriterionIR(
            "process.target_app_open",
            TemporalSemantics.PROCESS_OBLIGATION,
            required=True,
            required_capabilities=(EvidenceCapability.ACTIONS,),
        ),
        CriterionIR(
            "process.target_query_visible",
            TemporalSemantics.PROCESS_OBLIGATION,
            required=True,
            required_capabilities=(EvidenceCapability.SCREENSHOT,),
        ),
        CriterionIR(
            "outcome.same_product_target_evidence",
            TemporalSemantics.PERSISTENT_STATE,
            required=True,
            required_capabilities=(EvidenceCapability.SCREENSHOT,),
        ),
        CriterionIR(
            "termination.done_after_target",
            TemporalSemantics.PROCESS_OBLIGATION,
            required=True,
            required_capabilities=(EvidenceCapability.ACTIONS,),
        ),
        CriterionIR(
            "process.source_selection_rule",
            TemporalSemantics.PROCESS_OBLIGATION,
            required=True,
            required_capabilities=(EvidenceCapability.SCREENSHOT,),
        ),
        CriterionIR(
            "quality.not_loading",
            TemporalSemantics.PERSISTENT_STATE,
            required=False,
            required_capabilities=(EvidenceCapability.SCREENSHOT,),
        ),
        CriterionIR(
            "quality.no_blocking_overlay",
            TemporalSemantics.PERSISTENT_STATE,
            required=False,
            required_capabilities=(EvidenceCapability.SCREENSHOT,),
        ),
    )
    full_screen = ContractRoiIR(
        roi_id="full-screen",
        bounds=(0.0, 0.0, 1.0, 1.0),
        coordinate_space=RoiCoordinateSpace.NORMALIZED,
    )
    return ContractIR(
        contract_id="phase5.cross-app-realism.verifier.v1",
        criteria=criteria,
        source="frozen-registry",
        task_family="cross-app-entity-transfer",
        required_capabilities=(
            EvidenceCapability.SCREENSHOT,
            EvidenceCapability.ACTIONS,
        ),
        g1_bindings=(
            G1CriterionBindingIR(
                "quality.not_loading",
                G1CheckerKind.NOT_LOADING,
                (full_screen,),
            ),
            G1CriterionBindingIR(
                "quality.no_blocking_overlay",
                G1CheckerKind.NO_BLOCKING_OVERLAY,
                (replace(full_screen, roi_id="full-screen-overlay"),),
            ),
        ),
        compiler_provenance=ContractProvenanceIR(
            ContractSourceType.REGISTRY,
            _BUILTIN_REGISTRY_ID,
            _BUILTIN_REGISTRY_REVISION,
            source_digest,
            PHASE5_CONTRACT_SELECTION_KEY,
            PHASE5_CONTRACT_SELECTION_KEY,
        ),
        metadata={
            "checker_backend": "phase5-full-vlm",
            "checker_backend_version": FULL_VERIFIER_VERSION,
            "claim_boundary": CLAIM_BOUNDARY,
            "state_evidence": {
                "process.sales_sort_activated": {
                    "desired_state": "selected",
                    "anchor_source": "task_control_phrase",
                    "frame_scope": "source",
                    "allow_vlm": True,
                }
            },
            "criterion_dependencies": {
                "process.source_selection_rule": (
                    "process.sales_sort_activated",
                ),
            },
        },
    )


def builtin_registry() -> FrozenContractRegistry:
    contract = _phase5_contract()
    provenance = FrozenRegistryProvenance(
        registry_id=_BUILTIN_REGISTRY_ID,
        revision=_BUILTIN_REGISTRY_REVISION,
        source_digest=contract.compiler_provenance.source_digest,
    )
    return FrozenContractRegistry(
        provenance=provenance,
        contracts=(
            FrozenContract(
                registry_key=PHASE5_CONTRACT_SELECTION_KEY,
                contract=contract,
                contract_sha256=contract_sha256(contract),
                provenance=provenance,
            ),
        ),
    )


def _task_spec(case: CasePaths, config: UpgradedVerifierConfig) -> TaskSpec:
    if config.task_spec is not None:
        config.task_spec.validate()
        return config.task_spec
    if case.contract_freeze is not None:
        return task_spec_from_contract_freeze(case.contract_freeze)
    run = find_run_manifest(case.run_dir.resolve(strict=True))
    try:
        return TaskSpec.from_run_manifest(run)
    except ValueError:
        if any(
            (case.run_dir / name).is_file()
            for name in (
                "phase5_realism_collection_run_manifest.json",
                "phase5_realism_cohort_collection_run_manifest.json",
            )
        ):
            return TaskSpec(
                task_id=str(run.get("task_id") or "legacy-phase5-task"),
                task_text="在淘宝搜索商品并点击销量排序，随后打开小红书搜索来源商品",
                task_family="cross_app_entity_transfer",
                initial_app=str(run.get("initial_app") or "淘宝"),
                target_apps=(str(run.get("target_app") or "小红书"),),
            )
        raise


def _selection_key(
    case: CasePaths, config: UpgradedVerifierConfig, task_spec: TaskSpec
) -> str:
    if config.selection_key:
        return config.selection_key
    run_dir = case.run_dir.resolve(strict=True)
    run = find_run_manifest(run_dir)
    catalog_key = resolve_catalog_selection_key(run)
    if catalog_key is not None:
        return catalog_key
    # These filenames are themselves frozen acquisition-protocol identities.
    # The generic phase5_collection manifest is deliberately excluded because
    # it also represents task families without the realism sales-sort rule.
    if any(
        (run_dir / name).is_file()
        for name in (
            "phase5_realism_collection_run_manifest.json",
            "phase5_realism_cohort_collection_run_manifest.json",
        )
    ):
        return PHASE5_CONTRACT_SELECTION_KEY
    return task_spec.selection_key


def resolve_contract(
    case: CasePaths,
    config: UpgradedVerifierConfig,
    task_spec: Optional[TaskSpec] = None,
) -> RoutedContract:
    task = task_spec or _task_spec(case, config)
    if case.contract_freeze is not None:
        return routed_contract_from_freeze(
            case.contract_freeze, expected_task=task
        )
    registry = (
        load_frozen_registry(case.task_contract)
        if case.task_contract is not None
        else config.registry
    )
    key = _selection_key(case, config, task)
    candidates = config.template_candidates
    if (
        task.task_family in SUPPORTED_TASK_FAMILIES
        and not any(candidate.selection_key == key for candidate in candidates)
    ):
        candidates = candidates + (builtin_family_route_candidate(task),)
    return route_contract(
        key,
        registry,
        template_candidates=candidates,
        enable_validated_jit=config.enable_validated_jit,
        jit_request=config.jit_request,
        jit_proposer=config.jit_proposer,
    )


def _g1_contexts(
    trace_root: Path, bundle: TraceEvidenceBundle
) -> Tuple[G1FrameContext, ...]:
    contexts = []
    previous = None
    for frame in bundle.outcome_frames:
        screenshot_size = None
        if frame.screenshot_ref:
            with Image.open(trace_root / frame.screenshot_ref) as image:
                screenshot_size = (int(image.width), int(image.height))
        missing = []
        if frame.screenshot_ref is None:
            missing.append("screenshot")
        if frame.hierarchy_raw_json_ref is None and frame.hierarchy_xml_ref is None:
            missing.append("hierarchy")
        contexts.append(
            G1FrameContext(
                frame_index=frame.frame_index,
                previous_frame_index=previous,
                pre_action_index=frame.frame_index,
                screenshot_ref=frame.screenshot_ref,
                hierarchy_raw_json_ref=frame.hierarchy_raw_json_ref,
                hierarchy_xml_ref=frame.hierarchy_xml_ref,
                screenshot_size=screenshot_size,
                artifacts=(),
                raw_context_complete=not missing,
                missing_context=tuple(missing),
            )
        )
        previous = frame.frame_index
    return tuple(contexts)


def _validate_intake_binding(case: CasePaths, run_dir: Path) -> str:
    """Verify the create-once intake receipt before consuming trace evidence."""

    receipt_path = case.intake_receipt.resolve(strict=True)
    receipt = strict_json_bytes(receipt_path.read_bytes(), context="trace intake receipt")
    expected = receipt.get("source_tree_sha256")
    if not isinstance(expected, str) or len(expected) != 64:
        raise Phase5IntakeError("intake receipt lacks a valid source_tree_sha256")
    actual = semantic_sha256(list(source_file_manifest(run_dir)))
    if expected != actual:
        raise Phase5IntakeError("intake receipt source tree hash drift")
    return actual


def _status(value: Any) -> CriterionStatus:
    try:
        return CriterionStatus(str(value))
    except ValueError:
        return CriterionStatus.UNKNOWN_EVIDENCE


def _quality_state(
    assembly: Optional[G1ObservationAssembly], frame_index: int
) -> ObservationState:
    if assembly is None:
        return ObservationState.DEGRADED
    states = {
        observation.observation_state
        for observation in assembly.observations
        if observation.frame_index == frame_index
    }
    for state in (
        ObservationState.STABLE_LOADING,
        ObservationState.UNSTABLE_TRANSITION,
        ObservationState.OBSCURED_BUT_PERSISTENT,
    ):
        if state in states:
            return state
    return ObservationState.STABLE_SEMANTIC


_VISUAL_EVIDENCE_CAPABILITIES = frozenset(
    {
        EvidenceCapability.SCREENSHOT,
        EvidenceCapability.HIERARCHY_RAW_JSON,
        EvidenceCapability.HIERARCHY_XML,
    }
)


def _uses_current_frame_semantics(criterion: CriterionIR) -> bool:
    """Whether a checker conclusion depends on the observed UI frame.

    Contract capability declarations, rather than criterion names or app/task
    special cases, define this boundary.  Action/trace/logical facts must not be
    invalidated merely because the UI changed after the action.
    """

    return any(
        capability in _VISUAL_EVIDENCE_CAPABILITIES
        for capability in criterion.required_capabilities
    )


def _explicit_frame_blocker(
    assembly: Optional[G1ObservationAssembly], frame_index: int
) -> Optional[ObservationState]:
    """Return only blockers that directly describe the current frame.

    Pairwise difference is deliberately excluded: navigation commonly makes a
    successful terminal frame differ from its predecessor.  Explicit loading
    and overlays still prevent a positive/negative semantic checker result from
    becoming decisive terminal evidence.
    """

    if assembly is None:
        return ObservationState.DEGRADED
    frame = next(
        (
            item.descriptor
            for item in assembly.frames
            if item.descriptor.frame_index == frame_index
        ),
        None,
    )
    if frame is None:
        return ObservationState.DEGRADED
    if frame.observation_state is ObservationState.STABLE_LOADING:
        return ObservationState.STABLE_LOADING
    if frame.overlay_kind is not OverlayKind.NONE:
        return ObservationState.DEGRADED
    return None


def _checker_observations(
    contract: ContractIR,
    checker_result: Mapping[str, Any],
    assembly: Optional[G1ObservationAssembly],
) -> Tuple[CriterionObservation, ...]:
    frames = checker_result.get("evidence_frames")
    if not isinstance(frames, Mapping):
        raise ValueError("checker backend did not return evidence_frames")
    source_frame = int(frames["source"])
    terminal_frame = int(frames["terminal"])
    criteria = checker_result.get("criteria")
    if not isinstance(criteria, Mapping):
        raise ValueError("checker backend did not return criteria")
    contract_criteria = {
        criterion.criterion_id: criterion for criterion in contract.criteria
    }
    observations = []
    for criterion_id, record in criteria.items():
        criterion_key = str(criterion_id)
        criterion = contract_criteria.get(criterion_key)
        if criterion is None or not isinstance(record, Mapping):
            continue
        default_frame = (
            source_frame
            if criterion_key == "trace.integrity"
            or criterion_key.startswith("process.source")
            or criterion_key == "process.sales_sort_activated"
            or criterion_key == "process.transfer_phrase_source_supported"
            else terminal_frame
        )
        frame_index = int(record.get("frame_index", default_frame))
        frame_semantic = _uses_current_frame_semantics(criterion)
        state = (
            ObservationState.STABLE_SEMANTIC
            if not frame_semantic
            else _quality_state(assembly, frame_index)
        )
        status = _status(record.get("status"))
        if frame_semantic and status in {
            CriterionStatus.SATISFIED,
            CriterionStatus.VIOLATED,
        }:
            blocker = _explicit_frame_blocker(assembly, frame_index)
            if blocker is ObservationState.STABLE_LOADING or (
                blocker is not None and status is CriterionStatus.SATISFIED
            ):
                # A checker may have recognized task text underneath a loading
                # or blocking layer, but that is not positive terminal proof.
                # Loading is also non-decisive for apparent contradictions.
                state = blocker
                status = CriterionStatus.UNKNOWN_EVIDENCE
            else:
                # The checker establishes the semantics of the *current* frame.
                # A difference from the previous frame remains in G1 diagnostics
                # but is not evidence that this frame is itself unstable.  A
                # strong visible contradiction also remains decisive under an
                # overlay; fail-closed must not turn false task states unknown.
                state = ObservationState.STABLE_SEMANTIC
        observations.append(
            CriterionObservation(
                criterion_id=criterion_key,
                status=status,
                frame_index=frame_index,
                observation_state=state,
                overlay_kind=OverlayKind.NONE,
                evidence=EvidencePointer(
                    frame_index=frame_index,
                    source=f"{frame_index}.jpg",
                    detail=str(record.get("reason") or "checker observation"),
                ),
                explicit_revocation=status is CriterionStatus.VIOLATED,
            )
        )
    return tuple(observations)


def _termination(
    trace: DurableEventTrace, checker_result: Mapping[str, Any]
) -> DurableEventTrace:
    criteria = checker_result.get("criteria")
    done = {}
    if isinstance(criteria, Mapping):
        for criterion_id, record in criteria.items():
            if str(criterion_id).startswith("termination.") and isinstance(record, Mapping):
                done = record
                break
    on_time = isinstance(done, Mapping) and done.get("status") == "SATISFIED"
    events = tuple(
        (
            replace(
                event,
                quality=(
                    TerminationQuality.ON_TIME
                    if on_time
                    else TerminationQuality.PREMATURE_DONE
                ),
            )
            if isinstance(event, TerminationEvent)
            else event
        )
        for event in trace.events
    )
    updated = replace(trace, events=events)
    updated.validate()
    return updated


def _combined_verdict(contract: ContractIR, report: RunReport) -> RunVerdict:
    if report.trace_integrity is TraceIntegrity.INVALID:
        return RunVerdict.INVALID_TRACE
    required_ids = {
        criterion.criterion_id for criterion in contract.criteria if criterion.required
    }
    required = tuple(
        result
        for result in report.criterion_results
        if result.criterion_id in required_ids
    )
    statuses = {result.status for result in required}
    if CriterionStatus.VIOLATED in statuses:
        return RunVerdict.FAIL
    if statuses and statuses == {CriterionStatus.SATISFIED}:
        return RunVerdict.PASS
    if statuses and statuses.issubset(
        {
            CriterionStatus.UNSUPPORTED_CAPABILITY,
            CriterionStatus.SOURCE_EVIDENCE_MISSING,
        }
    ):
        return RunVerdict.UNSUPPORTED
    return RunVerdict.ABSTAIN


def _simple_result(
    contract: ContractIR,
    report: RunReport,
    checker_result: Mapping[str, Any],
) -> SimpleVerifierResult:
    verdict = _combined_verdict(contract, report)
    required_ids = {
        criterion.criterion_id for criterion in contract.criteria if criterion.required
    }
    if verdict is RunVerdict.FAIL:
        selected_statuses = {CriterionStatus.VIOLATED}
    elif verdict is RunVerdict.UNSUPPORTED:
        selected_statuses = {
            CriterionStatus.UNSUPPORTED_CAPABILITY,
            CriterionStatus.SOURCE_EVIDENCE_MISSING,
        }
    elif verdict is RunVerdict.ABSTAIN:
        selected_statuses = {
            CriterionStatus.UNKNOWN_EVIDENCE,
            CriterionStatus.UNSUPPORTED_CAPABILITY,
            CriterionStatus.SOURCE_EVIDENCE_MISSING,
        }
    else:
        selected_statuses = set()
    failed = tuple(
        result.criterion_id
        for result in report.criterion_results
        if result.criterion_id in required_ids and result.status in selected_statuses
    )
    criteria = checker_result.get("criteria", {})
    report_results = {
        result.criterion_id: result for result in report.criterion_results
    }
    reasons = []
    for criterion_id in failed:
        final_result = report_results[criterion_id]
        record = criteria.get(criterion_id) if isinstance(criteria, Mapping) else None
        # A checker explanation is suitable only when its status survived
        # temporal replay.  Otherwise use the final aggregation reason instead
        # of reporting a positive checker reason beside ABSTAIN/FAIL.
        if (
            isinstance(record, Mapping)
            and _status(record.get("status")) is final_result.status
            and record.get("reason")
        ):
            reasons.append(str(record["reason"]))
        elif final_result.reason:
            reasons.append(final_result.reason)
    reason = "; ".join(dict.fromkeys(reasons[:2]))
    if not reason:
        reason = {
            RunVerdict.PASS: "all required verification criteria are satisfied",
            RunVerdict.FAIL: "one or more required verification criteria are violated",
            RunVerdict.ABSTAIN: "available evidence is insufficient or conflicting",
            RunVerdict.INVALID_TRACE: "trace acquisition is invalid",
            RunVerdict.UNSUPPORTED: "required verification capability is unavailable",
        }[verdict]
    frames = sorted(
        {
            pointer.frame_index
            for result in report.criterion_results
            if result.criterion_id in failed or verdict is RunVerdict.PASS
            for pointer in result.evidence
        }
    )
    return SimpleVerifierResult(
        ok=verdict is RunVerdict.PASS,
        verdict=verdict.value,
        reason=reason,
        failed_criteria=failed,
        evidence_frames=tuple(frames),
        needs_review=verdict
        in {RunVerdict.ABSTAIN, RunVerdict.INVALID_TRACE, RunVerdict.UNSUPPORTED},
    )


def _exception_detail(exc: BaseException) -> dict[str, Any]:
    detail: dict[str, Any] = {
        "type": type(exc).__name__,
        "message": str(exc),
    }
    cause = exc.__cause__
    if cause is not None:
        detail["cause"] = {
            "type": type(cause).__name__,
            "message": str(cause),
        }
    return detail


def _early_result(
    verdict: RunVerdict,
    reason: str,
    *,
    failure_detail: Optional[Mapping[str, Any]] = None,
) -> UpgradedVerification:
    diagnostics: dict[str, Any] = {
        "verifier_version": UPGRADED_VERIFIER_VERSION,
        "pipeline_components": [],
        "failure": reason,
    }
    if failure_detail is not None:
        diagnostics["failure_detail"] = dict(failure_detail)
    return UpgradedVerification(
        SimpleVerifierResult(
            ok=False,
            verdict=verdict.value,
            reason=reason,
            needs_review=True,
        ),
        diagnostics,
    )


def verify_trace_case(
    case: CasePaths,
    config: UpgradedVerifierConfig,
    *,
    recorder: Optional[VisionCallRecorder] = None,
) -> UpgradedVerification:
    """Run the integrated verifier over one Phase 5 trace case."""

    components = []
    try:
        task_spec = _task_spec(case, config)
        routed = resolve_contract(case, config, task_spec)
        contract = routed.contract
        components.append("CONTRACT_ROUTER")

        contract_freeze = None
        if case.contract_freeze is not None:
            contract_freeze = validate_contract_freeze(
                case.contract_freeze, task_spec, routed
            )
            components.append("PRE_RUN_CONTRACT_FREEZE")

        run_dir = case.run_dir.resolve(strict=True)
        run = find_run_manifest(run_dir)
        intake_sha256 = _validate_intake_binding(case, run_dir)
        trace_root = trace_dir(run_dir, run)
        bundle = load_trace_directory(trace_root, trace_ref=str(run["run_id"]))
        durable = trace_bundle_to_event_trace(
            bundle, contract, trace_id=str(run["run_id"])
        )
        components.extend(("CANONICAL_TRACE_ADAPTER", "G0_CAPABILITY"))
        if bundle.capability_profile.integrity is TraceIntegrity.INVALID:
            return UpgradedVerification(
                SimpleVerifierResult(
                    False,
                    RunVerdict.INVALID_TRACE.value,
                    "trace acquisition is invalid or contains corrupt artifacts",
                    needs_review=True,
                ),
                {
                    "verifier_version": UPGRADED_VERIFIER_VERSION,
                    "pipeline_components": components,
                    "contract_sha256": routed.contract_sha256,
                    "capability": _capability_payload(bundle),
                },
            )

        assembly = None
        g1_error = None
        try:
            assembly = assemble_contract_g1_observations(
                trace_root, contract, _g1_contexts(trace_root, bundle)
            )
            durable = attach_g1_observations(durable, contract, assembly)
        except Exception as exc:  # noqa: BLE001 - optional quality layer degrades.
            g1_error = f"{type(exc).__name__}: {exc}"
        components.append("G1_OBSERVATION")

        if recorder is None and config.provider is not None:
            recorder = (
                VisionCallRecorder(config.provider)
                if config.cache_dir is None
                else VisionCallRecorder(config.provider, config.cache_dir)
            )
        call_start = 0 if recorder is None else len(recorder.calls)
        if config.checker_backend is not None:
            checker_result = config.checker_backend.evaluate(case, recorder)
        elif contract.contract_id == "phase5.cross-app-realism.verifier.v1":
            backend = SelectivePhase5CheckerBackend()
            deterministic_preview = backend.evaluate(case, None)
            layered_state = evaluate_contract_state_evidence(
                case,
                contract,
                task_spec,
                trace_root,
                deterministic_preview["evidence_frames"],
                recorder,
            )
            checker_result = backend.evaluate(
                case,
                recorder,
                criterion_overrides=layered_state,
            )
            layered_used_vlm = any(
                isinstance(record.get("evidence"), Mapping)
                and any(
                    isinstance(layer, Mapping)
                    and layer.get("layer") == "vlm_fact_extraction"
                    for layer in record["evidence"].get("layers", ())
                )
                for record in layered_state.values()
            )
            if layered_used_vlm and checker_result.get("used_selective_vlm") is not True:
                checker_result = dict(checker_result)
                checker_result["used_selective_vlm"] = True
        else:
            checker_result = config.checker_registry.evaluate(
                case, contract, task_spec, bundle, trace_root, recorder
            )
        checker_result = _apply_criterion_dependencies(contract, checker_result)
        trace_model_calls = (
            [] if recorder is None else list(recorder.calls[call_start:])
        )
        components.append("CHECKER_ROUTER")
        checker_criteria = checker_result.get("criteria")
        if isinstance(checker_criteria, Mapping) and any(
            isinstance(record, Mapping)
            and isinstance(record.get("evidence"), Mapping)
            and record["evidence"].get("schema_version")
            == STATE_EVIDENCE_SCHEMA_VERSION
            for record in checker_criteria.values()
        ):
            components.append("LAYERED_STATE_EVIDENCE")
        if checker_result.get("used_selective_vlm") is True:
            components.append("SELECTIVE_VLM")
        observations = _checker_observations(contract, checker_result, assembly)
        durable = attach_criterion_observations(durable, contract, observations)
        durable = _termination(durable, checker_result)
        report = replay_event_trace(contract, durable)
        components.append("TEMPORAL_REPLAY")
        simple = _simple_result(contract, report, checker_result)
        audit = build_audit_report_envelope(
            contract,
            durable,
            report,
            selection_audit=routed.audit,
            measurements=AuditMeasurements(
                model_calls=len(trace_model_calls),
            ),
        )
        components.append("AUDIT_ENVELOPE")
        components.append("MOBIFLOW_RESULT_ADAPTER")
        diagnostics = {
            "verifier_version": UPGRADED_VERIFIER_VERSION,
            "pipeline_components": components,
            "run_id": str(run["run_id"]),
            "task_id": str(run["task_id"]),
            "task_spec": task_spec.payload(),
            "contract": {
                "contract_id": contract.contract_id,
                "contract_sha256": routed.contract_sha256,
                "selection": contract_selection_audit_payload(routed.audit),
            },
            "contract_freeze": contract_freeze,
            "capability": _capability_payload(bundle),
            "intake_source_tree_sha256": intake_sha256,
            "g1": {
                "observation_count": (
                    0 if assembly is None else len(assembly.observations)
                ),
                "error": g1_error,
            },
            "event_trace_sha256": event_trace_sha256(durable),
            "run_report_sha256": run_report_sha256(report),
            "run_report": run_report_payload(report),
            "audit_envelope": audit_report_envelope_payload(audit),
            "final_verdict": simple.verdict,
            "verdicts": {
                "final": simple.verdict,
                "outcome": report.outcome_verdict.value,
                "process": (
                    None
                    if report.process_verdict is None
                    else report.process_verdict.value
                ),
            },
            "public_result": simple.as_dict(),
            "checker_backend": checker_result,
            "model_calls": trace_model_calls,
        }
        return UpgradedVerification(simple, diagnostics)
    except ContractRouterError as exc:
        return _early_result(
            RunVerdict.UNSUPPORTED,
            str(exc),
            failure_detail=_exception_detail(exc),
        )
    except Phase5IntakeError as exc:
        if not config.continue_on_error:
            raise
        return _early_result(RunVerdict.INVALID_TRACE, str(exc))
    except Exception as exc:  # noqa: BLE001 - verifier must fail closed.
        if not config.continue_on_error:
            raise
        return _early_result(
            RunVerdict.ABSTAIN, f"verifier backend error: {type(exc).__name__}: {exc}"
        )


def _capability_payload(bundle: TraceEvidenceBundle) -> dict[str, Any]:
    profile = bundle.capability_profile
    return {
        "integrity": profile.integrity.value,
        "available": sorted(capability.value for capability in profile.available),
        "screenshot_frames": list(profile.screenshot_frames),
        "hierarchy_raw_json_frames": list(profile.hierarchy_raw_json_frames),
        "hierarchy_xml_frames": list(profile.hierarchy_xml_frames),
        "action_count": profile.action_count,
        "warnings": list(profile.warnings),
        "corrupt_artifacts": list(profile.corrupt_artifacts),
        "diagnostic_fields_excluded": list(bundle.diagnostics.excluded_field_names),
    }


def verify(
    frames: Sequence[Mapping[str, Any]],
    task: Any,
    options: Optional[UpgradedMobiflowOptions] = None,
) -> SimpleVerifierResult:
    """MobiFlow-shaped compatibility API.

    The current integrated implementation requires the trace case carried by
    ``UpgradedMobiflowOptions``. ``frames`` and ``task`` remain in the signature
    so existing callers keep the familiar call shape and result.ok behavior.
    """

    del frames, task
    if not isinstance(options, UpgradedMobiflowOptions):
        return SimpleVerifierResult(
            False,
            RunVerdict.UNSUPPORTED.value,
            "upgraded verifier requires trace-backed compatibility options",
            needs_review=True,
        )
    return verify_trace_case(options.trace_case, options.verifier_config).result


__all__ = [
    "FullPhase5CheckerBackend",
    "PHASE5_CONTRACT_SELECTION_KEY",
    "Phase5CheckerBackend",
    "SimpleVerifierResult",
    "SelectivePhase5CheckerBackend",
    "UPGRADED_VERIFIER_VERSION",
    "UpgradedMobiflowOptions",
    "UpgradedVerification",
    "UpgradedVerifierConfig",
    "builtin_registry",
    "resolve_contract",
    "verify",
    "verify_trace_case",
]
