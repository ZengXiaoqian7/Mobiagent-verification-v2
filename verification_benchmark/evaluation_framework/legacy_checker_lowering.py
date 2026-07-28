"""Pure local four-valued execution for semantically lowered legacy checkers."""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Mapping, Optional, Tuple

from .aggregation import aggregate_contract
from .event_log import contract_sha256
from .models import (
    CheckerAcquisitionProvenanceIR,
    CheckerEvidenceIdentityIR,
    ContractDagIR,
    ContractDagNodeIR,
    ContractIR,
    ContractSourceType,
    CriterionResult,
    CriterionStatus,
    DagDependencyMode,
    DagLogicalOperator,
    EvidenceCapability,
    EvidenceCapabilityProfile,
    RunReport,
    RunVerdict,
    TraceIntegrity,
)


LEGACY_CHECKER_LOWERING_VERSION = "harmony-eval-legacy-checker-lowering-v1"


class LegacyCheckerSignal(str, Enum):
    MATCH = "MATCH"
    NO_MATCH = "NO_MATCH"
    STRONG_CONTRADICTION = "STRONG_CONTRADICTION"
    SOURCE_EVIDENCE_MISSING = "SOURCE_EVIDENCE_MISSING"
    UNAVAILABLE = "UNAVAILABLE"

    @classmethod
    def from_legacy_bool(cls, value: Optional[bool]) -> "LegacyCheckerSignal":
        if value is True:
            return cls.MATCH
        if value is False:
            return cls.NO_MATCH
        if value is None:
            return cls.UNAVAILABLE
        raise ValueError("legacy checker result must be true, false, or null")


@dataclass(frozen=True)
class LegacyCheckerOutcome:
    node_id: str
    checker_id: str
    frame_index: int
    signal: LegacyCheckerSignal
    reason_code: str = "legacy-manual"
    evidence_fields: Tuple[str, ...] = ()

    def validate(self) -> None:
        for name, value in (
            ("node_id", self.node_id),
            ("checker_id", self.checker_id),
        ):
            if (
                not isinstance(value, str)
                or not value.strip()
                or value != value.strip()
            ):
                raise ValueError(f"legacy checker outcome {name} must be canonical")
        if (
            not isinstance(self.frame_index, int)
            or isinstance(self.frame_index, bool)
            or self.frame_index < 0
        ):
            raise ValueError("legacy checker outcome frame_index must be non-negative")
        if not isinstance(self.signal, LegacyCheckerSignal):
            raise ValueError("legacy checker outcome signal is invalid")
        if (
            not isinstance(self.reason_code, str)
            or not self.reason_code.strip()
            or self.reason_code != self.reason_code.strip()
        ):
            raise ValueError("legacy checker outcome reason_code must be canonical")
        if not isinstance(self.evidence_fields, tuple) or any(
            not isinstance(value, str) or not value.strip() or value != value.strip()
            for value in self.evidence_fields
        ):
            raise ValueError(
                "legacy checker outcome evidence_fields must be canonical strings"
            )
        if len(self.evidence_fields) != len(set(self.evidence_fields)):
            raise ValueError("legacy checker outcome evidence_fields must be unique")


