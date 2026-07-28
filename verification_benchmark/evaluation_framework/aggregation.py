from __future__ import annotations

from dataclasses import replace
from typing import Iterable, Optional, Sequence

from .models import (
    ContractIR,
    CriterionIR,
    CriterionResult,
    CriterionStatus,
    EvidenceCapabilityProfile,
    RunReport,
    RunMode,
    RunVerdict,
    TemporalSemantics,
    TerminationQuality,
    TraceIntegrity,
)


def _criterion_verdict(results: Sequence[CriterionResult]) -> RunVerdict:
    """Map required criterion states to a conservative verdict."""

    if not results:
        return RunVerdict.ABSTAIN
    statuses = [result.status for result in results]
    if CriterionStatus.VIOLATED in statuses:
        return RunVerdict.FAIL
    if all(status is CriterionStatus.SATISFIED for status in statuses):
        return RunVerdict.PASS
    if all(status is CriterionStatus.UNSUPPORTED_CAPABILITY for status in statuses):
        return RunVerdict.UNSUPPORTED
    return RunVerdict.ABSTAIN


def _missing_result(criterion: CriterionIR) -> CriterionResult:
    return CriterionResult(
        criterion_id=criterion.criterion_id,
        temporal_semantics=criterion.temporal_semantics,
        status=CriterionStatus.UNKNOWN_EVIDENCE,
        reason="criterion result was not produced",
    )


def _apply_capability_gate(
    criterion: CriterionIR,
    result: CriterionResult,
    profile: EvidenceCapabilityProfile,
) -> CriterionResult:
    missing = profile.missing(criterion.required_capabilities)
    if not missing:
        return result
    names = ", ".join(capability.value for capability in missing)
    return replace(
        result,
        status=CriterionStatus.UNSUPPORTED_CAPABILITY,
        evidence=(),
        reason=f"required checker capability unavailable: {names}",
        first_satisfied_frame=None,
        obscured_but_persistent=False,
    )


def aggregate_contract(
    contract: ContractIR,
    criterion_results: Iterable[CriterionResult],
    capability_profile: EvidenceCapabilityProfile,
    *,
    termination_quality: TerminationQuality = TerminationQuality.UNKNOWN,
    mode: RunMode = RunMode.AUDIT_BENCHMARK,
    outcome_at_declared_done: Optional[RunVerdict] = None,
    outcome_after_grace: Optional[RunVerdict] = None,
    declared_done_frame: Optional[int] = None,
) -> RunReport:
    """Aggregate a contract without conflating outcome, process, or termination.

    G0 invalidity and contract-level mandatory evidence are handled before
    criterion verdicts. Optional checker capabilities instead gate the affected
    criterion to UNSUPPORTED_CAPABILITY; they never fabricate PASS or FAIL.
    """

    contract.validate()
    supplied = tuple(criterion_results)
    supplied_by_id = {result.criterion_id: result for result in supplied}
    if len(supplied_by_id) != len(supplied):
        raise ValueError("criterion_results must contain unique criterion_id values")
    known_ids = {criterion.criterion_id for criterion in contract.criteria}
    unexpected = sorted(set(supplied_by_id) - known_ids)
    if unexpected:
        raise ValueError(f"criterion results not present in contract: {unexpected}")

    normalized = tuple(
        _apply_capability_gate(
            criterion,
            supplied_by_id.get(criterion.criterion_id, _missing_result(criterion)),
            capability_profile,
        )
        for criterion in contract.criteria
    )
    for criterion, result in zip(contract.criteria, normalized):
        if result.temporal_semantics is not criterion.temporal_semantics:
            raise ValueError(
                f"criterion result temporal semantics mismatch for {criterion.criterion_id}"
            )

    missing_contract_capabilities = capability_profile.missing(contract.required_capabilities)
    invalid_trace = (
        capability_profile.integrity is TraceIntegrity.INVALID
        or bool(missing_contract_capabilities)
    )
    required_outcome = [
        result
        for criterion, result in zip(contract.criteria, normalized)
        if criterion.required and criterion.temporal_semantics is not TemporalSemantics.PROCESS_OBLIGATION
    ]
    required_process = [
        result
        for criterion, result in zip(contract.criteria, normalized)
        if criterion.required and criterion.temporal_semantics is TemporalSemantics.PROCESS_OBLIGATION
    ]

    outcome_verdict = _criterion_verdict(required_outcome)
    process_verdict = _criterion_verdict(required_process) if required_process else None
    reason = "contract criteria aggregated conservatively"
    if invalid_trace:
        outcome_verdict = RunVerdict.INVALID_TRACE
        process_verdict = RunVerdict.INVALID_TRACE if required_process else None
        if missing_contract_capabilities:
            names = ", ".join(capability.value for capability in missing_contract_capabilities)
            reason = f"contract-mandatory evidence completely unavailable: {names}"
        else:
            reason = "trace acquisition is invalid or an artifact is corrupt"

    return RunReport(
        contract_id=contract.contract_id,
        verdict=outcome_verdict,
        outcome_verdict=outcome_verdict,
        process_verdict=process_verdict,
        termination_quality=termination_quality,
        trace_integrity=capability_profile.integrity,
        capability_profile=capability_profile,
        criterion_results=normalized,
        mode=mode,
        outcome_at_declared_done=outcome_at_declared_done,
        outcome_after_grace=outcome_after_grace,
        declared_done_frame=declared_done_frame,
        reason=reason,
        compiler_provenance=contract.compiler_provenance,
    )
