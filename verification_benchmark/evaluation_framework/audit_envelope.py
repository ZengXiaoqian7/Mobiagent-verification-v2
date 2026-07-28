"""Canonical Phase 3 Audit/Benchmark report envelope.

This module composes existing ContractIR, durable trace, RunReport, router audit,
and checker-acquisition facts.  It deliberately does not create alternate
Contract or trace hashes and never parses diagnostic ``reason`` text.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Mapping, Optional, Tuple

from .contract_router import (
    ContractRouterError,
    ContractSelectionAudit,
    contract_selection_audit_sha256,
)
from .event_log import (
    EVENT_LOG_ENVELOPE_SCHEMA_VERSION,
    CriterionObservationEvent,
    DurableEventTrace,
    FrameEvidenceEvent,
    TerminationEvent,
    contract_sha256,
    event_trace_sha256,
)
from .models import (
    CheckerAcquisitionProvenanceIR,
    CheckerEvidenceIdentityIR,
    ContractIR,
    ContractProvenanceIR,
    ContractSourceType,
    CriterionStatus,
    EvidenceCapability,
    EvidencePointer,
    ObservationState,
    OverlayKind,
    RunMode,
    RunReport,
    RunVerdict,
    TemporalSemantics,
    TerminationQuality,
    TraceIntegrity,
)
from .replay import REPLAY_ENGINE_VERSION


AUDIT_ENVELOPE_SCHEMA_VERSION = "harmony-eval-audit-report-envelope-v1"
AUDIT_COMPILATION_REJECTION_SCHEMA_VERSION = (
    "harmony-eval-audit-compilation-rejection-v1"
)
AUDIT_REPORT_CANONICALIZER_VERSION = "harmony-eval-audit-canonical-json-v1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class AuditDimension(str, Enum):
    OUTCOME = "OUTCOME"
    PROCESS = "PROCESS"


class OverlayStatus(str, Enum):
    CLEAR = "CLEAR"
    BLOCKING = "BLOCKING"
    UNKNOWN = "UNKNOWN"


class FailureDomain(str, Enum):
    TRACE = "TRACE"
    CAPABILITY = "CAPABILITY"
    OUTCOME = "OUTCOME"
    PROCESS = "PROCESS"
    TERMINATION = "TERMINATION"
    OVERLAY = "OVERLAY"
    COMPILATION = "COMPILATION"


class FailureCode(str, Enum):
    TRACE_INVALID = "TRACE_INVALID"
    SOURCE_EVIDENCE_MISSING = "SOURCE_EVIDENCE_MISSING"
    CAPABILITY_UNSUPPORTED = "CAPABILITY_UNSUPPORTED"
    OUTCOME_EVIDENCE_UNKNOWN = "OUTCOME_EVIDENCE_UNKNOWN"
    OUTCOME_VIOLATED = "OUTCOME_VIOLATED"
    PROCESS_CAPABILITY_UNSUPPORTED = "PROCESS_CAPABILITY_UNSUPPORTED"
    PROCESS_EVIDENCE_UNKNOWN = "PROCESS_EVIDENCE_UNKNOWN"
    PROCESS_OBLIGATION_VIOLATED = "PROCESS_OBLIGATION_VIOLATED"
    PREMATURE_DONE = "PREMATURE_DONE"
    TIMEOUT = "TIMEOUT"
    LEFT_SUCCESS_REGRESSION = "LEFT_SUCCESS_REGRESSION"
    TERMINAL_LOADING = "TERMINAL_LOADING"
    BLOCKING_OVERLAY = "BLOCKING_OVERLAY"
    OVERLAY_STATE_UNKNOWN = "OVERLAY_STATE_UNKNOWN"
    COMPILER_REJECTED = "COMPILER_REJECTED"


class GuaranteeLevel(str, Enum):
    NONE = "NONE"
    S0 = "S0"
    S1 = "S1"
    S2 = "S2"
    S3 = "S3"


class GuaranteeEvidenceKind(str, Enum):
    VISIBLE_RISK_ACTION_OR_RECEIPT = "VISIBLE_RISK_ACTION_OR_RECEIPT"
    INSTRUMENTED_APP_STATE_DIFF = "INSTRUMENTED_APP_STATE_DIFF"
    AUTHORIZED_BACKEND_AUDIT = "AUTHORIZED_BACKEND_AUDIT"


def _canonical_string(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{context} must be a canonical non-empty string")
    return value


def _digest(value: Any, context: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError(f"{context} must be a lowercase SHA-256")
    return value


def _non_negative_number(value: Any, context: str) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        or value < 0
    ):
        raise ValueError(f"{context} must be a finite non-negative number")
    return float(value)


@dataclass(frozen=True)
class AuditEvidencePointer:
    frame_index: int
    source: str
    timestamp: Optional[float] = None

    def validate(self) -> None:
        if (
            not isinstance(self.frame_index, int)
            or isinstance(self.frame_index, bool)
            or self.frame_index < 0
        ):
            raise ValueError("audit evidence frame_index must be non-negative")
        _canonical_string(self.source, "audit evidence source")
        if self.timestamp is not None:
            _non_negative_number(self.timestamp, "audit evidence timestamp")


@dataclass(frozen=True)
class AuditCriterionRecord:
    criterion_id: str
    dimension: AuditDimension
    temporal_semantics: TemporalSemantics
    required: bool
    status: CriterionStatus
    evidence: Tuple[AuditEvidencePointer, ...]
    first_satisfied_frame: Optional[int]
    last_evaluated_frame: Optional[int]
    obscured_but_persistent: bool

    def validate(self) -> None:
        _canonical_string(self.criterion_id, "audit criterion_id")
        if not isinstance(self.dimension, AuditDimension):
            raise ValueError("audit criterion dimension is invalid")
        if not isinstance(self.temporal_semantics, TemporalSemantics):
            raise ValueError("audit criterion temporal semantics is invalid")
        if not isinstance(self.required, bool):
            raise ValueError("audit criterion required flag must be boolean")
        if not isinstance(self.status, CriterionStatus):
            raise ValueError("audit criterion status is invalid")
        if not isinstance(self.evidence, tuple):
            raise ValueError("audit criterion evidence must be immutable")
        for item in self.evidence:
            if not isinstance(item, AuditEvidencePointer):
                raise ValueError("audit criterion evidence pointer is invalid")
            item.validate()
        for name, value in (
            ("first_satisfied_frame", self.first_satisfied_frame),
            ("last_evaluated_frame", self.last_evaluated_frame),
        ):
            if value is not None and (
                not isinstance(value, int) or isinstance(value, bool) or value < 0
            ):
                raise ValueError(f"audit criterion {name} must be non-negative or null")
        if not isinstance(self.obscured_but_persistent, bool):
            raise ValueError("audit criterion obscured flag must be boolean")


@dataclass(frozen=True)
class AuditFailureRecord:
    domain: FailureDomain
    code: FailureCode
    criterion_ids: Tuple[str, ...] = ()

    def validate(self) -> None:
        if not isinstance(self.domain, FailureDomain) or not isinstance(
            self.code, FailureCode
        ):
            raise ValueError("audit failure enum is invalid")
        expected_domains = {
            FailureCode.TRACE_INVALID: FailureDomain.TRACE,
            FailureCode.SOURCE_EVIDENCE_MISSING: FailureDomain.TRACE,
            FailureCode.CAPABILITY_UNSUPPORTED: FailureDomain.CAPABILITY,
            FailureCode.OUTCOME_EVIDENCE_UNKNOWN: FailureDomain.OUTCOME,
            FailureCode.OUTCOME_VIOLATED: FailureDomain.OUTCOME,
            FailureCode.PROCESS_CAPABILITY_UNSUPPORTED: FailureDomain.CAPABILITY,
            FailureCode.PROCESS_EVIDENCE_UNKNOWN: FailureDomain.PROCESS,
            FailureCode.PROCESS_OBLIGATION_VIOLATED: FailureDomain.PROCESS,
            FailureCode.PREMATURE_DONE: FailureDomain.TERMINATION,
            FailureCode.TIMEOUT: FailureDomain.TERMINATION,
            FailureCode.LEFT_SUCCESS_REGRESSION: FailureDomain.TERMINATION,
            FailureCode.TERMINAL_LOADING: FailureDomain.TERMINATION,
            FailureCode.BLOCKING_OVERLAY: FailureDomain.OVERLAY,
            FailureCode.OVERLAY_STATE_UNKNOWN: FailureDomain.OVERLAY,
            FailureCode.COMPILER_REJECTED: FailureDomain.COMPILATION,
        }
        if expected_domains[self.code] is not self.domain:
            raise ValueError("audit failure code/domain mismatch")
        if not isinstance(self.criterion_ids, tuple):
            raise ValueError("audit failure criterion_ids must be immutable")
        if tuple(sorted(set(self.criterion_ids))) != self.criterion_ids:
            raise ValueError("audit failure criterion_ids must be sorted and unique")
        for criterion_id in self.criterion_ids:
            _canonical_string(criterion_id, "audit failure criterion_id")


@dataclass(frozen=True)
class GuaranteeEvidenceIR:
    kind: GuaranteeEvidenceKind
    source_ref: str
    evidence_sha256: str

    def validate(self) -> None:
        if not isinstance(self.kind, GuaranteeEvidenceKind):
            raise ValueError("guarantee evidence kind is invalid")
        _canonical_string(self.source_ref, "guarantee evidence source_ref")
        _digest(self.evidence_sha256, "guarantee evidence_sha256")


@dataclass(frozen=True)
class GuaranteeClaim:
    level: GuaranteeLevel
    supporting_criterion_ids: Tuple[str, ...]
    supporting_evidence: Tuple[GuaranteeEvidenceIR, ...]

    def validate_shape(self) -> None:
        if not isinstance(self.level, GuaranteeLevel):
            raise ValueError("guarantee level is invalid")
        if (
            tuple(sorted(set(self.supporting_criterion_ids)))
            != self.supporting_criterion_ids
        ):
            raise ValueError("guarantee criterion ids must be sorted and unique")
        for criterion_id in self.supporting_criterion_ids:
            _canonical_string(criterion_id, "guarantee criterion_id")
        if not isinstance(self.supporting_evidence, tuple):
            raise ValueError("guarantee evidence must be immutable")
        for item in self.supporting_evidence:
            if not isinstance(item, GuaranteeEvidenceIR):
                raise ValueError("guarantee evidence item is invalid")
            item.validate()


@dataclass(frozen=True)
class AuditMeasurements:
    latency_ms: Optional[float] = None
    provider_calls: Optional[int] = None
    model_calls: Optional[int] = None
    cost_amount: Optional[float] = None
    cost_currency: Optional[str] = None

    def validate(self) -> None:
        if self.latency_ms is not None:
            _non_negative_number(self.latency_ms, "latency_ms")
        for name, value in (
            ("provider_calls", self.provider_calls),
            ("model_calls", self.model_calls),
        ):
            if value is not None and (
                not isinstance(value, int) or isinstance(value, bool) or value < 0
            ):
                raise ValueError(f"{name} must be a non-negative integer or null")
        if self.cost_amount is not None:
            _non_negative_number(self.cost_amount, "cost_amount")
        if (self.cost_amount is None) != (self.cost_currency is None):
            raise ValueError("cost amount and currency must be declared together")
        if self.cost_currency is not None:
            _canonical_string(self.cost_currency, "cost_currency")


@dataclass(frozen=True)
class AuditContractIdentity:
    contract_id: str
    contract_sha256: str
    contract_source: str
    compiler_provenance: ContractProvenanceIR
    selection_audit_sha256: Optional[str]

    def validate(self) -> None:
        _canonical_string(self.contract_id, "audit contract_id")
        _digest(self.contract_sha256, "audit contract_sha256")
        _canonical_string(self.contract_source, "audit contract_source")
        if not isinstance(self.compiler_provenance, ContractProvenanceIR):
            raise ValueError("audit compiler provenance is required")
        self.compiler_provenance.validate(contract_source=self.contract_source)
        if self.selection_audit_sha256 is not None:
            _digest(self.selection_audit_sha256, "selection_audit_sha256")


@dataclass(frozen=True)
class AuditTraceIdentity:
    trace_id: str
    trace_schema_version: str
    event_log_envelope_schema_version: str
    trace_sha256: str
    source_trace_ref: Optional[str]
    replay_engine_version: str = REPLAY_ENGINE_VERSION

    def validate(self) -> None:
        _canonical_string(self.trace_id, "audit trace_id")
        _canonical_string(self.trace_schema_version, "audit trace schema")
        if self.event_log_envelope_schema_version != EVENT_LOG_ENVELOPE_SCHEMA_VERSION:
            raise ValueError("audit event log envelope schema is unsupported")
        _digest(self.trace_sha256, "audit trace_sha256")
        if self.source_trace_ref is not None:
            _canonical_string(self.source_trace_ref, "audit source_trace_ref")
        if self.replay_engine_version != REPLAY_ENGINE_VERSION:
            raise ValueError("audit replay engine version is unsupported")


@dataclass(frozen=True)
class AuditTermination:
    quality: TerminationQuality
    declared_done_frame: Optional[int]
    grace_end_frame: Optional[int]
    declared_done_timestamp: Optional[float]
    grace_end_timestamp: Optional[float]

    def validate(self) -> None:
        if not isinstance(self.quality, TerminationQuality):
            raise ValueError("audit termination quality is invalid")
        for name, value in (
            ("declared_done_frame", self.declared_done_frame),
            ("grace_end_frame", self.grace_end_frame),
        ):
            if value is not None and (
                not isinstance(value, int) or isinstance(value, bool) or value < 0
            ):
                raise ValueError(f"audit termination {name} is invalid")
        for name, value in (
            ("declared_done_timestamp", self.declared_done_timestamp),
            ("grace_end_timestamp", self.grace_end_timestamp),
        ):
            if value is not None:
                _non_negative_number(value, f"audit termination {name}")


@dataclass(frozen=True)
class AuditOverlaySummary:
    status: OverlayStatus
    terminal_kinds: Tuple[OverlayKind, ...]
    terminal_loading: bool

    def validate(self) -> None:
        if not isinstance(self.status, OverlayStatus):
            raise ValueError("audit overlay status is invalid")
        if not isinstance(self.terminal_kinds, tuple) or any(
            not isinstance(item, OverlayKind) for item in self.terminal_kinds
        ):
            raise ValueError("audit terminal overlay kinds are invalid")
        if (
            tuple(sorted(set(self.terminal_kinds), key=lambda item: item.value))
            != self.terminal_kinds
        ):
            raise ValueError("audit terminal overlay kinds must be sorted and unique")
        if not isinstance(self.terminal_loading, bool):
            raise ValueError("audit terminal_loading must be boolean")


@dataclass(frozen=True)
class AuditCapabilitySummary:
    trace_integrity: TraceIntegrity
    available: Tuple[EvidenceCapability, ...]
    contract_required: Tuple[EvidenceCapability, ...]
    criterion_required: Tuple[EvidenceCapability, ...]
    missing_contract: Tuple[EvidenceCapability, ...]
    missing_criterion: Tuple[EvidenceCapability, ...]

    def validate(self) -> None:
        if not isinstance(self.trace_integrity, TraceIntegrity):
            raise ValueError("audit trace integrity is invalid")
        for name, values in (
            ("available", self.available),
            ("contract_required", self.contract_required),
            ("criterion_required", self.criterion_required),
            ("missing_contract", self.missing_contract),
            ("missing_criterion", self.missing_criterion),
        ):
            if not isinstance(values, tuple) or any(
                not isinstance(item, EvidenceCapability) for item in values
            ):
                raise ValueError(f"audit capability {name} is invalid")
            if tuple(sorted(set(values), key=lambda item: item.value)) != values:
                raise ValueError(f"audit capability {name} must be sorted and unique")
        available = set(self.available)
        expected_contract = tuple(
            item for item in self.contract_required if item not in available
        )
        expected_criterion = tuple(
            item for item in self.criterion_required if item not in available
        )
        if self.missing_contract != expected_contract:
            raise ValueError("audit missing contract capabilities are not recomputable")
        if self.missing_criterion != expected_criterion:
            raise ValueError(
                "audit missing criterion capabilities are not recomputable"
            )


@dataclass(frozen=True)
class AuditReportEnvelope:
    contract: AuditContractIdentity
    trace: AuditTraceIdentity
    verdict: RunVerdict
    outcome_verdict: RunVerdict
    process_verdict: Optional[RunVerdict]
    outcome_at_declared_done: Optional[RunVerdict]
    outcome_after_grace: Optional[RunVerdict]
    termination: AuditTermination
    overlay: AuditOverlaySummary
    capability: AuditCapabilitySummary
    criteria: Tuple[AuditCriterionRecord, ...]
    failures: Tuple[AuditFailureRecord, ...]
    guarantee: GuaranteeClaim
    acquisition_provenance: Optional[CheckerAcquisitionProvenanceIR]
    measurements: AuditMeasurements
    mode: RunMode = RunMode.AUDIT_BENCHMARK
    report_kind: str = "AUDIT_RUN"
    canonicalizer_version: str = AUDIT_REPORT_CANONICALIZER_VERSION
    schema_version: str = AUDIT_ENVELOPE_SCHEMA_VERSION

    def validate(self) -> None:
        if self.schema_version != AUDIT_ENVELOPE_SCHEMA_VERSION:
            raise ValueError("unsupported audit envelope schema")
        if self.report_kind != "AUDIT_RUN":
            raise ValueError("unsupported audit report kind")
        if self.mode is not RunMode.AUDIT_BENCHMARK:
            raise ValueError("formal audit envelopes require AUDIT_BENCHMARK mode")
        if self.canonicalizer_version != AUDIT_REPORT_CANONICALIZER_VERSION:
            raise ValueError("unsupported audit canonicalizer")
        self.contract.validate()
        self.trace.validate()
        self.termination.validate()
        self.overlay.validate()
        self.capability.validate()
        if not isinstance(self.verdict, RunVerdict) or not isinstance(
            self.outcome_verdict, RunVerdict
        ):
            raise ValueError("audit verdict is invalid")
        for value in (
            self.process_verdict,
            self.outcome_at_declared_done,
            self.outcome_after_grace,
        ):
            if value is not None and not isinstance(value, RunVerdict):
                raise ValueError("audit optional verdict is invalid")
        if not isinstance(self.criteria, tuple) or not self.criteria:
            raise ValueError("audit envelope must contain criteria")
        for item in self.criteria:
            if not isinstance(item, AuditCriterionRecord):
                raise ValueError("audit criterion record is invalid")
            item.validate()
        criterion_ids = tuple(item.criterion_id for item in self.criteria)
        if len(criterion_ids) != len(set(criterion_ids)):
            raise ValueError("audit criterion ids must be unique")
        expected_outcome, expected_process = _derived_verdicts(self)
        if (
            self.outcome_verdict is not expected_outcome
            or self.verdict is not expected_outcome
        ):
            raise ValueError(
                "audit outcome verdict is not recomputable from typed facts"
            )
        if self.process_verdict is not expected_process:
            raise ValueError(
                "audit process verdict is not recomputable from typed facts"
            )
        if not isinstance(self.failures, tuple):
            raise ValueError("audit failures must be immutable")
        for failure in self.failures:
            if not isinstance(failure, AuditFailureRecord):
                raise ValueError("audit failure record is invalid")
            failure.validate()
        if self.failures != _derive_failures(self):
            raise ValueError(
                "audit failure taxonomy is not recomputable from typed facts"
            )
        self.guarantee.validate_shape()
        if self.guarantee != _derive_guarantee(
            self.criteria,
            self.outcome_verdict,
            self.guarantee.supporting_evidence,
        ):
            raise ValueError(
                "audit guarantee claim exceeds or diverges from its evidence"
            )
        if self.acquisition_provenance is not None:
            self.acquisition_provenance.validate()
            if not hmac.compare_digest(
                self.acquisition_provenance.contract_sha256,
                self.contract.contract_sha256,
            ):
                raise ValueError("acquisition provenance contract hash mismatch")
            if not hmac.compare_digest(
                self.acquisition_provenance.evidence.trace_sha256,
                self.trace.trace_sha256,
            ):
                raise ValueError("acquisition provenance trace hash mismatch")
        self.measurements.validate()


@dataclass(frozen=True)
class CompilationRejectionRecord:
    selection_key: str
    router_version: str
    selection_audit_sha256: str
    rejected_source: ContractSourceType
    router_failure_code: str
    failure: AuditFailureRecord = AuditFailureRecord(
        FailureDomain.COMPILATION, FailureCode.COMPILER_REJECTED
    )
    record_kind: str = "COMPILATION_REJECTION"
    schema_version: str = AUDIT_COMPILATION_REJECTION_SCHEMA_VERSION

    def validate(self) -> None:
        if (
            self.schema_version != AUDIT_COMPILATION_REJECTION_SCHEMA_VERSION
            or self.record_kind != "COMPILATION_REJECTION"
        ):
            raise ValueError("invalid compilation rejection record identity")
        _canonical_string(self.selection_key, "compilation rejection selection_key")
        _canonical_string(self.router_version, "compilation rejection router_version")
        _digest(self.selection_audit_sha256, "compilation rejection selection audit")
        if not isinstance(self.rejected_source, ContractSourceType):
            raise ValueError("compilation rejection source is invalid")
        _canonical_string(
            self.router_failure_code, "compilation rejection failure code"
        )
        self.failure.validate()
        if self.failure.code is not FailureCode.COMPILER_REJECTED:
            raise ValueError("compilation rejection must use COMPILER_REJECTED")


def _termination(trace: DurableEventTrace) -> TerminationEvent:
    return next(item for item in trace.events if isinstance(item, TerminationEvent))


def _terminal_frame_facts(
    trace: DurableEventTrace,
) -> tuple[Tuple[OverlayKind, ...], bool]:
    termination = _termination(trace)
    states: list[tuple[float, int, ObservationState, OverlayKind]] = []
    timestamp_mode = (
        termination.grace_end_timestamp is not None
        or termination.declared_done_timestamp is not None
    )
    timestamp_boundary = (
        termination.grace_end_timestamp or termination.declared_done_timestamp
    )
    frame_boundary = termination.grace_end_frame
    if frame_boundary is None:
        frame_boundary = termination.declared_done_frame
    for event in trace.events:
        if isinstance(event, FrameEvidenceEvent):
            if timestamp_mode:
                if (
                    event.timestamp is None
                    or timestamp_boundary is None
                    or event.timestamp > timestamp_boundary
                ):
                    continue
                coordinate = float(event.timestamp)
            else:
                if frame_boundary is not None and event.frame_index > frame_boundary:
                    continue
                coordinate = float(event.frame_index)
            states.append(
                (
                    coordinate,
                    event.sequence_index,
                    event.observation_state,
                    event.overlay_kind,
                )
            )
        elif isinstance(event, CriterionObservationEvent):
            pointer = event.observation.evidence
            if timestamp_mode:
                if (
                    pointer is None
                    or pointer.timestamp is None
                    or timestamp_boundary is None
                    or pointer.timestamp > timestamp_boundary
                ):
                    continue
                coordinate = float(pointer.timestamp)
            else:
                if (
                    frame_boundary is not None
                    and event.observation.frame_index > frame_boundary
                ):
                    continue
                coordinate = float(event.observation.frame_index)
            states.append(
                (
                    coordinate,
                    event.sequence_index,
                    event.observation.observation_state,
                    event.observation.overlay_kind,
                )
            )
    if not states:
        return (), False
    terminal_coordinate = max(item[0] for item in states)
    terminal = tuple(item for item in states if item[0] == terminal_coordinate)
    kinds = tuple(
        sorted(
            {item[3] for item in terminal if item[3] is not OverlayKind.NONE},
            key=lambda item: item.value,
        )
    )
    loading = any(item[2] is ObservationState.STABLE_LOADING for item in terminal)
    return kinds, loading


def _overlay_summary(trace: DurableEventTrace) -> AuditOverlaySummary:
    kinds, loading = _terminal_frame_facts(trace)
    if any(
        item in (OverlayKind.SYSTEM_DIALOG, OverlayKind.APP_MODAL) for item in kinds
    ):
        status = OverlayStatus.BLOCKING
    elif OverlayKind.UNKNOWN_OVERLAY in kinds:
        status = OverlayStatus.UNKNOWN
    else:
        status = OverlayStatus.CLEAR
    return AuditOverlaySummary(status, kinds, loading)


def _failure(code: FailureCode, ids: tuple[str, ...] = ()) -> AuditFailureRecord:
    domains = {
        FailureCode.TRACE_INVALID: FailureDomain.TRACE,
        FailureCode.SOURCE_EVIDENCE_MISSING: FailureDomain.TRACE,
        FailureCode.CAPABILITY_UNSUPPORTED: FailureDomain.CAPABILITY,
        FailureCode.OUTCOME_EVIDENCE_UNKNOWN: FailureDomain.OUTCOME,
        FailureCode.OUTCOME_VIOLATED: FailureDomain.OUTCOME,
        FailureCode.PROCESS_CAPABILITY_UNSUPPORTED: FailureDomain.CAPABILITY,
        FailureCode.PROCESS_EVIDENCE_UNKNOWN: FailureDomain.PROCESS,
        FailureCode.PROCESS_OBLIGATION_VIOLATED: FailureDomain.PROCESS,
        FailureCode.PREMATURE_DONE: FailureDomain.TERMINATION,
        FailureCode.TIMEOUT: FailureDomain.TERMINATION,
        FailureCode.LEFT_SUCCESS_REGRESSION: FailureDomain.TERMINATION,
        FailureCode.TERMINAL_LOADING: FailureDomain.TERMINATION,
        FailureCode.BLOCKING_OVERLAY: FailureDomain.OVERLAY,
        FailureCode.OVERLAY_STATE_UNKNOWN: FailureDomain.OVERLAY,
        FailureCode.COMPILER_REJECTED: FailureDomain.COMPILATION,
    }
    return AuditFailureRecord(domains[code], code, tuple(sorted(set(ids))))


def _derive_failures(envelope: AuditReportEnvelope) -> Tuple[AuditFailureRecord, ...]:
    outcome = tuple(
        item
        for item in envelope.criteria
        if item.required and item.dimension is AuditDimension.OUTCOME
    )
    process = tuple(
        item
        for item in envelope.criteria
        if item.required and item.dimension is AuditDimension.PROCESS
    )
    failures: list[AuditFailureRecord] = []
    if (
        envelope.outcome_verdict is RunVerdict.INVALID_TRACE
        or envelope.capability.trace_integrity is TraceIntegrity.INVALID
    ):
        failures.append(_failure(FailureCode.TRACE_INVALID))
    source_missing = tuple(
        item.criterion_id
        for item in outcome
        if item.status is CriterionStatus.SOURCE_EVIDENCE_MISSING
    )
    process_source_missing = tuple(
        item.criterion_id
        for item in process
        if item.status is CriterionStatus.SOURCE_EVIDENCE_MISSING
    )
    if source_missing or process_source_missing:
        failures.append(
            _failure(
                FailureCode.SOURCE_EVIDENCE_MISSING,
                source_missing + process_source_missing,
            )
        )
    unsupported_outcome = tuple(
        item.criterion_id
        for item in outcome
        if item.status is CriterionStatus.UNSUPPORTED_CAPABILITY
    )
    if envelope.outcome_verdict is RunVerdict.UNSUPPORTED or unsupported_outcome:
        failures.append(
            _failure(FailureCode.CAPABILITY_UNSUPPORTED, unsupported_outcome)
        )
    unknown_outcome = tuple(
        item.criterion_id
        for item in outcome
        if item.status is CriterionStatus.UNKNOWN_EVIDENCE
    )
    if unknown_outcome or (
        envelope.outcome_verdict is RunVerdict.ABSTAIN
        and not unsupported_outcome
        and not source_missing
    ):
        failures.append(_failure(FailureCode.OUTCOME_EVIDENCE_UNKNOWN, unknown_outcome))
    violated_outcome = tuple(
        item.criterion_id for item in outcome if item.status is CriterionStatus.VIOLATED
    )
    if envelope.outcome_verdict is RunVerdict.FAIL or violated_outcome:
        failures.append(_failure(FailureCode.OUTCOME_VIOLATED, violated_outcome))
    process_unsupported = tuple(
        item.criterion_id
        for item in process
        if item.status is CriterionStatus.UNSUPPORTED_CAPABILITY
    )
    if process_unsupported:
        failures.append(
            _failure(FailureCode.PROCESS_CAPABILITY_UNSUPPORTED, process_unsupported)
        )
    process_unknown = tuple(
        item.criterion_id
        for item in process
        if item.status is CriterionStatus.UNKNOWN_EVIDENCE
    )
    if process_unknown:
        failures.append(_failure(FailureCode.PROCESS_EVIDENCE_UNKNOWN, process_unknown))
    process_violated = tuple(
        item.criterion_id for item in process if item.status is CriterionStatus.VIOLATED
    )
    if process_violated:
        failures.append(
            _failure(FailureCode.PROCESS_OBLIGATION_VIOLATED, process_violated)
        )
    if envelope.termination.quality is TerminationQuality.PREMATURE_DONE:
        failures.append(_failure(FailureCode.PREMATURE_DONE))
    if envelope.termination.quality is TerminationQuality.TIMEOUT:
        failures.append(_failure(FailureCode.TIMEOUT))
    if (
        envelope.outcome_at_declared_done is RunVerdict.PASS
        and envelope.outcome_after_grace is RunVerdict.FAIL
    ):
        failures.append(_failure(FailureCode.LEFT_SUCCESS_REGRESSION))
    if envelope.overlay.terminal_loading:
        failures.append(_failure(FailureCode.TERMINAL_LOADING))
    if envelope.overlay.status is OverlayStatus.BLOCKING:
        failures.append(_failure(FailureCode.BLOCKING_OVERLAY))
    elif envelope.overlay.status is OverlayStatus.UNKNOWN:
        failures.append(_failure(FailureCode.OVERLAY_STATE_UNKNOWN))
    return tuple(failures)


def _derive_guarantee(
    criteria: Tuple[AuditCriterionRecord, ...],
    outcome_verdict: RunVerdict,
    evidence: Tuple[GuaranteeEvidenceIR, ...],
) -> GuaranteeClaim:
    for item in evidence:
        item.validate()
    visible = tuple(
        sorted(
            item.criterion_id
            for item in criteria
            if outcome_verdict is RunVerdict.PASS
            and item.required
            and item.dimension is AuditDimension.OUTCOME
            and item.status is CriterionStatus.SATISFIED
            and any(_is_visible_outcome_pointer(pointer) for pointer in item.evidence)
        )
    )
    kinds = {item.kind for item in evidence}
    if GuaranteeEvidenceKind.AUTHORIZED_BACKEND_AUDIT in kinds:
        level = GuaranteeLevel.S3
    elif GuaranteeEvidenceKind.INSTRUMENTED_APP_STATE_DIFF in kinds:
        level = GuaranteeLevel.S2
    elif GuaranteeEvidenceKind.VISIBLE_RISK_ACTION_OR_RECEIPT in kinds:
        level = GuaranteeLevel.S1
    elif visible:
        level = GuaranteeLevel.S0
    else:
        level = GuaranteeLevel.NONE
    return GuaranteeClaim(level, visible, evidence)


def _is_visible_outcome_pointer(pointer: AuditEvidencePointer) -> bool:
    source = pointer.source.lower()
    return source in {
        "screenshot",
        "hierarchy_raw_json",
        "hierarchy_xml",
        "observable_receipt",
        "app_state",
    } or source.endswith((".jpg", ".jpeg", ".png", ".json", ".xml"))


def _criterion_records(
    contract: ContractIR, report: RunReport
) -> Tuple[AuditCriterionRecord, ...]:
    definitions = {item.criterion_id: item for item in contract.criteria}
    records = []
    for result in report.criterion_results:
        definition = definitions.get(result.criterion_id)
        if definition is None:
            raise ValueError("RunReport contains a criterion absent from ContractIR")
        dimension = (
            AuditDimension.PROCESS
            if definition.temporal_semantics is TemporalSemantics.PROCESS_OBLIGATION
            else AuditDimension.OUTCOME
        )
        pointers = tuple(
            AuditEvidencePointer(item.frame_index, item.source, item.timestamp)
            for item in result.evidence
        )
        records.append(
            AuditCriterionRecord(
                result.criterion_id,
                dimension,
                result.temporal_semantics,
                definition.required,
                result.status,
                pointers,
                result.first_satisfied_frame,
                result.last_evaluated_frame,
                result.obscured_but_persistent,
            )
        )
    expected = tuple(item.criterion_id for item in contract.criteria)
    actual = tuple(item.criterion_id for item in records)
    if actual != expected:
        raise ValueError("RunReport criteria must exactly match ContractIR order")
    return tuple(records)


def _criterion_verdict(criteria: Tuple[AuditCriterionRecord, ...]) -> RunVerdict:
    if not criteria:
        return RunVerdict.ABSTAIN
    statuses = tuple(item.status for item in criteria)
    if CriterionStatus.VIOLATED in statuses:
        return RunVerdict.FAIL
    if all(item is CriterionStatus.SATISFIED for item in statuses):
        return RunVerdict.PASS
    if all(item is CriterionStatus.UNSUPPORTED_CAPABILITY for item in statuses):
        return RunVerdict.UNSUPPORTED
    return RunVerdict.ABSTAIN


def _derived_verdicts(
    envelope: AuditReportEnvelope,
) -> tuple[RunVerdict, Optional[RunVerdict]]:
    outcome = tuple(
        item
        for item in envelope.criteria
        if item.required and item.dimension is AuditDimension.OUTCOME
    )
    process = tuple(
        item
        for item in envelope.criteria
        if item.required and item.dimension is AuditDimension.PROCESS
    )
    if envelope.capability.trace_integrity is TraceIntegrity.INVALID or bool(
        envelope.capability.missing_contract
    ):
        return RunVerdict.INVALID_TRACE, (RunVerdict.INVALID_TRACE if process else None)
    return _criterion_verdict(outcome), (
        _criterion_verdict(process) if process else None
    )


def build_audit_report_envelope(
    contract: ContractIR,
    trace: DurableEventTrace,
    report: RunReport,
    *,
    selection_audit: Optional[ContractSelectionAudit] = None,
    guarantee_evidence: Tuple[GuaranteeEvidenceIR, ...] = (),
    measurements: AuditMeasurements = AuditMeasurements(),
) -> AuditReportEnvelope:
    """Build and fully validate an Audit-only envelope from existing typed facts."""

    contract.validate()
    trace.validate()
    digest = contract_sha256(contract)
    trace_digest = event_trace_sha256(trace)
    if not hmac.compare_digest(trace.contract_sha256, digest):
        raise ValueError("audit trace Contract hash mismatch")
    if contract.compiler_provenance is None:
        raise ValueError("formal audit envelope requires complete compiler provenance")
    if report.contract_id != contract.contract_id:
        raise ValueError("RunReport Contract id mismatch")
    if report.compiler_provenance != contract.compiler_provenance:
        raise ValueError("RunReport compiler provenance mismatch")
    if report.capability_profile != trace.capability_profile:
        raise ValueError("RunReport capability profile mismatch")
    if report.trace_integrity is not trace.capability_profile.integrity:
        raise ValueError("RunReport trace integrity mismatch")
    if (
        report.mode is not RunMode.AUDIT_BENCHMARK
        or trace.mode is not RunMode.AUDIT_BENCHMARK
    ):
        raise ValueError("formal audit envelope cannot contain Guardrail mode")
    selection_digest = None
    if selection_audit is not None:
        selection_audit.validate(require_selected=True)
        selected = selection_audit.attempts[-1]
        provenance = contract.compiler_provenance
        if (
            selected.source_type is not provenance.source_type
            or selected.source_id != provenance.source_id
            or selected.source_version != provenance.source_version
            or selection_audit.selection_key != provenance.selection_key
        ):
            raise ValueError("selection audit does not match compiler provenance")
        selection_digest = contract_selection_audit_sha256(selection_audit)
    termination_event = _termination(trace)
    if report.termination_quality is not termination_event.quality:
        raise ValueError("RunReport termination quality mismatch")
    if report.declared_done_frame != termination_event.declared_done_frame:
        raise ValueError("RunReport declared-done boundary mismatch")
    criteria = _criterion_records(contract, report)
    available = tuple(
        sorted(trace.capability_profile.available, key=lambda item: item.value)
    )
    contract_required = tuple(
        sorted(contract.required_capabilities, key=lambda item: item.value)
    )
    criterion_required = tuple(
        sorted(
            {
                capability
                for criterion in contract.criteria
                if criterion.required
                for capability in criterion.required_capabilities
            },
            key=lambda item: item.value,
        )
    )
    available_set = set(available)
    missing_contract = tuple(
        item for item in contract_required if item not in available_set
    )
    missing_criterion = tuple(
        item for item in criterion_required if item not in available_set
    )
    guarantee_evidence = tuple(guarantee_evidence)
    measurements.validate()
    envelope = AuditReportEnvelope(
        contract=AuditContractIdentity(
            contract.contract_id,
            digest,
            contract.source,
            contract.compiler_provenance,
            selection_digest,
        ),
        trace=AuditTraceIdentity(
            trace.trace_id,
            trace.schema_version,
            EVENT_LOG_ENVELOPE_SCHEMA_VERSION,
            trace_digest,
            trace.source_trace_ref,
        ),
        verdict=report.verdict,
        outcome_verdict=report.outcome_verdict,
        process_verdict=report.process_verdict,
        outcome_at_declared_done=report.outcome_at_declared_done,
        outcome_after_grace=report.outcome_after_grace,
        termination=AuditTermination(
            termination_event.quality,
            termination_event.declared_done_frame,
            termination_event.grace_end_frame,
            termination_event.declared_done_timestamp,
            termination_event.grace_end_timestamp,
        ),
        overlay=_overlay_summary(trace),
        capability=AuditCapabilitySummary(
            trace.capability_profile.integrity,
            available,
            contract_required,
            criterion_required,
            missing_contract,
            missing_criterion,
        ),
        criteria=criteria,
        failures=(),
        guarantee=_derive_guarantee(
            criteria,
            report.outcome_verdict,
            guarantee_evidence,
        ),
        acquisition_provenance=report.checker_acquisition_provenance,
        measurements=measurements,
    )
    envelope = replace(envelope, failures=_derive_failures(envelope))
    envelope.validate()
    return envelope


def build_compilation_rejection_record(
    error: ContractRouterError,
) -> CompilationRejectionRecord:
    if not isinstance(error, ContractRouterError):
        raise ValueError("compilation rejection requires ContractRouterError")
    error.audit.validate(require_selected=False)
    rejected = error.audit.attempts[-1]
    record = CompilationRejectionRecord(
        selection_key=error.audit.selection_key,
        router_version=error.audit.router_version,
        selection_audit_sha256=contract_selection_audit_sha256(error.audit),
        rejected_source=rejected.source_type,
        router_failure_code=error.code.value,
    )
    record.validate()
    return record


def _provenance_payload(value: ContractProvenanceIR) -> dict[str, Any]:
    return {
        "source_type": value.source_type.value,
        "source_id": value.source_id,
        "source_version": value.source_version,
        "source_digest": value.source_digest,
        "source_locator": value.source_locator,
        "selection_key": value.selection_key,
    }


def _acquisition_payload(value: Optional[CheckerAcquisitionProvenanceIR]) -> Any:
    if value is None:
        return None
    return {
        "contract_sha256": value.contract_sha256,
        "evidence": {
            "trace_id": value.evidence.trace_id,
            "trace_sha256": value.evidence.trace_sha256,
            "evidence_sha256": value.evidence.evidence_sha256,
            "frame_start": value.evidence.frame_start,
            "frame_end_exclusive": value.evidence.frame_end_exclusive,
        },
        "outcomes_sha256": value.outcomes_sha256,
        "provider_id": value.provider_id,
        "acquisition_version": value.acquisition_version,
        "provider_configuration_sha256": value.provider_configuration_sha256,
        "evidence_storage_sha256": value.evidence_storage_sha256,
    }


def audit_report_envelope_payload(envelope: AuditReportEnvelope) -> dict[str, Any]:
    envelope.validate()
    return {
        "schema_version": envelope.schema_version,
        "report_kind": envelope.report_kind,
        "mode": envelope.mode.value,
        "canonicalizer_version": envelope.canonicalizer_version,
        "contract": {
            "contract_id": envelope.contract.contract_id,
            "contract_sha256": envelope.contract.contract_sha256,
            "contract_source": envelope.contract.contract_source,
            "compiler_provenance": _provenance_payload(
                envelope.contract.compiler_provenance
            ),
            "selection_audit_sha256": envelope.contract.selection_audit_sha256,
        },
        "trace": {
            "trace_id": envelope.trace.trace_id,
            "trace_schema_version": envelope.trace.trace_schema_version,
            "event_log_envelope_schema_version": envelope.trace.event_log_envelope_schema_version,
            "trace_sha256": envelope.trace.trace_sha256,
            "source_trace_ref": envelope.trace.source_trace_ref,
            "replay_engine_version": envelope.trace.replay_engine_version,
        },
        "verdicts": {
            "verdict": envelope.verdict.value,
            "outcome": envelope.outcome_verdict.value,
            "process": (
                envelope.process_verdict.value if envelope.process_verdict else None
            ),
            "outcome_at_declared_done": (
                envelope.outcome_at_declared_done.value
                if envelope.outcome_at_declared_done
                else None
            ),
            "outcome_after_grace": (
                envelope.outcome_after_grace.value
                if envelope.outcome_after_grace
                else None
            ),
        },
        "termination": {
            "quality": envelope.termination.quality.value,
            "declared_done_frame": envelope.termination.declared_done_frame,
            "grace_end_frame": envelope.termination.grace_end_frame,
            "declared_done_timestamp": envelope.termination.declared_done_timestamp,
            "grace_end_timestamp": envelope.termination.grace_end_timestamp,
        },
        "overlay": {
            "status": envelope.overlay.status.value,
            "terminal_kinds": [item.value for item in envelope.overlay.terminal_kinds],
            "terminal_loading": envelope.overlay.terminal_loading,
        },
        "capability": {
            "trace_integrity": envelope.capability.trace_integrity.value,
            "available": [item.value for item in envelope.capability.available],
            "contract_required": [
                item.value for item in envelope.capability.contract_required
            ],
            "criterion_required": [
                item.value for item in envelope.capability.criterion_required
            ],
            "missing_contract": [
                item.value for item in envelope.capability.missing_contract
            ],
            "missing_criterion": [
                item.value for item in envelope.capability.missing_criterion
            ],
        },
        "criteria": [
            {
                "criterion_id": item.criterion_id,
                "dimension": item.dimension.value,
                "temporal_semantics": item.temporal_semantics.value,
                "required": item.required,
                "status": item.status.value,
                "evidence": [
                    {
                        "frame_index": pointer.frame_index,
                        "source": pointer.source,
                        "timestamp": pointer.timestamp,
                    }
                    for pointer in item.evidence
                ],
                "first_satisfied_frame": item.first_satisfied_frame,
                "last_evaluated_frame": item.last_evaluated_frame,
                "obscured_but_persistent": item.obscured_but_persistent,
            }
            for item in envelope.criteria
        ],
        "failures": [
            {
                "domain": item.domain.value,
                "code": item.code.value,
                "criterion_ids": list(item.criterion_ids),
            }
            for item in envelope.failures
        ],
        "guarantee": {
            "level": envelope.guarantee.level.value,
            "supporting_criterion_ids": list(
                envelope.guarantee.supporting_criterion_ids
            ),
            "supporting_evidence": [
                {
                    "kind": item.kind.value,
                    "source_ref": item.source_ref,
                    "evidence_sha256": item.evidence_sha256,
                }
                for item in envelope.guarantee.supporting_evidence
            ],
        },
        "acquisition_provenance": _acquisition_payload(envelope.acquisition_provenance),
        "measurements": {
            "latency_ms": envelope.measurements.latency_ms,
            "provider_calls": envelope.measurements.provider_calls,
            "model_calls": envelope.measurements.model_calls,
            "cost_amount": envelope.measurements.cost_amount,
            "cost_currency": envelope.measurements.cost_currency,
        },
    }


def _canonical_bytes(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def audit_report_envelope_sha256(envelope: AuditReportEnvelope) -> str:
    return hashlib.sha256(
        _canonical_bytes(audit_report_envelope_payload(envelope))
    ).hexdigest()


def compilation_rejection_payload(record: CompilationRejectionRecord) -> dict[str, Any]:
    record.validate()
    return {
        "schema_version": record.schema_version,
        "record_kind": record.record_kind,
        "selection_key": record.selection_key,
        "router_version": record.router_version,
        "selection_audit_sha256": record.selection_audit_sha256,
        "rejected_source": record.rejected_source.value,
        "router_failure_code": record.router_failure_code,
        "failure": {
            "domain": record.failure.domain.value,
            "code": record.failure.code.value,
            "criterion_ids": [],
        },
    }


def compilation_rejection_sha256(record: CompilationRejectionRecord) -> str:
    return hashlib.sha256(
        _canonical_bytes(compilation_rejection_payload(record))
    ).hexdigest()


def _expect_keys(value: Any, expected: set[str], context: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{context} must be an object")
    actual = set(value)
    if actual != expected:
        raise ValueError(
            f"{context} keys mismatch; missing={sorted(expected-actual)}, unexpected={sorted(actual-expected)}"
        )
    return value


def _strict_json_bytes(data: bytes) -> Mapping[str, Any]:
    if not isinstance(data, bytes):
        raise ValueError("audit envelope input must be bytes")

    def hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    try:
        loaded = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=hook,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON number: {value}")
            ),
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"audit envelope JSON is invalid: {exc}") from exc
    if not isinstance(loaded, Mapping):
        raise ValueError("audit envelope root must be an object")
    return loaded


def audit_report_envelope_from_json_bytes(data: bytes) -> AuditReportEnvelope:
    """Strictly decode a canonical envelope; unknown and duplicate fields fail closed."""

    root = _expect_keys(
        _strict_json_bytes(data),
        {
            "schema_version",
            "report_kind",
            "mode",
            "canonicalizer_version",
            "contract",
            "trace",
            "verdicts",
            "termination",
            "overlay",
            "capability",
            "criteria",
            "failures",
            "guarantee",
            "acquisition_provenance",
            "measurements",
        },
        "audit envelope",
    )
    contract = _expect_keys(
        root["contract"],
        {
            "contract_id",
            "contract_sha256",
            "contract_source",
            "compiler_provenance",
            "selection_audit_sha256",
        },
        "audit contract",
    )
    provenance = _expect_keys(
        contract["compiler_provenance"],
        {
            "source_type",
            "source_id",
            "source_version",
            "source_digest",
            "source_locator",
            "selection_key",
        },
        "compiler provenance",
    )
    trace = _expect_keys(
        root["trace"],
        {
            "trace_id",
            "trace_schema_version",
            "event_log_envelope_schema_version",
            "trace_sha256",
            "source_trace_ref",
            "replay_engine_version",
        },
        "audit trace",
    )
    verdicts = _expect_keys(
        root["verdicts"],
        {
            "verdict",
            "outcome",
            "process",
            "outcome_at_declared_done",
            "outcome_after_grace",
        },
        "audit verdicts",
    )
    termination = _expect_keys(
        root["termination"],
        {
            "quality",
            "declared_done_frame",
            "grace_end_frame",
            "declared_done_timestamp",
            "grace_end_timestamp",
        },
        "audit termination",
    )
    overlay = _expect_keys(
        root["overlay"],
        {"status", "terminal_kinds", "terminal_loading"},
        "audit overlay",
    )
    capability = _expect_keys(
        root["capability"],
        {
            "trace_integrity",
            "available",
            "contract_required",
            "criterion_required",
            "missing_contract",
            "missing_criterion",
        },
        "audit capability",
    )
    if not isinstance(root["criteria"], list) or not isinstance(root["failures"], list):
        raise ValueError("audit criteria and failures must be arrays")
    criteria = []
    for raw in root["criteria"]:
        item = _expect_keys(
            raw,
            {
                "criterion_id",
                "dimension",
                "temporal_semantics",
                "required",
                "status",
                "evidence",
                "first_satisfied_frame",
                "last_evaluated_frame",
                "obscured_but_persistent",
            },
            "audit criterion",
        )
        if not isinstance(item["evidence"], list):
            raise ValueError("audit criterion evidence must be an array")
        pointers = []
        for raw_pointer in item["evidence"]:
            pointer = _expect_keys(
                raw_pointer,
                {"frame_index", "source", "timestamp"},
                "audit evidence pointer",
            )
            pointers.append(
                AuditEvidencePointer(
                    pointer["frame_index"], pointer["source"], pointer["timestamp"]
                )
            )
        criteria.append(
            AuditCriterionRecord(
                item["criterion_id"],
                AuditDimension(item["dimension"]),
                TemporalSemantics(item["temporal_semantics"]),
                item["required"],
                CriterionStatus(item["status"]),
                tuple(pointers),
                item["first_satisfied_frame"],
                item["last_evaluated_frame"],
                item["obscured_but_persistent"],
            )
        )
    failures = []
    for raw in root["failures"]:
        item = _expect_keys(raw, {"domain", "code", "criterion_ids"}, "audit failure")
        if not isinstance(item["criterion_ids"], list):
            raise ValueError("audit failure criterion_ids must be an array")
        failures.append(
            AuditFailureRecord(
                FailureDomain(item["domain"]),
                FailureCode(item["code"]),
                tuple(item["criterion_ids"]),
            )
        )
    guarantee = _expect_keys(
        root["guarantee"],
        {"level", "supporting_criterion_ids", "supporting_evidence"},
        "audit guarantee",
    )
    if not isinstance(guarantee["supporting_criterion_ids"], list) or not isinstance(
        guarantee["supporting_evidence"], list
    ):
        raise ValueError("audit guarantee arrays are invalid")
    guarantee_evidence = []
    for raw in guarantee["supporting_evidence"]:
        item = _expect_keys(
            raw, {"kind", "source_ref", "evidence_sha256"}, "guarantee evidence"
        )
        guarantee_evidence.append(
            GuaranteeEvidenceIR(
                GuaranteeEvidenceKind(item["kind"]),
                item["source_ref"],
                item["evidence_sha256"],
            )
        )
    acquisition_raw = root["acquisition_provenance"]
    acquisition = None
    if acquisition_raw is not None:
        item = _expect_keys(
            acquisition_raw,
            {
                "contract_sha256",
                "evidence",
                "outcomes_sha256",
                "provider_id",
                "acquisition_version",
                "provider_configuration_sha256",
                "evidence_storage_sha256",
            },
            "acquisition provenance",
        )
        evidence = _expect_keys(
            item["evidence"],
            {
                "trace_id",
                "trace_sha256",
                "evidence_sha256",
                "frame_start",
                "frame_end_exclusive",
            },
            "acquisition evidence",
        )
        acquisition = CheckerAcquisitionProvenanceIR(
            item["contract_sha256"],
            CheckerEvidenceIdentityIR(
                evidence["trace_id"],
                evidence["trace_sha256"],
                evidence["evidence_sha256"],
                evidence["frame_start"],
                evidence["frame_end_exclusive"],
            ),
            item["outcomes_sha256"],
            item["provider_id"],
            item["acquisition_version"],
            item["provider_configuration_sha256"],
            item["evidence_storage_sha256"],
        )
    measurements = _expect_keys(
        root["measurements"],
        {"latency_ms", "provider_calls", "model_calls", "cost_amount", "cost_currency"},
        "audit measurements",
    )
    envelope = AuditReportEnvelope(
        contract=AuditContractIdentity(
            contract["contract_id"],
            contract["contract_sha256"],
            contract["contract_source"],
            ContractProvenanceIR(
                ContractSourceType(provenance["source_type"]),
                provenance["source_id"],
                provenance["source_version"],
                provenance["source_digest"],
                provenance["source_locator"],
                provenance["selection_key"],
            ),
            contract["selection_audit_sha256"],
        ),
        trace=AuditTraceIdentity(
            trace["trace_id"],
            trace["trace_schema_version"],
            trace["event_log_envelope_schema_version"],
            trace["trace_sha256"],
            trace["source_trace_ref"],
            trace["replay_engine_version"],
        ),
        verdict=RunVerdict(verdicts["verdict"]),
        outcome_verdict=RunVerdict(verdicts["outcome"]),
        process_verdict=(
            RunVerdict(verdicts["process"]) if verdicts["process"] is not None else None
        ),
        outcome_at_declared_done=(
            RunVerdict(verdicts["outcome_at_declared_done"])
            if verdicts["outcome_at_declared_done"] is not None
            else None
        ),
        outcome_after_grace=(
            RunVerdict(verdicts["outcome_after_grace"])
            if verdicts["outcome_after_grace"] is not None
            else None
        ),
        termination=AuditTermination(
            TerminationQuality(termination["quality"]),
            termination["declared_done_frame"],
            termination["grace_end_frame"],
            termination["declared_done_timestamp"],
            termination["grace_end_timestamp"],
        ),
        overlay=AuditOverlaySummary(
            OverlayStatus(overlay["status"]),
            tuple(OverlayKind(item) for item in overlay["terminal_kinds"]),
            overlay["terminal_loading"],
        ),
        capability=AuditCapabilitySummary(
            TraceIntegrity(capability["trace_integrity"]),
            tuple(EvidenceCapability(item) for item in capability["available"]),
            tuple(EvidenceCapability(item) for item in capability["contract_required"]),
            tuple(
                EvidenceCapability(item) for item in capability["criterion_required"]
            ),
            tuple(EvidenceCapability(item) for item in capability["missing_contract"]),
            tuple(EvidenceCapability(item) for item in capability["missing_criterion"]),
        ),
        criteria=tuple(criteria),
        failures=tuple(failures),
        guarantee=GuaranteeClaim(
            GuaranteeLevel(guarantee["level"]),
            tuple(guarantee["supporting_criterion_ids"]),
            tuple(guarantee_evidence),
        ),
        acquisition_provenance=acquisition,
        measurements=AuditMeasurements(
            measurements["latency_ms"],
            measurements["provider_calls"],
            measurements["model_calls"],
            measurements["cost_amount"],
            measurements["cost_currency"],
        ),
        mode=RunMode(root["mode"]),
        report_kind=root["report_kind"],
        canonicalizer_version=root["canonicalizer_version"],
        schema_version=root["schema_version"],
    )
    envelope.validate()
    return envelope


def audit_report_envelope_json_schema() -> dict[str, Any]:
    """Return the committed strict Draft 2020-12 transport schema."""

    sha = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
    nullable_sha = {"oneOf": [{"type": "null"}, sha]}
    nullable_number = {"type": ["number", "null"], "minimum": 0}
    nullable_integer = {"type": ["integer", "null"], "minimum": 0}
    nullable_string = {"type": ["string", "null"]}
    verdict = {"enum": [item.value for item in RunVerdict]}
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "audit_report_envelope_v1.schema.json",
        "title": "Harmony Evaluation Audit Report Envelope v1",
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema_version",
            "report_kind",
            "mode",
            "canonicalizer_version",
            "contract",
            "trace",
            "verdicts",
            "termination",
            "overlay",
            "capability",
            "criteria",
            "failures",
            "guarantee",
            "acquisition_provenance",
            "measurements",
        ],
        "properties": {
            "schema_version": {"const": AUDIT_ENVELOPE_SCHEMA_VERSION},
            "report_kind": {"const": "AUDIT_RUN"},
            "mode": {"const": RunMode.AUDIT_BENCHMARK.value},
            "canonicalizer_version": {"const": AUDIT_REPORT_CANONICALIZER_VERSION},
            "contract": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "contract_id",
                    "contract_sha256",
                    "contract_source",
                    "compiler_provenance",
                    "selection_audit_sha256",
                ],
                "properties": {
                    "contract_id": {"type": "string", "minLength": 1},
                    "contract_sha256": sha,
                    "contract_source": {"type": "string", "minLength": 1},
                    "selection_audit_sha256": nullable_sha,
                    "compiler_provenance": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": [
                            "source_type",
                            "source_id",
                            "source_version",
                            "source_digest",
                            "source_locator",
                            "selection_key",
                        ],
                        "properties": {
                            "source_type": {
                                "enum": [item.value for item in ContractSourceType]
                            },
                            "source_id": {"type": "string", "minLength": 1},
                            "source_version": {"type": "string", "minLength": 1},
                            "source_digest": sha,
                            "source_locator": {"type": "string", "minLength": 1},
                            "selection_key": {"type": "string", "minLength": 1},
                        },
                    },
                },
            },
            "trace": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "trace_id",
                    "trace_schema_version",
                    "event_log_envelope_schema_version",
                    "trace_sha256",
                    "source_trace_ref",
                    "replay_engine_version",
                ],
                "properties": {
                    "trace_id": {"type": "string", "minLength": 1},
                    "trace_schema_version": {"type": "string", "minLength": 1},
                    "event_log_envelope_schema_version": {
                        "const": EVENT_LOG_ENVELOPE_SCHEMA_VERSION
                    },
                    "trace_sha256": sha,
                    "source_trace_ref": nullable_string,
                    "replay_engine_version": {"const": REPLAY_ENGINE_VERSION},
                },
            },
            "verdicts": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "verdict",
                    "outcome",
                    "process",
                    "outcome_at_declared_done",
                    "outcome_after_grace",
                ],
                "properties": {
                    "verdict": verdict,
                    "outcome": verdict,
                    "process": {"oneOf": [{"type": "null"}, verdict]},
                    "outcome_at_declared_done": {"oneOf": [{"type": "null"}, verdict]},
                    "outcome_after_grace": {"oneOf": [{"type": "null"}, verdict]},
                },
            },
            "termination": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "quality",
                    "declared_done_frame",
                    "grace_end_frame",
                    "declared_done_timestamp",
                    "grace_end_timestamp",
                ],
                "properties": {
                    "quality": {"enum": [item.value for item in TerminationQuality]},
                    "declared_done_frame": nullable_integer,
                    "grace_end_frame": nullable_integer,
                    "declared_done_timestamp": nullable_number,
                    "grace_end_timestamp": nullable_number,
                },
            },
            "overlay": {
                "type": "object",
                "additionalProperties": False,
                "required": ["status", "terminal_kinds", "terminal_loading"],
                "properties": {
                    "status": {"enum": [item.value for item in OverlayStatus]},
                    "terminal_kinds": {
                        "type": "array",
                        "items": {"enum": [item.value for item in OverlayKind]},
                        "uniqueItems": True,
                    },
                    "terminal_loading": {"type": "boolean"},
                },
            },
            "capability": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "trace_integrity",
                    "available",
                    "contract_required",
                    "criterion_required",
                    "missing_contract",
                    "missing_criterion",
                ],
                "properties": {
                    "trace_integrity": {
                        "enum": [item.value for item in TraceIntegrity]
                    },
                    **{
                        name: {
                            "type": "array",
                            "items": {
                                "enum": [item.value for item in EvidenceCapability]
                            },
                            "uniqueItems": True,
                        }
                        for name in (
                            "available",
                            "contract_required",
                            "criterion_required",
                            "missing_contract",
                            "missing_criterion",
                        )
                    },
                },
            },
            "criteria": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "criterion_id",
                        "dimension",
                        "temporal_semantics",
                        "required",
                        "status",
                        "evidence",
                        "first_satisfied_frame",
                        "last_evaluated_frame",
                        "obscured_but_persistent",
                    ],
                    "properties": {
                        "criterion_id": {"type": "string", "minLength": 1},
                        "dimension": {"enum": [item.value for item in AuditDimension]},
                        "temporal_semantics": {
                            "enum": [item.value for item in TemporalSemantics]
                        },
                        "required": {"type": "boolean"},
                        "status": {"enum": [item.value for item in CriterionStatus]},
                        "evidence": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["frame_index", "source", "timestamp"],
                                "properties": {
                                    "frame_index": {"type": "integer", "minimum": 0},
                                    "source": {"type": "string", "minLength": 1},
                                    "timestamp": nullable_number,
                                },
                            },
                        },
                        "first_satisfied_frame": nullable_integer,
                        "last_evaluated_frame": nullable_integer,
                        "obscured_but_persistent": {"type": "boolean"},
                    },
                },
            },
            "failures": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["domain", "code", "criterion_ids"],
                    "properties": {
                        "domain": {
                            "enum": [
                                item.value
                                for item in FailureDomain
                                if item is not FailureDomain.COMPILATION
                            ]
                        },
                        "code": {
                            "enum": [
                                item.value
                                for item in FailureCode
                                if item is not FailureCode.COMPILER_REJECTED
                            ]
                        },
                        "criterion_ids": {
                            "type": "array",
                            "items": {"type": "string", "minLength": 1},
                            "uniqueItems": True,
                        },
                    },
                },
            },
            "guarantee": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "level",
                    "supporting_criterion_ids",
                    "supporting_evidence",
                ],
                "properties": {
                    "level": {"enum": [item.value for item in GuaranteeLevel]},
                    "supporting_criterion_ids": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1},
                        "uniqueItems": True,
                    },
                    "supporting_evidence": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["kind", "source_ref", "evidence_sha256"],
                            "properties": {
                                "kind": {
                                    "enum": [
                                        item.value for item in GuaranteeEvidenceKind
                                    ]
                                },
                                "source_ref": {"type": "string", "minLength": 1},
                                "evidence_sha256": sha,
                            },
                        },
                    },
                },
            },
            "acquisition_provenance": {
                "oneOf": [
                    {"type": "null"},
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "required": [
                            "contract_sha256",
                            "evidence",
                            "outcomes_sha256",
                            "provider_id",
                            "acquisition_version",
                            "provider_configuration_sha256",
                            "evidence_storage_sha256",
                        ],
                        "properties": {
                            "contract_sha256": sha,
                            "evidence": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": [
                                    "trace_id",
                                    "trace_sha256",
                                    "evidence_sha256",
                                    "frame_start",
                                    "frame_end_exclusive",
                                ],
                                "properties": {
                                    "trace_id": {"type": "string", "minLength": 1},
                                    "trace_sha256": sha,
                                    "evidence_sha256": sha,
                                    "frame_start": {"type": "integer", "minimum": 0},
                                    "frame_end_exclusive": {
                                        "type": "integer",
                                        "minimum": 0,
                                    },
                                },
                            },
                            "outcomes_sha256": sha,
                            "provider_id": {"type": "string", "minLength": 1},
                            "acquisition_version": {"type": "string", "minLength": 1},
                            "provider_configuration_sha256": nullable_sha,
                            "evidence_storage_sha256": nullable_sha,
                        },
                    },
                ]
            },
            "measurements": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "latency_ms",
                    "provider_calls",
                    "model_calls",
                    "cost_amount",
                    "cost_currency",
                ],
                "properties": {
                    "latency_ms": nullable_number,
                    "provider_calls": nullable_integer,
                    "model_calls": nullable_integer,
                    "cost_amount": nullable_number,
                    "cost_currency": nullable_string,
                },
            },
        },
    }


__all__ = [
    name
    for name in globals()
    if name.startswith("Audit")
    or name.startswith("Guarantee")
    or name.startswith("Failure")
    or name.startswith("Compilation")
    or name.startswith("AUDIT_")
    or name.startswith("build_")
    or name.startswith("audit_report_")
    or name.startswith("compilation_rejection_")
]