@dataclass(frozen=True)
class LegacyCheckerOutcomeTable:
    outcomes: Tuple[LegacyCheckerOutcome, ...]
    provenance: Optional[CheckerAcquisitionProvenanceIR] = None

    def validate(
        self,
        contract: ContractIR,
        *,
        evidence_identity: CheckerEvidenceIdentityIR,
        frame_count: int,
    ) -> None:
        if not isinstance(self.outcomes, tuple):
            raise ValueError("legacy checker outcomes must be an immutable tuple")
        if not isinstance(self.provenance, CheckerAcquisitionProvenanceIR):
            raise ValueError(
                "legacy checker outcome table lacks acquisition provenance"
            )
        self.provenance.validate()
        evidence_identity.validate()
        actual_contract_sha256 = contract_sha256(contract)
        if not hmac.compare_digest(
            self.provenance.contract_sha256, actual_contract_sha256
        ):
            raise ValueError("checker outcomes are bound to a different ContractIR")
        if self.provenance.evidence != evidence_identity:
            raise ValueError(
                "checker outcomes are bound to a different evidence identity"
            )
        if evidence_identity.frame_count != frame_count:
            raise ValueError("checker evidence window does not match frame_count")
        expected_outcomes_sha256 = legacy_checker_outcomes_sha256(
            self.outcomes,
            contract_sha256_value=actual_contract_sha256,
            evidence_identity=evidence_identity,
            provider_id=self.provenance.provider_id,
            acquisition_version=self.provenance.acquisition_version,
            provider_configuration_sha256=(
                self.provenance.provider_configuration_sha256
            ),
            evidence_storage_sha256=self.provenance.evidence_storage_sha256,
        )
        if not hmac.compare_digest(
            self.provenance.outcomes_sha256, expected_outcomes_sha256
        ):
            raise ValueError("checker outcome table hash mismatch")
        dag = contract.dag
        if not isinstance(dag, ContractDagIR):
            raise ValueError("legacy checker outcome table requires a contract DAG")
        nodes = {node.node_id: node for node in dag.nodes}
        keys = []
        for outcome in self.outcomes:
            if not isinstance(outcome, LegacyCheckerOutcome):
                raise ValueError("outcome table contains an invalid value")
            outcome.validate()
            node = nodes.get(outcome.node_id)
            if node is None:
                raise ValueError(
                    f"checker outcome references unknown DAG node: {outcome.node_id}"
                )
            if outcome.checker_id not in node.checker_ids:
                raise ValueError(
                    f"checker outcome references unconfigured checker: {outcome.checker_id}"
                )
            if outcome.frame_index >= frame_count:
                raise ValueError(
                    "checker outcome frame_index is outside the evidence window"
                )
            keys.append((outcome.node_id, outcome.checker_id, outcome.frame_index))
        if len(keys) != len(set(keys)):
            raise ValueError(
                "legacy checker outcomes must be unique by node/checker/frame"
            )

    def signal(
        self, node_id: str, checker_id: str, frame_index: int
    ) -> LegacyCheckerSignal:
        matches = tuple(
            item.signal
            for item in self.outcomes
            if item.node_id == node_id
            and item.checker_id == checker_id
            and item.frame_index == frame_index
        )
        if not matches:
            return LegacyCheckerSignal.UNAVAILABLE
        if len(matches) != 1:
            raise ValueError("duplicate checker outcomes reached execution")
        return matches[0]


def _canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _outcome_payload(outcome: LegacyCheckerOutcome) -> dict[str, Any]:
    outcome.validate()
    return {
        "node_id": outcome.node_id,
        "checker_id": outcome.checker_id,
        "frame_index": outcome.frame_index,
        "signal": outcome.signal.value,
        "reason_code": outcome.reason_code,
        "evidence_fields": list(outcome.evidence_fields),
    }


def legacy_checker_outcomes_sha256(
    outcomes: Tuple[LegacyCheckerOutcome, ...],
    *,
    contract_sha256_value: str,
    evidence_identity: CheckerEvidenceIdentityIR,
    provider_id: str,
    acquisition_version: str,
    provider_configuration_sha256: Optional[str] = None,
    evidence_storage_sha256: Optional[str] = None,
) -> str:
    if not isinstance(outcomes, tuple):
        raise ValueError("legacy checker outcomes must be an immutable tuple")
    evidence_identity.validate()
    payloads = sorted(
        (_outcome_payload(outcome) for outcome in outcomes),
        key=lambda value: (
            value["node_id"],
            value["checker_id"],
            value["frame_index"],
        ),
    )
    payload = {
        "contract_sha256": contract_sha256_value,
        "evidence": {
            "trace_id": evidence_identity.trace_id,
            "trace_sha256": evidence_identity.trace_sha256,
            "evidence_sha256": evidence_identity.evidence_sha256,
            "frame_start": evidence_identity.frame_start,
            "frame_end_exclusive": evidence_identity.frame_end_exclusive,
        },
        "provider_id": provider_id,
        "acquisition_version": acquisition_version,
        "outcomes": payloads,
    }
    if (provider_configuration_sha256 is None) != (evidence_storage_sha256 is None):
        raise ValueError("recorded-provider digests must be declared together")
    if provider_configuration_sha256 is not None:
        payload["provider_configuration_sha256"] = provider_configuration_sha256
        payload["evidence_storage_sha256"] = evidence_storage_sha256
    return hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()


