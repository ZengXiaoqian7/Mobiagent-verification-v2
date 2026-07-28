"""Regenerate the deterministic development Audit Report Envelope v1 artifact."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from verification_benchmark.evaluation_framework import (  # noqa: E402
    AUDIT_ENVELOPE_SCHEMA_VERSION,
    AuditMeasurements,
    ContractIR,
    ContractProvenanceIR,
    ContractRouterError,
    ContractRouterFailureCode,
    ContractSelectionAttempt,
    ContractSelectionAudit,
    ContractSelectionDecision,
    ContractSourceType,
    CriterionIR,
    CriterionObservation,
    CriterionObservationEvent,
    CriterionResult,
    CriterionStatus,
    DurableEventTrace,
    EvidenceCapabilityProfile,
    EvidencePointer,
    FailureCode,
    FrameEvidenceEvent,
    GuaranteeLevel,
    ObservationState,
    OverlayKind,
    RunMode,
    RunVerdict,
    TemporalSemantics,
    TerminationEvent,
    TerminationQuality,
    TraceIntegrity,
    aggregate_contract,
    audit_report_envelope_json_schema,
    audit_report_envelope_sha256,
    build_audit_report_envelope,
    build_compilation_rejection_record,
    compilation_rejection_payload,
    compilation_rejection_sha256,
    contract_sha256,
    contract_selection_audit_sha256,
    event_trace_sha256,
)


SCHEMA_PATH = ROOT / "verification_benchmark/schemas/audit_report_envelope_v1.schema.json"
AUDIT_PATH = ROOT / "verification_benchmark/reports/audit_envelope/development/mock_audit_report_envelope_v1.audit.json"
MOCK_AUDIT_SCHEMA = "harmony-eval-mock-audit-report-envelope-audit-v1"


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _canonical_sha(value: object) -> str:
    rendered = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(rendered).hexdigest()


def _build() -> tuple[dict[str, object], dict[str, object]]:
    provenance = ContractProvenanceIR(
        ContractSourceType.REGISTRY,
        "mock-development-registry",
        "1",
        _digest("mock-development-registry"),
        "registry/mock-development.json",
        "task:mock-audit-envelope",
    )
    contract = ContractIR.from_criteria(
        "development.mock-audit-envelope",
        (
            CriterionIR("visible_outcome", TemporalSemantics.PERSISTENT_STATE),
            CriterionIR("required_action", TemporalSemantics.PROCESS_OBLIGATION),
        ),
        source="frozen-registry",
        compiler_provenance=provenance,
    )
    profile = EvidenceCapabilityProfile(
        screenshot_frames=(0,), action_count=1, integrity=TraceIntegrity.VALID
    )
    trace = DurableEventTrace(
        "development-mock-audit-trace",
        contract_sha256(contract),
        profile,
        (
            FrameEvidenceEvent(
                0,
                0,
                ObservationState.STABLE_SEMANTIC,
                OverlayKind.NONE,
                screenshot_ref="frames/0.jpg",
                timestamp=1.0,
            ),
            CriterionObservationEvent(
                1,
                CriterionObservation(
                    "visible_outcome",
                    CriterionStatus.SATISFIED,
                    0,
                    ObservationState.STABLE_SEMANTIC,
                    evidence=EvidencePointer(0, "screenshot", 1.0),
                ),
            ),
            CriterionObservationEvent(
                2,
                CriterionObservation(
                    "required_action",
                    CriterionStatus.SATISFIED,
                    0,
                    ObservationState.STABLE_SEMANTIC,
                    evidence=EvidencePointer(0, "actions", 1.0),
                ),
            ),
            TerminationEvent(
                3,
                TerminationQuality.ON_TIME,
                0,
                0,
                1.0,
                1.0,
            ),
        ),
        mode=RunMode.AUDIT_BENCHMARK,
        source_trace_ref="traces/development-mock-audit-trace",
    )
    report = aggregate_contract(
        contract,
        (
            CriterionResult(
                "visible_outcome",
                TemporalSemantics.PERSISTENT_STATE,
                CriterionStatus.SATISFIED,
                evidence=(EvidencePointer(0, "screenshot", 1.0),),
                first_satisfied_frame=0,
                last_evaluated_frame=0,
            ),
            CriterionResult(
                "required_action",
                TemporalSemantics.PROCESS_OBLIGATION,
                CriterionStatus.SATISFIED,
                evidence=(EvidencePointer(0, "actions", 1.0),),
                first_satisfied_frame=0,
                last_evaluated_frame=0,
            ),
        ),
        profile,
        termination_quality=TerminationQuality.ON_TIME,
        mode=RunMode.AUDIT_BENCHMARK,
        outcome_at_declared_done=RunVerdict.PASS,
        outcome_after_grace=RunVerdict.PASS,
        declared_done_frame=0,
    )
    selection = ContractSelectionAudit(
        "task:mock-audit-envelope",
        (
            ContractSelectionAttempt(
                ContractSourceType.REGISTRY,
                ContractSelectionDecision.SELECTED,
                "mock-development-registry",
                "1",
            ),
        ),
    )
    envelope = build_audit_report_envelope(
        contract,
        trace,
        report,
        selection_audit=selection,
        measurements=AuditMeasurements(),
    )

    rejected_audit = ContractSelectionAudit(
        "task:mock-rejected",
        (
            ContractSelectionAttempt(
                ContractSourceType.REGISTRY,
                ContractSelectionDecision.REJECTED,
                "mock-development-registry",
                "1",
            ),
        ),
    )
    rejection = build_compilation_rejection_record(
        ContractRouterError(
            ContractRouterFailureCode.REGISTRY_REJECTED,
            "mock invalid contract",
            rejected_audit,
        )
    )
    schema = audit_report_envelope_json_schema()
    audit = {
        "schema_version": MOCK_AUDIT_SCHEMA,
        "envelope_schema_version": AUDIT_ENVELOPE_SCHEMA_VERSION,
        "envelope_schema_semantic_sha256": _canonical_sha(schema),
        "valid_run": {
            "contract_sha256": contract_sha256(contract),
            "trace_sha256": event_trace_sha256(trace),
            "selection_audit_sha256": contract_selection_audit_sha256(selection),
            "envelope_sha256": audit_report_envelope_sha256(envelope),
            "verdict": envelope.verdict.value,
            "outcome_verdict": envelope.outcome_verdict.value,
            "process_verdict": envelope.process_verdict.value,
            "failure_codes": [item.code.value for item in envelope.failures],
            "guarantee_level": envelope.guarantee.level.value,
            "measurements_all_unknown": all(
                item is None
                for item in (
                    envelope.measurements.latency_ms,
                    envelope.measurements.provider_calls,
                    envelope.measurements.model_calls,
                    envelope.measurements.cost_amount,
                    envelope.measurements.cost_currency,
                )
            ),
        },
        "compilation_rejection": {
            "record_sha256": compilation_rejection_sha256(rejection),
            "record": compilation_rejection_payload(rejection),
            "has_run_verdict": False,
        },
        "taxonomy_enum": [item.value for item in FailureCode],
        "claim_boundary": {
            "audit_mode_only": True,
            "guardrail_intervention": False,
            "real_provider_calls": 0,
            "api_key_reads": 0,
            "network_requests": 0,
            "held_out_reads": 0,
            "external_cost": 0,
            "guarantee_ceiling_without_extra_evidence": GuaranteeLevel.S0.value,
        },
    }
    audit["audit_semantic_sha256"] = _canonical_sha(audit)
    return schema, audit


def main() -> int:
    schema, audit = _build()
    SCHEMA_PATH.parent.mkdir(parents=True, exist_ok=True)
    AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    SCHEMA_PATH.write_text(
        json.dumps(schema, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    AUDIT_PATH.write_text(
        json.dumps(audit, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(
        "MOCK_AUDIT_ENVELOPE_PASS; "
        f"envelope_sha256={audit['valid_run']['envelope_sha256']}; "
        "network_requests=0; api_key_reads=0"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