def bind_legacy_checker_outcomes(
    contract: ContractIR,
    outcomes: Tuple[LegacyCheckerOutcome, ...],
    *,
    evidence_identity: CheckerEvidenceIdentityIR,
    provider_id: str,
    acquisition_version: str,
    provider_configuration_sha256: Optional[str] = None,
    evidence_storage_sha256: Optional[str] = None,
) -> LegacyCheckerOutcomeTable:
    """Bind an immutable local outcome set to one contract and evidence window."""

    if not isinstance(outcomes, tuple):
        raise ValueError("legacy checker outcomes must be an immutable tuple")
    contract.validate()
    evidence_identity.validate()
    for outcome in outcomes:
        if not isinstance(outcome, LegacyCheckerOutcome):
            raise ValueError("outcome table contains an invalid value")
        outcome.validate()
    ordered = tuple(
        sorted(
            outcomes,
            key=lambda item: (item.node_id, item.checker_id, item.frame_index),
        )
    )
    contract_digest = contract_sha256(contract)
    digest = legacy_checker_outcomes_sha256(
        ordered,
        contract_sha256_value=contract_digest,
        evidence_identity=evidence_identity,
        provider_id=provider_id,
        acquisition_version=acquisition_version,
        provider_configuration_sha256=provider_configuration_sha256,
        evidence_storage_sha256=evidence_storage_sha256,
    )
    provenance = CheckerAcquisitionProvenanceIR(
        contract_sha256=contract_digest,
        evidence=evidence_identity,
        outcomes_sha256=digest,
        provider_id=provider_id,
        acquisition_version=acquisition_version,
        provider_configuration_sha256=provider_configuration_sha256,
        evidence_storage_sha256=evidence_storage_sha256,
    )
    provenance.validate()
    return LegacyCheckerOutcomeTable(
        ordered,
        provenance,
    )


@dataclass(frozen=True)
class LoweredLegacyNodeResult:
    node_id: str
    status: CriterionStatus
    matched_frame: Optional[int]
    candidate_frames: Tuple[int, ...]
    frame_statuses: Tuple[Tuple[int, CriterionStatus], ...]


@dataclass(frozen=True)
class LoweredLegacyNodeMatch:
    node_id: str
    frame_index: int


@dataclass(frozen=True)
class LegacyLoweringEvaluation:
    report: RunReport
    node_results: Tuple[LoweredLegacyNodeResult, ...]
    matched: Tuple[LoweredLegacyNodeMatch, ...]
    total_score: int
    success: bool
    lowering_version: str = LEGACY_CHECKER_LOWERING_VERSION

    def __post_init__(self) -> None:
        if self.lowering_version != LEGACY_CHECKER_LOWERING_VERSION:
            raise ValueError("legacy checker lowering version is unsupported")
        if self.success != (self.report.outcome_verdict is RunVerdict.PASS):
            raise ValueError("legacy lowering success flag does not match RunReport")
        if (
            not isinstance(self.total_score, int)
            or isinstance(self.total_score, bool)
            or self.total_score < 0
        ):
            raise ValueError("legacy lowering total_score must be non-negative")


def _condition_status(
    node: ContractDagNodeIR,
    outcomes: LegacyCheckerOutcomeTable,
    frame_index: int,
    *,
    deadline_reached: bool,
) -> CriterionStatus:
    signals = tuple(
        outcomes.signal(node.node_id, checker_id, frame_index)
        for checker_id in node.checker_ids
    )
    if node.condition_operator is DagLogicalOperator.ANY_OF:
        if LegacyCheckerSignal.MATCH in signals:
            return CriterionStatus.SATISFIED
        if LegacyCheckerSignal.SOURCE_EVIDENCE_MISSING in signals:
            return CriterionStatus.SOURCE_EVIDENCE_MISSING
        if LegacyCheckerSignal.UNAVAILABLE in signals:
            return CriterionStatus.UNSUPPORTED_CAPABILITY
        if LegacyCheckerSignal.STRONG_CONTRADICTION in signals:
            return CriterionStatus.VIOLATED
        if deadline_reached:
            return CriterionStatus.VIOLATED
        return CriterionStatus.UNKNOWN_EVIDENCE

    if LegacyCheckerSignal.STRONG_CONTRADICTION in signals:
        return CriterionStatus.VIOLATED
    if LegacyCheckerSignal.SOURCE_EVIDENCE_MISSING in signals:
        return CriterionStatus.SOURCE_EVIDENCE_MISSING
    if LegacyCheckerSignal.UNAVAILABLE in signals:
        return CriterionStatus.UNSUPPORTED_CAPABILITY
    if all(signal is LegacyCheckerSignal.MATCH for signal in signals):
        return CriterionStatus.SATISFIED
    if deadline_reached:
        return CriterionStatus.VIOLATED
    return CriterionStatus.UNKNOWN_EVIDENCE


def _blocked_status(
    parent_results: Tuple[LoweredLegacyNodeResult, ...],
    *,
    all_required: bool,
) -> CriterionStatus:
    statuses = tuple(item.status for item in parent_results)
    if all_required:
        if CriterionStatus.VIOLATED in statuses:
            return CriterionStatus.VIOLATED
        if CriterionStatus.SOURCE_EVIDENCE_MISSING in statuses:
            return CriterionStatus.SOURCE_EVIDENCE_MISSING
        if CriterionStatus.UNSUPPORTED_CAPABILITY in statuses:
            return CriterionStatus.UNSUPPORTED_CAPABILITY
        return CriterionStatus.UNKNOWN_EVIDENCE
    if statuses and all(status is CriterionStatus.VIOLATED for status in statuses):
        return CriterionStatus.VIOLATED
    if CriterionStatus.SOURCE_EVIDENCE_MISSING in statuses:
        return CriterionStatus.SOURCE_EVIDENCE_MISSING
    if (
        statuses
        and all(
            status in (CriterionStatus.VIOLATED, CriterionStatus.UNSUPPORTED_CAPABILITY)
            for status in statuses
        )
        and CriterionStatus.UNSUPPORTED_CAPABILITY in statuses
    ):
        return CriterionStatus.UNSUPPORTED_CAPABILITY
    return CriterionStatus.UNKNOWN_EVIDENCE


def _unmatched_status(
    frame_statuses: Tuple[Tuple[int, CriterionStatus], ...],
) -> CriterionStatus:
    statuses = tuple(status for _, status in frame_statuses)
    if not statuses:
        return CriterionStatus.UNKNOWN_EVIDENCE
    if CriterionStatus.SOURCE_EVIDENCE_MISSING in statuses:
        return CriterionStatus.SOURCE_EVIDENCE_MISSING
    if CriterionStatus.UNSUPPORTED_CAPABILITY in statuses:
        return CriterionStatus.UNSUPPORTED_CAPABILITY
    if CriterionStatus.VIOLATED in statuses:
        return CriterionStatus.VIOLATED
    return CriterionStatus.UNKNOWN_EVIDENCE


def _success_status(
    dag: ContractDagIR,
    results: dict[str, LoweredLegacyNodeResult],
    *,
    final_frame: Optional[int],
    deadline_reached: bool,
) -> CriterionStatus:
    target_statuses = []
    for node_id in dag.success.node_ids:
        result = results[node_id]
        if final_frame is not None and final_frame in result.candidate_frames:
            target_statuses.append(CriterionStatus.SATISFIED)
            continue
        if result.status is CriterionStatus.UNSUPPORTED_CAPABILITY:
            target_statuses.append(CriterionStatus.UNSUPPORTED_CAPABILITY)
        elif result.status is CriterionStatus.SOURCE_EVIDENCE_MISSING:
            target_statuses.append(CriterionStatus.SOURCE_EVIDENCE_MISSING)
        elif result.status is CriterionStatus.VIOLATED:
            target_statuses.append(CriterionStatus.VIOLATED)
        elif deadline_reached and final_frame is not None:
            target_statuses.append(CriterionStatus.VIOLATED)
        else:
            target_statuses.append(CriterionStatus.UNKNOWN_EVIDENCE)

    if dag.success.operator is DagLogicalOperator.ANY_OF:
        if CriterionStatus.SATISFIED in target_statuses:
            return CriterionStatus.SATISFIED
        if all(status is CriterionStatus.VIOLATED for status in target_statuses):
            return CriterionStatus.VIOLATED
        if CriterionStatus.SOURCE_EVIDENCE_MISSING in target_statuses:
            return CriterionStatus.SOURCE_EVIDENCE_MISSING
        if (
            all(
                status
                in (CriterionStatus.VIOLATED, CriterionStatus.UNSUPPORTED_CAPABILITY)
                for status in target_statuses
            )
            and CriterionStatus.UNSUPPORTED_CAPABILITY in target_statuses
        ):
            return CriterionStatus.UNSUPPORTED_CAPABILITY
        return CriterionStatus.UNKNOWN_EVIDENCE

    if all(status is CriterionStatus.SATISFIED for status in target_statuses):
        return CriterionStatus.SATISFIED
    if CriterionStatus.VIOLATED in target_statuses:
        return CriterionStatus.VIOLATED
    if CriterionStatus.SOURCE_EVIDENCE_MISSING in target_statuses:
        return CriterionStatus.SOURCE_EVIDENCE_MISSING
    if CriterionStatus.UNSUPPORTED_CAPABILITY in target_statuses:
        return CriterionStatus.UNSUPPORTED_CAPABILITY
    return CriterionStatus.UNKNOWN_EVIDENCE


def _matched_path(
    dag: ContractDagIR,
    results: dict[str, LoweredLegacyNodeResult],
    previous: dict[str, Optional[str]],
    *,
    success: bool,
    final_frame: Optional[int],
) -> Tuple[LoweredLegacyNodeMatch, ...]:
    if not success:
        matches = [
            LoweredLegacyNodeMatch(node.node_id, results[node.node_id].matched_frame)
            for node in dag.nodes
            if results[node.node_id].matched_frame is not None
        ]
        return tuple(sorted(matches, key=lambda item: item.frame_index))

    def backtrack(node_id: str) -> list[LoweredLegacyNodeMatch]:
        chain = []
        current: Optional[str] = node_id
        while current is not None and results[current].matched_frame is not None:
            frame_index = results[current].matched_frame
            if current in dag.success.node_ids and final_frame is not None:
                frame_index = final_frame
            chain.append(LoweredLegacyNodeMatch(current, frame_index))
            current = previous[current]
        chain.reverse()
        return chain

    if dag.success.operator is DagLogicalOperator.ALL_OF:
        matched = []
        added = set()
        for target in dag.success.node_ids:
            for item in backtrack(target):
                if item.node_id not in added:
                    matched.append(item)
                    added.add(item.node_id)
    else:
        candidates = [
            node_id
            for node_id in dag.success.node_ids
            if final_frame is not None
            and final_frame in results[node_id].candidate_frames
        ]
        target = candidates[0]
        matched = backtrack(target)
    return tuple(sorted(matched, key=lambda item: item.frame_index))


def evaluate_lowered_legacy_contract(
    contract: ContractIR,
    outcomes: LegacyCheckerOutcomeTable,
    *,
    frame_count: int,
    deadline_reached: bool,
    evidence_identity: CheckerEvidenceIdentityIR,
    capability_profile: Optional[EvidenceCapabilityProfile] = None,
) -> LegacyLoweringEvaluation:
    """Execute only frozen checker signals; this function has no callback or network path."""

    if not isinstance(contract, ContractIR):
        raise ValueError("contract must be a ContractIR")
    contract.validate()
    provenance = contract.compiler_provenance
    if provenance is None or provenance.source_type is not ContractSourceType.LEGACY:
        raise ValueError(
            "legacy lowering requires a provenance-bound Legacy ContractIR"
        )
    if not isinstance(contract.dag, ContractDagIR):
        raise ValueError("legacy lowering requires a typed ContractDagIR")
    if (
        len(contract.criteria) != 1
        or contract.criteria[0].criterion_id != "legacy.avdag_execution"
    ):
        raise ValueError(
            "legacy lowering requires the adapter's macro outcome criterion"
        )
    if EvidenceCapability.LEGACY_AVDAG_EXECUTION in (
        contract.criteria[0].required_capabilities + contract.required_capabilities
    ):
        raise ValueError(
            "legacy runtime insurance capability must be removed before lowering"
        )
    if (
        not isinstance(frame_count, int)
        or isinstance(frame_count, bool)
        or frame_count < 0
    ):
        raise ValueError("frame_count must be a non-negative integer")
    if not isinstance(deadline_reached, bool):
        raise ValueError("deadline_reached must be boolean")
    if not isinstance(outcomes, LegacyCheckerOutcomeTable):
        raise ValueError("outcomes must be a LegacyCheckerOutcomeTable")
    if not isinstance(evidence_identity, CheckerEvidenceIdentityIR):
        raise ValueError("evidence_identity must be a CheckerEvidenceIdentityIR")
    outcomes.validate(
        contract,
        evidence_identity=evidence_identity,
        frame_count=frame_count,
    )
    profile = capability_profile or EvidenceCapabilityProfile(
        integrity=TraceIntegrity.VALID
    )
    if not isinstance(profile, EvidenceCapabilityProfile):
        raise ValueError("capability_profile must be an EvidenceCapabilityProfile")
    if profile.integrity is TraceIntegrity.INVALID:
        raise ValueError(
            "checker lowering cannot execute against an invalid evidence profile"
        )

    dag = contract.dag
    success_nodes = set(dag.success.node_ids)
    results: dict[str, LoweredLegacyNodeResult] = {}
    previous: dict[str, Optional[str]] = {}
    for node_id in dag.topological_order():
        node = next(item for item in dag.nodes if item.node_id == node_id)
        mode, parent_ids = dag.effective_dependency(node_id)
        parent_results = tuple(results[parent_id] for parent_id in parent_ids)
        chosen_parent: Optional[str] = None
        if mode is DagDependencyMode.ALL_OF and any(
            item.matched_frame is None for item in parent_results
        ):
            results[node_id] = LoweredLegacyNodeResult(
                node_id,
                _blocked_status(parent_results, all_required=True),
                None,
                (),
                (),
            )
            previous[node_id] = None
            continue
        if mode is DagDependencyMode.ANY_OF:
            available = tuple(
                item for item in parent_results if item.matched_frame is not None
            )
            if not available:
                results[node_id] = LoweredLegacyNodeResult(
                    node_id,
                    _blocked_status(parent_results, all_required=False),
                    None,
                    (),
                    (),
                )
                previous[node_id] = None
                continue
            chosen = min(available, key=lambda item: item.matched_frame)  # type: ignore[arg-type]
            chosen_parent = chosen.node_id
            start_frame = int(chosen.matched_frame) + 1  # type: ignore[arg-type]
        elif mode is DagDependencyMode.ALL_OF:
            chosen = max(parent_results, key=lambda item: item.matched_frame)  # type: ignore[arg-type]
            chosen_parent = chosen.node_id
            start_frame = int(chosen.matched_frame) + 1  # type: ignore[arg-type]
        else:
            start_frame = 0

        candidates = []
        frame_statuses = []
        for frame_index in range(start_frame, frame_count):
            status = _condition_status(
                node,
                outcomes,
                frame_index,
                deadline_reached=deadline_reached,
            )
            frame_statuses.append((frame_index, status))
            if status is CriterionStatus.SATISFIED:
                candidates.append(frame_index)
                if node_id not in success_nodes:
                    break
        frozen_candidates = tuple(candidates)
        frozen_statuses = tuple(frame_statuses)
        matched_frame = frozen_candidates[0] if frozen_candidates else None
        status = (
            CriterionStatus.SATISFIED
            if frozen_candidates
            else _unmatched_status(frozen_statuses)
        )
        results[node_id] = LoweredLegacyNodeResult(
            node_id,
            status,
            matched_frame,
            frozen_candidates,
            frozen_statuses,
        )
        previous[node_id] = chosen_parent

    final_frame = frame_count - 1 if frame_count else None
    macro_status = _success_status(
        dag,
        results,
        final_frame=final_frame,
        deadline_reached=deadline_reached,
    )
    success = macro_status is CriterionStatus.SATISFIED
    matched = _matched_path(
        dag,
        results,
        previous,
        success=success,
        final_frame=final_frame,
    )
    scores = {node.node_id: node.score for node in dag.nodes}
    total_score = sum(scores[item.node_id] for item in matched)
    criterion = contract.criteria[0]
    criterion_result = CriterionResult(
        criterion_id=criterion.criterion_id,
        temporal_semantics=criterion.temporal_semantics,
        status=macro_status,
        reason=(
            "legacy checker DAG reached its terminal success topology"
            if success
            else "legacy checker DAG did not establish terminal success"
        ),
        last_evaluated_frame=final_frame,
    )
    report = replace(
        aggregate_contract(contract, (criterion_result,), profile),
        checker_acquisition_provenance=outcomes.provenance,
    )
    return LegacyLoweringEvaluation(
        report=report,
        node_results=tuple(results[node.node_id] for node in dag.nodes),
        matched=matched,
        total_score=total_score,
        success=success,
    )


__all__ = [
    "LEGACY_CHECKER_LOWERING_VERSION",
    "LegacyCheckerOutcome",
    "LegacyCheckerOutcomeTable",
    "LegacyCheckerSignal",
    "LegacyLoweringEvaluation",
    "LoweredLegacyNodeMatch",
    "LoweredLegacyNodeResult",
    "bind_legacy_checker_outcomes",
    "evaluate_lowered_legacy_contract",
    "legacy_checker_outcomes_sha256",
]
