"""Strict local protocol for intercepting candidate ``done(success)`` events.

The online Guardrail is deliberately not an Audit authority.  It evaluates an
observable prefix with the existing replay kernel, records interventions in a
separate fact chain, and leaves final benchmark adjudication to a freshly
projected ``AUDIT_BENCHMARK`` trace.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Iterable, Mapping, Optional, Tuple

from .audit_envelope import AuditReportEnvelope, audit_report_envelope_sha256
from .event_log import (
    CriterionObservationEvent,
    DurableEventTrace,
    contract_sha256,
    event_trace_sha256,
)
from .guardrail import (
    CriterionStateSnapshot,
    criterion_oscillation_counts,
    observable_state_corruptions,
    state_regressions,
)
from .models import (
    ContractIR,
    CriterionResult,
    CriterionStatus,
    RunMode,
    RunVerdict,
    TemporalSemantics,
)
from .replay import replay_event_trace


HARD_MAX_INTERVENTIONS = 3
GUARDRAIL_POLICY_SCHEMA_VERSION = "harmony-eval-guardrail-policy-v1"
GUARDRAIL_FEEDBACK_SCHEMA_VERSION = "harmony-eval-guardrail-feedback-v1"
GUARDRAIL_TRACE_SCHEMA_VERSION = "harmony-eval-guardrail-trace-v1"
GUARDRAIL_AB_REPORT_SCHEMA_VERSION = "harmony-eval-guardrail-ab-report-v1"
GUARDRAIL_CANONICALIZER_VERSION = "harmony-eval-guardrail-canonical-json-v1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class GuardrailCandidateType(str, Enum):
    DONE_SUCCESS = "DONE_SUCCESS"


class GuardrailDecision(str, Enum):
    ALLOW_DONE = "ALLOW_DONE"
    INTERVENE_CONTINUE = "INTERVENE_CONTINUE"
    ABSTAIN = "ABSTAIN"
    FORCE_STOP = "FORCE_STOP"


class GuardrailReasonCode(str, Enum):
    AUDIT_PREFIX_PASS = "AUDIT_PREFIX_PASS"
    HIGH_CONFIDENCE_CRITERION_VIOLATION = "HIGH_CONFIDENCE_CRITERION_VIOLATION"
    EVIDENCE_UNKNOWN = "EVIDENCE_UNKNOWN"
    SOURCE_EVIDENCE_MISSING = "SOURCE_EVIDENCE_MISSING"
    CAPABILITY_UNSUPPORTED = "CAPABILITY_UNSUPPORTED"
    TRACE_INVALID = "TRACE_INVALID"
    MAX_INTERVENTIONS_REACHED = "MAX_INTERVENTIONS_REACHED"
    STATE_REGRESSION_DETECTED = "STATE_REGRESSION_DETECTED"
    OBSERVABLE_STATE_CORRUPTION_DETECTED = "OBSERVABLE_STATE_CORRUPTION_DETECTED"
    CRITERION_OSCILLATION_DETECTED = "CRITERION_OSCILLATION_DETECTED"
    STEP_BUDGET_EXHAUSTED = "STEP_BUDGET_EXHAUSTED"
    TIME_BUDGET_EXHAUSTED = "TIME_BUDGET_EXHAUSTED"
    TOKEN_BUDGET_EXHAUSTED = "TOKEN_BUDGET_EXHAUSTED"
    MODEL_CALL_BUDGET_EXHAUSTED = "MODEL_CALL_BUDGET_EXHAUSTED"


class GuardrailActionRequired(str, Enum):
    CONTINUE = "CONTINUE"
    TERMINATE = "TERMINATE"
    NONE = "NONE"


class GuardrailAbstainAction(str, Enum):
    ALLOW_DONE_UNJUDGED = "ALLOW_DONE_UNJUDGED"
    FORCE_STOP_UNJUDGED = "FORCE_STOP_UNJUDGED"
    ESCALATE_HUMAN = "ESCALATE_HUMAN"


class GuardrailSafetyAction(str, Enum):
    WARN = "WARN"
    FORCE_STOP = "FORCE_STOP"


class GuardrailOperationalStatus(str, Enum):
    RUNNING = "RUNNING"
    DONE_ALLOWED = "DONE_ALLOWED"
    DONE_ALLOWED_UNJUDGED = "DONE_ALLOWED_UNJUDGED"
    FAIL_MAX_INTERVENTIONS_REACHED = "FAIL_MAX_INTERVENTIONS_REACHED"
    FAIL_STATE_REGRESSION = "FAIL_STATE_REGRESSION"
    FAIL_CRITERION_OSCILLATION = "FAIL_CRITERION_OSCILLATION"
    FAIL_STEP_BUDGET = "FAIL_STEP_BUDGET"
    FAIL_TIME_BUDGET = "FAIL_TIME_BUDGET"
    FAIL_TOKEN_BUDGET = "FAIL_TOKEN_BUDGET"
    FAIL_MODEL_CALL_BUDGET = "FAIL_MODEL_CALL_BUDGET"
    FORCE_STOP_UNJUDGED = "FORCE_STOP_UNJUDGED"
    ESCALATED_HUMAN = "ESCALATED_HUMAN"


class GuardrailTraceEventKind(str, Enum):
    CANDIDATE_OBSERVED = "CANDIDATE_OBSERVED"
    POST_INTERVENTION_SNAPSHOT = "POST_INTERVENTION_SNAPSHOT"
    STATE_REGRESSION_DETECTED = "STATE_REGRESSION_DETECTED"
    OBSERVABLE_STATE_CORRUPTION_DETECTED = "OBSERVABLE_STATE_CORRUPTION_DETECTED"
    CRITERION_OSCILLATION_DETECTED = "CRITERION_OSCILLATION_DETECTED"
    INTERVENTION_ISSUED = "INTERVENTION_ISSUED"
    ABSTAIN_RECORDED = "ABSTAIN_RECORDED"
    FORCED_STOP = "FORCED_STOP"
    DONE_ALLOWED = "DONE_ALLOWED"
    HUMAN_ESCALATION = "HUMAN_ESCALATION"


def _canonical_bytes(value: Any) -> bytes:
    try:
        rendered = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"value is not canonical JSON: {exc}") from exc
    return rendered.encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _validate_sha(value: str, context: str) -> None:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError(f"{context} must be a lowercase SHA-256")


def _validate_id(value: str, context: str) -> None:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{context} must be a canonical non-empty string")


def _validate_non_negative_int(value: Any, context: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{context} must be a non-negative integer")


def _validate_optional_budget(value: Optional[int], context: str) -> None:
    if value is not None:
        _validate_non_negative_int(value, context)


def _validate_optional_number(value: Optional[float], context: str) -> None:
    if value is not None and (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        or value < 0
    ):
        raise ValueError(f"{context} must be a finite non-negative number or null")


def _sorted_unique(values: Tuple[str, ...], context: str) -> None:
    if any(not isinstance(value, str) or not value for value in values):
        raise ValueError(f"{context} must contain non-empty strings")
    if values != tuple(sorted(set(values))):
        raise ValueError(f"{context} must be sorted and unique")


@dataclass(frozen=True)
class GuardrailPolicy:
    allowlist_id: str
    allowlist_sha256: str
    allowed_contract_sha256s: Tuple[str, ...]
    max_interventions: int
    max_extra_steps: int
    logical_deadline_seconds: Optional[float]
    max_tokens: Optional[int]
    max_model_calls: Optional[int]
    protected_criteria: Tuple[str, ...]
    regression_action: GuardrailSafetyAction
    max_oscillations_per_criterion: Optional[int]
    oscillation_action: GuardrailSafetyAction
    on_abstain: GuardrailAbstainAction
    track_observable_state_corruption: bool = False
    enforce_process_obligations: bool = False
    schema_version: str = GUARDRAIL_POLICY_SCHEMA_VERSION

    def validate(self) -> None:
        if self.schema_version != GUARDRAIL_POLICY_SCHEMA_VERSION:
            raise ValueError("unsupported Guardrail policy schema")
        _validate_id(self.allowlist_id, "allowlist_id")
        _validate_sha(self.allowlist_sha256, "allowlist_sha256")
        if not isinstance(self.allowed_contract_sha256s, tuple):
            raise ValueError("allowed_contract_sha256s must be immutable")
        _sorted_unique(self.allowed_contract_sha256s, "allowed_contract_sha256s")
        if not self.allowed_contract_sha256s:
            raise ValueError("allowed_contract_sha256s must not be empty")
        for value in self.allowed_contract_sha256s:
            _validate_sha(value, "allowed contract identity")
        _validate_non_negative_int(self.max_interventions, "max_interventions")
        if self.max_interventions > HARD_MAX_INTERVENTIONS:
            raise ValueError(
                f"max_interventions exceeds hard ceiling {HARD_MAX_INTERVENTIONS}"
            )
        _validate_non_negative_int(self.max_extra_steps, "max_extra_steps")
        _validate_optional_number(
            self.logical_deadline_seconds, "logical_deadline_seconds"
        )
        _validate_optional_budget(self.max_tokens, "max_tokens")
        _validate_optional_budget(self.max_model_calls, "max_model_calls")
        if not isinstance(self.protected_criteria, tuple):
            raise ValueError("protected_criteria must be immutable")
        _sorted_unique(self.protected_criteria, "protected_criteria")
        if not isinstance(self.regression_action, GuardrailSafetyAction):
            raise ValueError("regression_action is invalid")
        if self.max_oscillations_per_criterion is not None:
            if (
                not isinstance(self.max_oscillations_per_criterion, int)
                or isinstance(self.max_oscillations_per_criterion, bool)
                or self.max_oscillations_per_criterion < 1
            ):
                raise ValueError(
                    "max_oscillations_per_criterion must be a positive integer or null"
                )
        if not isinstance(self.oscillation_action, GuardrailSafetyAction):
            raise ValueError("oscillation_action is invalid")
        if not isinstance(self.on_abstain, GuardrailAbstainAction):
            raise ValueError("on_abstain is invalid")
        if not isinstance(self.track_observable_state_corruption, bool):
            raise ValueError("track_observable_state_corruption must be boolean")
        if not isinstance(self.enforce_process_obligations, bool):
            raise ValueError("enforce_process_obligations must be boolean")


def guardrail_policy_payload(policy: GuardrailPolicy) -> dict[str, Any]:
    policy.validate()
    payload = {
        "schema_version": policy.schema_version,
        "allowlist_id": policy.allowlist_id,
        "allowlist_sha256": policy.allowlist_sha256,
        "allowed_contract_sha256s": list(policy.allowed_contract_sha256s),
        "max_interventions": policy.max_interventions,
        "max_extra_steps": policy.max_extra_steps,
        "logical_deadline_seconds": policy.logical_deadline_seconds,
        "max_tokens": policy.max_tokens,
        "max_model_calls": policy.max_model_calls,
        "protected_criteria": list(policy.protected_criteria),
        "regression_action": policy.regression_action.value,
        "max_oscillations_per_criterion": policy.max_oscillations_per_criterion,
        "oscillation_action": policy.oscillation_action.value,
        "on_abstain": policy.on_abstain.value,
    }
    # Preserve the hashes of the frozen pre-Pivot Mock policies.  Live black-box
    # policies opt in explicitly, and the opt-in is then hash-bound.
    if policy.track_observable_state_corruption:
        payload["track_observable_state_corruption"] = True
    if policy.enforce_process_obligations:
        payload["enforce_process_obligations"] = True
    return payload


def guardrail_policy_sha256(policy: GuardrailPolicy) -> str:
    return _digest(guardrail_policy_payload(policy))


@dataclass(frozen=True)
class GuardrailEvidencePointer:
    frame_index: int
    source: str
    timestamp: Optional[float]

    def validate(self) -> None:
        _validate_non_negative_int(self.frame_index, "evidence frame_index")
        _validate_id(self.source, "evidence source")
        _validate_optional_number(self.timestamp, "evidence timestamp")


@dataclass(frozen=True)
class CriterionStateRecord:
    criterion_id: str
    temporal_semantics: TemporalSemantics
    status: CriterionStatus
    required: bool
    protected: bool
    evidence: Tuple[GuardrailEvidencePointer, ...] = ()

    def validate(self) -> None:
        _validate_id(self.criterion_id, "criterion_id")
        if not isinstance(self.temporal_semantics, TemporalSemantics):
            raise ValueError("criterion temporal_semantics is invalid")
        if not isinstance(self.status, CriterionStatus):
            raise ValueError("criterion status is invalid")
        if not isinstance(self.required, bool) or not isinstance(self.protected, bool):
            raise ValueError("criterion required/protected flags must be boolean")
        if not isinstance(self.evidence, tuple):
            raise ValueError("criterion evidence must be immutable")
        for pointer in self.evidence:
            if not isinstance(pointer, GuardrailEvidencePointer):
                raise ValueError("criterion evidence pointer is invalid")
            pointer.validate()


def _criterion_state_payload(value: CriterionStateRecord) -> dict[str, Any]:
    value.validate()
    return {
        "criterion_id": value.criterion_id,
        "temporal_semantics": value.temporal_semantics.value,
        "status": value.status.value,
        "required": value.required,
        "protected": value.protected,
        "evidence": [
            {
                "frame_index": pointer.frame_index,
                "source": pointer.source,
                "timestamp": pointer.timestamp,
            }
            for pointer in value.evidence
        ],
    }


def criterion_state_vector_sha256(
    values: Tuple[CriterionStateRecord, ...],
) -> str:
    _validate_state_vector(values)
    return _digest([_criterion_state_payload(value) for value in values])


def _validate_state_vector(values: Tuple[CriterionStateRecord, ...]) -> None:
    if not isinstance(values, tuple) or not values:
        raise ValueError("criterion_state_vector must be a non-empty tuple")
    for value in values:
        if not isinstance(value, CriterionStateRecord):
            raise ValueError("criterion_state_vector contains an invalid record")
        value.validate()
    ids = tuple(value.criterion_id for value in values)
    if ids != tuple(sorted(set(ids))):
        raise ValueError("criterion_state_vector must be sorted with unique ids")


@dataclass(frozen=True)
class DoneCandidate:
    run_id: str
    session_id: str
    candidate_ordinal: int
    step_index: int
    frame_index: int
    timestamp: float
    contract_sha256: str
    observable_prefix_sha256: str
    tokens_used: Optional[int] = None
    model_calls_used: Optional[int] = None
    candidate_type: GuardrailCandidateType = GuardrailCandidateType.DONE_SUCCESS

    def validate(self) -> None:
        _validate_id(self.run_id, "candidate run_id")
        _validate_id(self.session_id, "candidate session_id")
        if (
            not isinstance(self.candidate_ordinal, int)
            or isinstance(self.candidate_ordinal, bool)
            or self.candidate_ordinal < 1
        ):
            raise ValueError("candidate_ordinal must be a positive integer")
        _validate_non_negative_int(self.step_index, "candidate step_index")
        _validate_non_negative_int(self.frame_index, "candidate frame_index")
        if self.timestamp is None:
            raise ValueError("candidate timestamp must not be null")
        _validate_optional_number(self.timestamp, "candidate timestamp")
        _validate_sha(self.contract_sha256, "candidate contract_sha256")
        _validate_sha(
            self.observable_prefix_sha256, "candidate observable_prefix_sha256"
        )
        _validate_optional_budget(self.tokens_used, "candidate tokens_used")
        _validate_optional_budget(self.model_calls_used, "candidate model_calls_used")
        if self.candidate_type is not GuardrailCandidateType.DONE_SUCCESS:
            raise ValueError("only DONE_SUCCESS candidates may enter the interceptor")


@dataclass(frozen=True)
class StructuredGuardrailFeedback:
    status: CriterionStatus
    decision: GuardrailDecision
    reason_code: GuardrailReasonCode
    intervention_index: int
    failed_criteria: Tuple[str, ...]
    criterion_state_vector: Tuple[CriterionStateRecord, ...]
    evidence_snapshot_sha256: str
    action_required: GuardrailActionRequired
    contract_sha256: str
    observable_prefix_sha256: str
    policy_sha256: str
    schema_version: str = GUARDRAIL_FEEDBACK_SCHEMA_VERSION

    def validate(self) -> None:
        if self.schema_version != GUARDRAIL_FEEDBACK_SCHEMA_VERSION:
            raise ValueError("unsupported Guardrail feedback schema")
        if self.status is not CriterionStatus.VIOLATED:
            raise ValueError("Guardrail feedback status must be VIOLATED")
        if self.decision is not GuardrailDecision.INTERVENE_CONTINUE:
            raise ValueError("feedback exists only for INTERVENE_CONTINUE")
        if (
            self.reason_code
            is not GuardrailReasonCode.HIGH_CONFIDENCE_CRITERION_VIOLATION
        ):
            raise ValueError("feedback reason must be a high-confidence violation")
        if (
            not isinstance(self.intervention_index, int)
            or isinstance(self.intervention_index, bool)
            or self.intervention_index < 1
            or self.intervention_index > HARD_MAX_INTERVENTIONS
        ):
            raise ValueError("feedback intervention_index is out of bounds")
        _sorted_unique(self.failed_criteria, "failed_criteria")
        if not self.failed_criteria:
            raise ValueError("feedback must identify failed criteria")
        _validate_state_vector(self.criterion_state_vector)
        states = {item.criterion_id: item for item in self.criterion_state_vector}
        if any(
            criterion_id not in states
            or not states[criterion_id].required
            or states[criterion_id].status is not CriterionStatus.VIOLATED
            for criterion_id in self.failed_criteria
        ):
            raise ValueError("failed_criteria must be required VIOLATED criteria")
        expected_snapshot = criterion_state_vector_sha256(self.criterion_state_vector)
        if not hmac.compare_digest(self.evidence_snapshot_sha256, expected_snapshot):
            raise ValueError("feedback evidence snapshot hash drift")
        if self.action_required is not GuardrailActionRequired.CONTINUE:
            raise ValueError("Guardrail feedback action must be CONTINUE")
        for name, value in (
            ("contract_sha256", self.contract_sha256),
            ("observable_prefix_sha256", self.observable_prefix_sha256),
            ("policy_sha256", self.policy_sha256),
        ):
            _validate_sha(value, f"feedback {name}")


def guardrail_feedback_payload(
    feedback: StructuredGuardrailFeedback,
) -> dict[str, Any]:
    feedback.validate()
    return {
        "schema_version": feedback.schema_version,
        "status": feedback.status.value,
        "decision": feedback.decision.value,
        "reason_code": feedback.reason_code.value,
        "intervention_index": feedback.intervention_index,
        "failed_criteria": list(feedback.failed_criteria),
        "criterion_state_vector": [
            _criterion_state_payload(item) for item in feedback.criterion_state_vector
        ],
        "evidence_snapshot_sha256": feedback.evidence_snapshot_sha256,
        "action_required": feedback.action_required.value,
        "contract_sha256": feedback.contract_sha256,
        "observable_prefix_sha256": feedback.observable_prefix_sha256,
        "policy_sha256": feedback.policy_sha256,
    }


def guardrail_feedback_sha256(feedback: StructuredGuardrailFeedback) -> str:
    return _digest(guardrail_feedback_payload(feedback))


@dataclass(frozen=True)
class GuardrailTraceEvent:
    event_ordinal: int
    event_kind: GuardrailTraceEventKind
    candidate_ordinal: int
    candidate_step_index: int
    candidate_frame_index: int
    candidate_timestamp: float
    intervention_index: int
    decision: Optional[GuardrailDecision]
    reason_code: Optional[GuardrailReasonCode]
    operational_status: GuardrailOperationalStatus
    contract_sha256: str
    observable_prefix_sha256: str
    policy_sha256: str
    evidence_snapshot_sha256: str
    criterion_ids: Tuple[str, ...] = ()
    protected_criterion_ids: Tuple[str, ...] = ()
    feedback: Optional[StructuredGuardrailFeedback] = None

    def validate(self) -> None:
        _validate_non_negative_int(self.event_ordinal, "trace event_ordinal")
        if not isinstance(self.event_kind, GuardrailTraceEventKind):
            raise ValueError("trace event_kind is invalid")
        if (
            not isinstance(self.candidate_ordinal, int)
            or isinstance(self.candidate_ordinal, bool)
            or self.candidate_ordinal < 1
        ):
            raise ValueError("trace candidate_ordinal must be positive")
        _validate_non_negative_int(
            self.candidate_step_index, "trace candidate_step_index"
        )
        _validate_non_negative_int(
            self.candidate_frame_index, "trace candidate_frame_index"
        )
        if self.candidate_timestamp is None:
            raise ValueError("trace candidate_timestamp must not be null")
        _validate_optional_number(self.candidate_timestamp, "trace candidate_timestamp")
        _validate_non_negative_int(self.intervention_index, "trace intervention_index")
        if self.intervention_index > HARD_MAX_INTERVENTIONS:
            raise ValueError("trace intervention_index exceeds hard ceiling")
        if self.decision is not None and not isinstance(
            self.decision, GuardrailDecision
        ):
            raise ValueError("trace decision is invalid")
        if self.reason_code is not None and not isinstance(
            self.reason_code, GuardrailReasonCode
        ):
            raise ValueError("trace reason_code is invalid")
        if not isinstance(self.operational_status, GuardrailOperationalStatus):
            raise ValueError("trace operational_status is invalid")
        for name, value in (
            ("contract_sha256", self.contract_sha256),
            ("observable_prefix_sha256", self.observable_prefix_sha256),
            ("policy_sha256", self.policy_sha256),
            ("evidence_snapshot_sha256", self.evidence_snapshot_sha256),
        ):
            _validate_sha(value, f"trace {name}")
        _sorted_unique(self.criterion_ids, "trace criterion_ids")
        _sorted_unique(self.protected_criterion_ids, "trace protected_criterion_ids")
        if not set(self.protected_criterion_ids).issubset(self.criterion_ids):
            raise ValueError("protected criterion ids must be a criterion_ids subset")
        if self.event_kind is GuardrailTraceEventKind.INTERVENTION_ISSUED:
            if self.feedback is None:
                raise ValueError("intervention event requires feedback")
            self.feedback.validate()
            if self.decision is not GuardrailDecision.INTERVENE_CONTINUE:
                raise ValueError("intervention event decision mismatch")
            if (
                self.feedback.intervention_index != self.intervention_index
                or not hmac.compare_digest(
                    self.feedback.contract_sha256, self.contract_sha256
                )
                or not hmac.compare_digest(
                    self.feedback.observable_prefix_sha256,
                    self.observable_prefix_sha256,
                )
                or not hmac.compare_digest(
                    self.feedback.policy_sha256, self.policy_sha256
                )
                or not hmac.compare_digest(
                    self.feedback.evidence_snapshot_sha256,
                    self.evidence_snapshot_sha256,
                )
            ):
                raise ValueError("intervention feedback identity/hash drift")
        elif self.feedback is not None:
            raise ValueError("feedback is allowed only on intervention events")


def _trace_event_payload(value: GuardrailTraceEvent) -> dict[str, Any]:
    value.validate()
    return {
        "event_ordinal": value.event_ordinal,
        "event_kind": value.event_kind.value,
        "candidate_ordinal": value.candidate_ordinal,
        "candidate_step_index": value.candidate_step_index,
        "candidate_frame_index": value.candidate_frame_index,
        "candidate_timestamp": value.candidate_timestamp,
        "intervention_index": value.intervention_index,
        "decision": value.decision.value if value.decision else None,
        "reason_code": value.reason_code.value if value.reason_code else None,
        "operational_status": value.operational_status.value,
        "contract_sha256": value.contract_sha256,
        "observable_prefix_sha256": value.observable_prefix_sha256,
        "policy_sha256": value.policy_sha256,
        "evidence_snapshot_sha256": value.evidence_snapshot_sha256,
        "criterion_ids": list(value.criterion_ids),
        "protected_criterion_ids": list(value.protected_criterion_ids),
        "feedback": (
            guardrail_feedback_payload(value.feedback) if value.feedback else None
        ),
    }


@dataclass(frozen=True)
class GuardrailTrace:
    trace_id: str
    run_id: str
    session_id: str
    contract_sha256: str
    policy_sha256: str
    events: Tuple[GuardrailTraceEvent, ...]
    schema_version: str = GUARDRAIL_TRACE_SCHEMA_VERSION

    def validate(self) -> None:
        if self.schema_version != GUARDRAIL_TRACE_SCHEMA_VERSION:
            raise ValueError("unsupported Guardrail trace schema")
        _validate_id(self.trace_id, "Guardrail trace_id")
        _validate_id(self.run_id, "Guardrail run_id")
        _validate_id(self.session_id, "Guardrail session_id")
        _validate_sha(self.contract_sha256, "Guardrail trace contract_sha256")
        _validate_sha(self.policy_sha256, "Guardrail trace policy_sha256")
        if not isinstance(self.events, tuple):
            raise ValueError("Guardrail events must be immutable")
        previous_candidate = 0
        current_candidate = 0
        current_candidate_facts: Optional[tuple[int, int, float]] = None
        previous_candidate_timestamp = -1.0
        intervention_count = 0
        terminal_seen = False
        terminal_kinds = {
            GuardrailTraceEventKind.FORCED_STOP,
            GuardrailTraceEventKind.DONE_ALLOWED,
            GuardrailTraceEventKind.HUMAN_ESCALATION,
        }
        prior_event_kind: Optional[GuardrailTraceEventKind] = None
        for expected, event in enumerate(self.events):
            if not isinstance(event, GuardrailTraceEvent):
                raise ValueError("Guardrail trace contains an invalid event")
            event.validate()
            if event.event_ordinal != expected:
                raise ValueError("Guardrail event ordinals must be contiguous")
            if event.candidate_ordinal < previous_candidate:
                raise ValueError("Guardrail candidate ordinals must be monotonic")
            if event.event_kind is GuardrailTraceEventKind.CANDIDATE_OBSERVED:
                if (
                    current_candidate > 0
                    and prior_event_kind
                    is not GuardrailTraceEventKind.INTERVENTION_ISSUED
                ):
                    raise ValueError(
                        "a new Guardrail candidate requires a prior intervention"
                    )
                if event.candidate_ordinal != current_candidate + 1:
                    raise ValueError(
                        "Guardrail candidate observations must be contiguous"
                    )
                if event.candidate_timestamp < previous_candidate_timestamp:
                    raise ValueError("Guardrail candidate timestamps must be monotonic")
                current_candidate = event.candidate_ordinal
                previous_candidate_timestamp = event.candidate_timestamp
                current_candidate_facts = (
                    event.candidate_step_index,
                    event.candidate_frame_index,
                    event.candidate_timestamp,
                )
            elif event.candidate_ordinal != current_candidate:
                raise ValueError(
                    "Guardrail candidate subevents require a preceding observation"
                )
            elif current_candidate_facts != (
                event.candidate_step_index,
                event.candidate_frame_index,
                event.candidate_timestamp,
            ):
                raise ValueError("Guardrail candidate fact drift within event group")
            if event.event_kind is GuardrailTraceEventKind.INTERVENTION_ISSUED:
                if event.intervention_index != intervention_count + 1:
                    raise ValueError(
                        "Guardrail intervention indices must be contiguous"
                    )
                intervention_count = event.intervention_index
            elif event.intervention_index != intervention_count:
                raise ValueError("Guardrail event intervention index drift")
            if terminal_seen:
                raise ValueError("Guardrail trace cannot contain events after terminal")
            if event.event_kind in terminal_kinds:
                terminal_seen = True
            if not hmac.compare_digest(event.contract_sha256, self.contract_sha256):
                raise ValueError("Guardrail event Contract hash drift")
            if not hmac.compare_digest(event.policy_sha256, self.policy_sha256):
                raise ValueError("Guardrail event policy hash drift")
            previous_candidate = event.candidate_ordinal
            prior_event_kind = event.event_kind
        if self.events and prior_event_kind not in terminal_kinds | {
            GuardrailTraceEventKind.INTERVENTION_ISSUED
        }:
            raise ValueError(
                "Guardrail trace must end at an intervention or terminal event"
            )


def guardrail_trace_payload(trace: GuardrailTrace) -> dict[str, Any]:
    trace.validate()
    return {
        "schema_version": trace.schema_version,
        "trace_id": trace.trace_id,
        "run_id": trace.run_id,
        "session_id": trace.session_id,
        "contract_sha256": trace.contract_sha256,
        "policy_sha256": trace.policy_sha256,
        "events": [_trace_event_payload(event) for event in trace.events],
    }


def guardrail_trace_sha256(trace: GuardrailTrace) -> str:
    return _digest(guardrail_trace_payload(trace))


@dataclass(frozen=True)
class GuardrailDecisionRecord:
    decision: GuardrailDecision
    reason_code: GuardrailReasonCode
    operational_status: GuardrailOperationalStatus
    host_action: Optional[GuardrailAbstainAction]
    feedback: Optional[StructuredGuardrailFeedback]
    criterion_state_vector: Tuple[CriterionStateRecord, ...]

    def validate(self) -> None:
        if not isinstance(self.decision, GuardrailDecision):
            raise ValueError("decision record decision is invalid")
        if not isinstance(self.reason_code, GuardrailReasonCode):
            raise ValueError("decision record reason is invalid")
        if not isinstance(self.operational_status, GuardrailOperationalStatus):
            raise ValueError("decision record operational status is invalid")
        _validate_state_vector(self.criterion_state_vector)
        if self.decision is GuardrailDecision.INTERVENE_CONTINUE:
            if self.feedback is None or self.host_action is not None:
                raise ValueError("intervention decision requires feedback only")
            self.feedback.validate()
        elif self.feedback is not None:
            raise ValueError("non-intervention decisions cannot contain feedback")
        if self.decision is GuardrailDecision.ABSTAIN:
            if not isinstance(self.host_action, GuardrailAbstainAction):
                raise ValueError("ABSTAIN requires a frozen host action")
        elif self.host_action is not None:
            raise ValueError("host_action is valid only for ABSTAIN")


@dataclass(frozen=True)
class GuardrailExecutionResult:
    operational_status: GuardrailOperationalStatus
    intervention_count: int
    extra_steps: int
    tokens_used: Optional[int]
    model_calls_used: Optional[int]
    forced_stop_reason: Optional[GuardrailReasonCode]
    trace: GuardrailTrace
    final_observable_trace_sha256: Optional[str]

    def validate(self) -> None:
        if not isinstance(self.operational_status, GuardrailOperationalStatus):
            raise ValueError("execution operational status is invalid")
        _validate_non_negative_int(self.intervention_count, "intervention_count")
        if self.intervention_count > HARD_MAX_INTERVENTIONS:
            raise ValueError("execution exceeded hard intervention ceiling")
        _validate_non_negative_int(self.extra_steps, "extra_steps")
        _validate_optional_budget(self.tokens_used, "tokens_used")
        _validate_optional_budget(self.model_calls_used, "model_calls_used")
        if self.forced_stop_reason is not None and not isinstance(
            self.forced_stop_reason, GuardrailReasonCode
        ):
            raise ValueError("forced_stop_reason is invalid")
        if not isinstance(self.trace, GuardrailTrace):
            raise ValueError("execution trace is invalid")
        self.trace.validate()
        if self.final_observable_trace_sha256 is not None:
            _validate_sha(
                self.final_observable_trace_sha256,
                "final_observable_trace_sha256",
            )


def _state_vector(
    contract: ContractIR,
    results: Tuple[CriterionResult, ...],
    protected: Iterable[str],
) -> Tuple[CriterionStateRecord, ...]:
    definitions = {item.criterion_id: item for item in contract.criteria}
    protected_set = set(protected)
    records = []
    for result in results:
        definition = definitions[result.criterion_id]
        records.append(
            CriterionStateRecord(
                criterion_id=result.criterion_id,
                temporal_semantics=result.temporal_semantics,
                status=result.status,
                required=definition.required,
                protected=result.criterion_id in protected_set,
                evidence=tuple(
                    GuardrailEvidencePointer(
                        item.frame_index, item.source, item.timestamp
                    )
                    for item in result.evidence
                ),
            )
        )
    return tuple(sorted(records, key=lambda item: item.criterion_id))


def _latest_observable_statuses(
    trace: DurableEventTrace,
    *,
    candidate_frame_index: int,
    fallback: Mapping[str, CriterionStatus],
) -> Mapping[str, CriterionStatus]:
    """Project the latest black-box observation at a done interception.

    Replay aggregation may intentionally retain an earlier SATISFIED result when
    a later observation is UNKNOWN_EVIDENCE.  That is correct for Audit outcome
    semantics, but it would hide the S1 evidence-loss transition requested by
    the live black-box experiment.  This projection affects only the observable
    corruption metric; Guardrail decisions and strict safety stops continue to
    use replay-derived criterion results.
    """

    selected: dict[str, tuple[int, int, CriterionStatus]] = {}
    for event in trace.events:
        if not isinstance(event, CriterionObservationEvent):
            continue
        observation = event.observation
        if observation.frame_index > candidate_frame_index:
            continue
        current = selected.get(observation.criterion_id)
        position = (observation.frame_index, event.sequence_index)
        if current is None or position > current[:2]:
            selected[observation.criterion_id] = (*position, observation.status)
    statuses = dict(fallback)
    statuses.update(
        {criterion_id: value[2] for criterion_id, value in selected.items()}
    )
    return statuses


class OnlineDoneInterceptor:
    """Per-run state machine.  The object never calls an Agent or a provider."""

    def __init__(
        self,
        *,
        policy: GuardrailPolicy,
        contract: ContractIR,
        run_id: str,
        session_id: str,
    ) -> None:
        policy.validate()
        contract.validate()
        _validate_id(run_id, "run_id")
        _validate_id(session_id, "session_id")
        self._policy = policy
        self._contract = contract
        self._contract_sha = contract_sha256(contract)
        if self._contract_sha not in policy.allowed_contract_sha256s:
            raise ValueError("Contract is absent from the frozen low-risk allowlist")
        unknown_protected = sorted(
            set(policy.protected_criteria)
            - {item.criterion_id for item in contract.criteria}
        )
        if unknown_protected:
            raise ValueError(
                f"protected criteria are absent from Contract: {unknown_protected}"
            )
        self._policy_sha = guardrail_policy_sha256(policy)
        self._run_id = run_id
        self._session_id = session_id
        self._events: list[GuardrailTraceEvent] = []
        self._history: list[CriterionStateSnapshot] = []
        self._observable_history: list[CriterionStateSnapshot] = []
        self._interventions = 0
        self._first_step: Optional[int] = None
        self._first_timestamp: Optional[float] = None
        self._last_candidate = 0
        self._last_step = 0
        self._terminal = False
        self._operational_status = GuardrailOperationalStatus.RUNNING
        self._forced_reason: Optional[GuardrailReasonCode] = None
        self._final_prefix_sha: Optional[str] = None
        self._tokens_used: Optional[int] = None
        self._model_calls_used: Optional[int] = None

    @property
    def terminal(self) -> bool:
        return self._terminal

    def _append(
        self,
        *,
        kind: GuardrailTraceEventKind,
        candidate: DoneCandidate,
        snapshot_sha: str,
        decision: Optional[GuardrailDecision] = None,
        reason: Optional[GuardrailReasonCode] = None,
        criterion_ids: Tuple[str, ...] = (),
        protected_ids: Tuple[str, ...] = (),
        feedback: Optional[StructuredGuardrailFeedback] = None,
        status: Optional[GuardrailOperationalStatus] = None,
    ) -> None:
        self._events.append(
            GuardrailTraceEvent(
                event_ordinal=len(self._events),
                event_kind=kind,
                candidate_ordinal=candidate.candidate_ordinal,
                candidate_step_index=candidate.step_index,
                candidate_frame_index=candidate.frame_index,
                candidate_timestamp=candidate.timestamp,
                intervention_index=self._interventions,
                decision=decision,
                reason_code=reason,
                operational_status=status or self._operational_status,
                contract_sha256=self._contract_sha,
                observable_prefix_sha256=candidate.observable_prefix_sha256,
                policy_sha256=self._policy_sha,
                evidence_snapshot_sha256=snapshot_sha,
                criterion_ids=tuple(sorted(set(criterion_ids))),
                protected_criterion_ids=tuple(sorted(set(protected_ids))),
                feedback=feedback,
            )
        )

    def _finish(
        self,
        *,
        status: GuardrailOperationalStatus,
        reason: Optional[GuardrailReasonCode],
        prefix_sha: str,
    ) -> None:
        self._operational_status = status
        self._forced_reason = reason
        self._final_prefix_sha = prefix_sha
        self._terminal = True

    def _decision(
        self,
        decision: GuardrailDecision,
        reason: GuardrailReasonCode,
        states: Tuple[CriterionStateRecord, ...],
        *,
        host_action: Optional[GuardrailAbstainAction] = None,
        feedback: Optional[StructuredGuardrailFeedback] = None,
    ) -> GuardrailDecisionRecord:
        record = GuardrailDecisionRecord(
            decision,
            reason,
            self._operational_status,
            host_action,
            feedback,
            states,
        )
        record.validate()
        return record

    def _force_stop(
        self,
        *,
        candidate: DoneCandidate,
        states: Tuple[CriterionStateRecord, ...],
        snapshot_sha: str,
        reason: GuardrailReasonCode,
        status: GuardrailOperationalStatus,
        criterion_ids: Tuple[str, ...] = (),
    ) -> GuardrailDecisionRecord:
        self._finish(
            status=status,
            reason=reason,
            prefix_sha=candidate.observable_prefix_sha256,
        )
        self._append(
            kind=GuardrailTraceEventKind.FORCED_STOP,
            candidate=candidate,
            snapshot_sha=snapshot_sha,
            decision=GuardrailDecision.FORCE_STOP,
            reason=reason,
            criterion_ids=criterion_ids,
            protected_ids=criterion_ids,
            status=status,
        )
        return self._decision(GuardrailDecision.FORCE_STOP, reason, states)

    def handle_done(
        self,
        candidate: DoneCandidate,
        observable_prefix: DurableEventTrace,
    ) -> GuardrailDecisionRecord:
        if self._terminal:
            raise RuntimeError("Guardrail run is terminal")
        candidate.validate()
        observable_prefix.validate()
        if self._policy.max_tokens is not None and candidate.tokens_used is None:
            raise ValueError("candidate tokens_used is required by the frozen policy")
        if (
            self._policy.max_model_calls is not None
            and candidate.model_calls_used is None
        ):
            raise ValueError(
                "candidate model_calls_used is required by the frozen policy"
            )
        if candidate.run_id != self._run_id or candidate.session_id != self._session_id:
            raise ValueError("candidate run/session identity drift")
        if candidate.candidate_ordinal != self._last_candidate + 1:
            raise ValueError("candidate ordinals must be contiguous")
        if not hmac.compare_digest(candidate.contract_sha256, self._contract_sha):
            raise ValueError("candidate Contract hash drift")
        if observable_prefix.mode is not RunMode.ONLINE_GUARDRAIL:
            raise ValueError("online interceptor requires ONLINE_GUARDRAIL prefix")
        actual_prefix_sha = event_trace_sha256(observable_prefix)
        if not hmac.compare_digest(
            candidate.observable_prefix_sha256, actual_prefix_sha
        ):
            raise ValueError("candidate observable prefix hash drift")
        if not hmac.compare_digest(
            observable_prefix.contract_sha256, self._contract_sha
        ):
            raise ValueError("observable prefix Contract hash drift")
        report = replay_event_trace(self._contract, observable_prefix)
        states = _state_vector(
            self._contract,
            report.criterion_results,
            self._policy.protected_criteria,
        )
        latest_statuses = _latest_observable_statuses(
            observable_prefix,
            candidate_frame_index=candidate.frame_index,
            fallback={item.criterion_id: item.status for item in states},
        )
        if self._policy.enforce_process_obligations:
            # PROCESS_OBLIGATION aggregation intentionally preserves any historic
            # violation for the final Audit.  Online enforcement instead needs the
            # current candidate state so a later corrective action can be allowed,
            # while the immutable earlier violation remains auditable.
            states = tuple(
                (
                    replace(item, status=latest_statuses[item.criterion_id])
                    if item.temporal_semantics is TemporalSemantics.PROCESS_OBLIGATION
                    else item
                )
                for item in states
            )
        snapshot_sha = criterion_state_vector_sha256(states)
        snapshot = CriterionStateSnapshot(
            intervention_index=self._interventions,
            statuses={item.criterion_id: item.status for item in states},
        )
        observable_snapshot = CriterionStateSnapshot(
            intervention_index=self._interventions,
            statuses=latest_statuses,
        )
        if self._first_step is None:
            self._first_step = candidate.step_index
            self._first_timestamp = candidate.timestamp
        elif candidate.step_index < self._first_step or candidate.timestamp < (
            self._first_timestamp or 0.0
        ):
            raise ValueError("candidate step/time cannot precede the first candidate")
        self._last_candidate = candidate.candidate_ordinal
        self._last_step = candidate.step_index
        self._tokens_used = candidate.tokens_used
        self._model_calls_used = candidate.model_calls_used
        self._append(
            kind=GuardrailTraceEventKind.CANDIDATE_OBSERVED,
            candidate=candidate,
            snapshot_sha=snapshot_sha,
        )

        all_regressions: Tuple[str, ...] = ()
        protected_regressions: Tuple[str, ...] = ()
        observable_corruptions: Tuple[str, ...] = ()
        if self._history:
            before = self._history[-1]
            self._append(
                kind=GuardrailTraceEventKind.POST_INTERVENTION_SNAPSHOT,
                candidate=candidate,
                snapshot_sha=snapshot_sha,
            )
            all_regressions = state_regressions(before, snapshot)
            protected_regressions = state_regressions(
                before,
                snapshot,
                protected_criteria=self._policy.protected_criteria,
            )
            observable_before = self._observable_history[-1]
            observable_corruptions = observable_state_corruptions(
                observable_before, observable_snapshot
            )
            protected_observable_corruptions = observable_state_corruptions(
                observable_before,
                observable_snapshot,
                criteria=self._policy.protected_criteria,
            )
            # SATISFIED -> VIOLATED already has a frozen v1 trace primitive below.
            # Emit only the UNKNOWN_EVIDENCE supplement here, so the S1 counter can
            # combine both event kinds without duplicating strict regressions or
            # rewriting previously frozen Guardrail traces.
            unknown_evidence_losses = tuple(
                criterion_id
                for criterion_id in observable_corruptions
                if criterion_id not in all_regressions
            )
            protected_unknown_evidence_losses = tuple(
                criterion_id
                for criterion_id in protected_observable_corruptions
                if criterion_id not in protected_regressions
            )
            if (
                self._policy.track_observable_state_corruption
                and unknown_evidence_losses
            ):
                self._append(
                    kind=GuardrailTraceEventKind.OBSERVABLE_STATE_CORRUPTION_DETECTED,
                    candidate=candidate,
                    snapshot_sha=snapshot_sha,
                    reason=GuardrailReasonCode.OBSERVABLE_STATE_CORRUPTION_DETECTED,
                    criterion_ids=unknown_evidence_losses,
                    protected_ids=protected_unknown_evidence_losses,
                )
            if all_regressions:
                self._append(
                    kind=GuardrailTraceEventKind.STATE_REGRESSION_DETECTED,
                    candidate=candidate,
                    snapshot_sha=snapshot_sha,
                    reason=GuardrailReasonCode.STATE_REGRESSION_DETECTED,
                    criterion_ids=all_regressions,
                    protected_ids=protected_regressions,
                )
        self._history.append(snapshot)
        self._observable_history.append(observable_snapshot)

        enforcement_scope = tuple(
            item
            for item in states
            if item.required
            and (
                self._policy.enforce_process_obligations
                or item.temporal_semantics is not TemporalSemantics.PROCESS_OBLIGATION
            )
        )
        failed = tuple(
            sorted(
                item.criterion_id
                for item in enforcement_scope
                if item.status is CriterionStatus.VIOLATED
            )
        )

        if (
            protected_regressions
            and failed
            and self._policy.regression_action is GuardrailSafetyAction.FORCE_STOP
        ):
            return self._force_stop(
                candidate=candidate,
                states=states,
                snapshot_sha=snapshot_sha,
                reason=GuardrailReasonCode.STATE_REGRESSION_DETECTED,
                status=GuardrailOperationalStatus.FAIL_STATE_REGRESSION,
                criterion_ids=protected_regressions,
            )

        oscillations = criterion_oscillation_counts(self._history)
        threshold = self._policy.max_oscillations_per_criterion
        reached = tuple(
            sorted(
                criterion_id
                for criterion_id, count in oscillations.items()
                if threshold is not None and count >= threshold
            )
        )
        if reached:
            self._append(
                kind=GuardrailTraceEventKind.CRITERION_OSCILLATION_DETECTED,
                candidate=candidate,
                snapshot_sha=snapshot_sha,
                reason=GuardrailReasonCode.CRITERION_OSCILLATION_DETECTED,
                criterion_ids=reached,
            )
            if (
                failed
                and self._policy.oscillation_action is GuardrailSafetyAction.FORCE_STOP
            ):
                return self._force_stop(
                    candidate=candidate,
                    states=states,
                    snapshot_sha=snapshot_sha,
                    reason=GuardrailReasonCode.CRITERION_OSCILLATION_DETECTED,
                    status=GuardrailOperationalStatus.FAIL_CRITERION_OSCILLATION,
                    criterion_ids=reached,
                )

        process_allows_done = not self._policy.enforce_process_obligations or all(
            item.status is CriterionStatus.SATISFIED
            for item in enforcement_scope
            if item.temporal_semantics is TemporalSemantics.PROCESS_OBLIGATION
        )
        if report.outcome_verdict is RunVerdict.PASS and process_allows_done:
            self._finish(
                status=GuardrailOperationalStatus.DONE_ALLOWED,
                reason=None,
                prefix_sha=candidate.observable_prefix_sha256,
            )
            self._append(
                kind=GuardrailTraceEventKind.DONE_ALLOWED,
                candidate=candidate,
                snapshot_sha=snapshot_sha,
                decision=GuardrailDecision.ALLOW_DONE,
                reason=GuardrailReasonCode.AUDIT_PREFIX_PASS,
                status=self._operational_status,
            )
            return self._decision(
                GuardrailDecision.ALLOW_DONE,
                GuardrailReasonCode.AUDIT_PREFIX_PASS,
                states,
            )

        abstain_reason = None
        if not failed:
            verdicts = {report.outcome_verdict}
            if RunVerdict.INVALID_TRACE in verdicts:
                abstain_reason = GuardrailReasonCode.TRACE_INVALID
            elif RunVerdict.UNSUPPORTED in verdicts:
                abstain_reason = GuardrailReasonCode.CAPABILITY_UNSUPPORTED
            else:
                required = enforcement_scope
                if any(
                    item.status is CriterionStatus.SOURCE_EVIDENCE_MISSING
                    for item in required
                ):
                    abstain_reason = GuardrailReasonCode.SOURCE_EVIDENCE_MISSING
                elif any(
                    item.status is CriterionStatus.UNSUPPORTED_CAPABILITY
                    for item in required
                ):
                    abstain_reason = GuardrailReasonCode.CAPABILITY_UNSUPPORTED
                else:
                    abstain_reason = GuardrailReasonCode.EVIDENCE_UNKNOWN
        if abstain_reason is not None:
            action = self._policy.on_abstain
            status = {
                GuardrailAbstainAction.ALLOW_DONE_UNJUDGED: GuardrailOperationalStatus.DONE_ALLOWED_UNJUDGED,
                GuardrailAbstainAction.FORCE_STOP_UNJUDGED: GuardrailOperationalStatus.FORCE_STOP_UNJUDGED,
                GuardrailAbstainAction.ESCALATE_HUMAN: GuardrailOperationalStatus.ESCALATED_HUMAN,
            }[action]
            self._finish(
                status=status,
                reason=None,
                prefix_sha=candidate.observable_prefix_sha256,
            )
            self._append(
                kind=GuardrailTraceEventKind.ABSTAIN_RECORDED,
                candidate=candidate,
                snapshot_sha=snapshot_sha,
                decision=GuardrailDecision.ABSTAIN,
                reason=abstain_reason,
                status=status,
            )
            terminal_kind = {
                GuardrailAbstainAction.ALLOW_DONE_UNJUDGED: GuardrailTraceEventKind.DONE_ALLOWED,
                GuardrailAbstainAction.FORCE_STOP_UNJUDGED: GuardrailTraceEventKind.FORCED_STOP,
                GuardrailAbstainAction.ESCALATE_HUMAN: GuardrailTraceEventKind.HUMAN_ESCALATION,
            }[action]
            self._append(
                kind=terminal_kind,
                candidate=candidate,
                snapshot_sha=snapshot_sha,
                decision=GuardrailDecision.ABSTAIN,
                reason=abstain_reason,
                status=status,
            )
            return self._decision(
                GuardrailDecision.ABSTAIN,
                abstain_reason,
                states,
                host_action=action,
            )

        if not failed:
            raise ValueError("shared replay produced an unhandled Guardrail outcome")

        extra_steps = candidate.step_index - (self._first_step or 0)
        elapsed = candidate.timestamp - (self._first_timestamp or 0.0)
        budget_failure: Optional[
            tuple[GuardrailReasonCode, GuardrailOperationalStatus]
        ] = None
        if extra_steps > self._policy.max_extra_steps:
            budget_failure = (
                GuardrailReasonCode.STEP_BUDGET_EXHAUSTED,
                GuardrailOperationalStatus.FAIL_STEP_BUDGET,
            )
        elif (
            self._policy.logical_deadline_seconds is not None
            and elapsed > self._policy.logical_deadline_seconds
        ):
            budget_failure = (
                GuardrailReasonCode.TIME_BUDGET_EXHAUSTED,
                GuardrailOperationalStatus.FAIL_TIME_BUDGET,
            )
        elif (
            self._policy.max_tokens is not None
            and candidate.tokens_used is not None
            and candidate.tokens_used > self._policy.max_tokens
        ):
            budget_failure = (
                GuardrailReasonCode.TOKEN_BUDGET_EXHAUSTED,
                GuardrailOperationalStatus.FAIL_TOKEN_BUDGET,
            )
        elif (
            self._policy.max_model_calls is not None
            and candidate.model_calls_used is not None
            and candidate.model_calls_used > self._policy.max_model_calls
        ):
            budget_failure = (
                GuardrailReasonCode.MODEL_CALL_BUDGET_EXHAUSTED,
                GuardrailOperationalStatus.FAIL_MODEL_CALL_BUDGET,
            )
        if budget_failure is not None:
            reason, status = budget_failure
            return self._force_stop(
                candidate=candidate,
                states=states,
                snapshot_sha=snapshot_sha,
                reason=reason,
                status=status,
            )
        if self._interventions >= self._policy.max_interventions:
            return self._force_stop(
                candidate=candidate,
                states=states,
                snapshot_sha=snapshot_sha,
                reason=GuardrailReasonCode.MAX_INTERVENTIONS_REACHED,
                status=GuardrailOperationalStatus.FAIL_MAX_INTERVENTIONS_REACHED,
            )

        self._interventions += 1
        feedback = StructuredGuardrailFeedback(
            status=CriterionStatus.VIOLATED,
            decision=GuardrailDecision.INTERVENE_CONTINUE,
            reason_code=GuardrailReasonCode.HIGH_CONFIDENCE_CRITERION_VIOLATION,
            intervention_index=self._interventions,
            failed_criteria=failed,
            criterion_state_vector=states,
            evidence_snapshot_sha256=snapshot_sha,
            action_required=GuardrailActionRequired.CONTINUE,
            contract_sha256=self._contract_sha,
            observable_prefix_sha256=candidate.observable_prefix_sha256,
            policy_sha256=self._policy_sha,
        )
        feedback.validate()
        self._append(
            kind=GuardrailTraceEventKind.INTERVENTION_ISSUED,
            candidate=candidate,
            snapshot_sha=snapshot_sha,
            decision=GuardrailDecision.INTERVENE_CONTINUE,
            reason=GuardrailReasonCode.HIGH_CONFIDENCE_CRITERION_VIOLATION,
            criterion_ids=failed,
            feedback=feedback,
        )
        self._operational_status = GuardrailOperationalStatus.RUNNING
        return self._decision(
            GuardrailDecision.INTERVENE_CONTINUE,
            GuardrailReasonCode.HIGH_CONFIDENCE_CRITERION_VIOLATION,
            states,
            feedback=feedback,
        )

    def result(self) -> GuardrailExecutionResult:
        trace = GuardrailTrace(
            trace_id=f"{self._run_id}.guardrail",
            run_id=self._run_id,
            session_id=self._session_id,
            contract_sha256=self._contract_sha,
            policy_sha256=self._policy_sha,
            events=tuple(self._events),
        )
        extra_steps = 0
        if self._events and self._first_step is not None:
            extra_steps = max(0, self._last_step - self._first_step)
        result = GuardrailExecutionResult(
            operational_status=self._operational_status,
            intervention_count=self._interventions,
            extra_steps=extra_steps,
            tokens_used=self._tokens_used,
            model_calls_used=self._model_calls_used,
            forced_stop_reason=self._forced_reason,
            trace=trace,
            final_observable_trace_sha256=self._final_prefix_sha,
        )
        result.validate()
        return result


def project_observable_trace_for_audit(
    trace: DurableEventTrace,
) -> DurableEventTrace:
    """Project only observable events into the independent Audit mode.

    Guardrail feedback never entered ``DurableEventTrace`` and therefore cannot
    be projected as outcome or process evidence.
    """

    trace.validate()
    if trace.mode is not RunMode.ONLINE_GUARDRAIL:
        raise ValueError("only ONLINE_GUARDRAIL traces need Audit projection")
    projected = replace(trace, mode=RunMode.AUDIT_BENCHMARK)
    projected.validate()
    return projected


# Strict JSON decoding -----------------------------------------------------


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _strict_json(data: bytes) -> Mapping[str, Any]:
    try:
        value = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=lambda token: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON number: {token}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid Guardrail JSON: {exc}") from exc
    if not isinstance(value, Mapping):
        raise ValueError("Guardrail JSON root must be an object")
    return value


def _keys(value: Any, expected: set[str], context: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{context} must be an object")
    actual = set(value)
    if actual != expected:
        raise ValueError(
            f"{context} keys mismatch; missing={sorted(expected-actual)}, "
            f"unexpected={sorted(actual-expected)}"
        )
    return value


def _state_from_payload(value: Any) -> CriterionStateRecord:
    item = _keys(
        value,
        {
            "criterion_id",
            "temporal_semantics",
            "status",
            "required",
            "protected",
            "evidence",
        },
        "criterion state",
    )
    evidence = item["evidence"]
    if not isinstance(evidence, list):
        raise ValueError("criterion evidence must be an array")
    pointers = []
    for raw in evidence:
        pointer = _keys(
            raw,
            {"frame_index", "source", "timestamp"},
            "criterion evidence pointer",
        )
        pointers.append(
            GuardrailEvidencePointer(
                pointer["frame_index"], pointer["source"], pointer["timestamp"]
            )
        )
    record = CriterionStateRecord(
        item["criterion_id"],
        TemporalSemantics(item["temporal_semantics"]),
        CriterionStatus(item["status"]),
        item["required"],
        item["protected"],
        tuple(pointers),
    )
    record.validate()
    return record


def _feedback_from_payload(value: Any) -> StructuredGuardrailFeedback:
    item = _keys(
        value,
        {
            "schema_version",
            "status",
            "decision",
            "reason_code",
            "intervention_index",
            "failed_criteria",
            "criterion_state_vector",
            "evidence_snapshot_sha256",
            "action_required",
            "contract_sha256",
            "observable_prefix_sha256",
            "policy_sha256",
        },
        "Guardrail feedback",
    )
    if not isinstance(item["failed_criteria"], list) or not isinstance(
        item["criterion_state_vector"], list
    ):
        raise ValueError("feedback criteria fields must be arrays")
    feedback = StructuredGuardrailFeedback(
        status=CriterionStatus(item["status"]),
        decision=GuardrailDecision(item["decision"]),
        reason_code=GuardrailReasonCode(item["reason_code"]),
        intervention_index=item["intervention_index"],
        failed_criteria=tuple(item["failed_criteria"]),
        criterion_state_vector=tuple(
            _state_from_payload(raw) for raw in item["criterion_state_vector"]
        ),
        evidence_snapshot_sha256=item["evidence_snapshot_sha256"],
        action_required=GuardrailActionRequired(item["action_required"]),
        contract_sha256=item["contract_sha256"],
        observable_prefix_sha256=item["observable_prefix_sha256"],
        policy_sha256=item["policy_sha256"],
        schema_version=item["schema_version"],
    )
    feedback.validate()
    return feedback


def guardrail_feedback_from_json_bytes(data: bytes) -> StructuredGuardrailFeedback:
    return _feedback_from_payload(_strict_json(data))


def _trace_event_from_payload(value: Any) -> GuardrailTraceEvent:
    item = _keys(
        value,
        {
            "event_ordinal",
            "event_kind",
            "candidate_ordinal",
            "candidate_step_index",
            "candidate_frame_index",
            "candidate_timestamp",
            "intervention_index",
            "decision",
            "reason_code",
            "operational_status",
            "contract_sha256",
            "observable_prefix_sha256",
            "policy_sha256",
            "evidence_snapshot_sha256",
            "criterion_ids",
            "protected_criterion_ids",
            "feedback",
        },
        "Guardrail trace event",
    )
    if not isinstance(item["criterion_ids"], list) or not isinstance(
        item["protected_criterion_ids"], list
    ):
        raise ValueError("trace criterion ids must be arrays")
    event = GuardrailTraceEvent(
        event_ordinal=item["event_ordinal"],
        event_kind=GuardrailTraceEventKind(item["event_kind"]),
        candidate_ordinal=item["candidate_ordinal"],
        candidate_step_index=item["candidate_step_index"],
        candidate_frame_index=item["candidate_frame_index"],
        candidate_timestamp=item["candidate_timestamp"],
        intervention_index=item["intervention_index"],
        decision=(
            GuardrailDecision(item["decision"])
            if item["decision"] is not None
            else None
        ),
        reason_code=(
            GuardrailReasonCode(item["reason_code"])
            if item["reason_code"] is not None
            else None
        ),
        operational_status=GuardrailOperationalStatus(item["operational_status"]),
        contract_sha256=item["contract_sha256"],
        observable_prefix_sha256=item["observable_prefix_sha256"],
        policy_sha256=item["policy_sha256"],
        evidence_snapshot_sha256=item["evidence_snapshot_sha256"],
        criterion_ids=tuple(item["criterion_ids"]),
        protected_criterion_ids=tuple(item["protected_criterion_ids"]),
        feedback=(
            _feedback_from_payload(item["feedback"])
            if item["feedback"] is not None
            else None
        ),
    )
    event.validate()
    return event


def guardrail_trace_from_json_bytes(data: bytes) -> GuardrailTrace:
    item = _keys(
        _strict_json(data),
        {
            "schema_version",
            "trace_id",
            "run_id",
            "session_id",
            "contract_sha256",
            "policy_sha256",
            "events",
        },
        "Guardrail trace",
    )
    if not isinstance(item["events"], list):
        raise ValueError("Guardrail trace events must be an array")
    trace = GuardrailTrace(
        trace_id=item["trace_id"],
        run_id=item["run_id"],
        session_id=item["session_id"],
        contract_sha256=item["contract_sha256"],
        policy_sha256=item["policy_sha256"],
        events=tuple(_trace_event_from_payload(raw) for raw in item["events"]),
        schema_version=item["schema_version"],
    )
    trace.validate()
    return trace


# A/B report ---------------------------------------------------------------


@dataclass(frozen=True)
class GuardrailAbCaseReport:
    case_id: str
    baseline_audit_envelope_sha256: str
    guardrail_audit_envelope_sha256: str
    guardrail_trace_sha256: str
    baseline_final_verdict: RunVerdict
    guardrail_final_verdict: RunVerdict
    guardrail_operational_status: GuardrailOperationalStatus
    correction_success: bool
    false_intervention: bool
    state_corruption_count: int
    criterion_oscillation_count: int
    intervention_count: int
    extra_steps: int
    forced_stop: bool
    latency_ms: Optional[float]
    tokens_used: Optional[int]
    model_calls_used: Optional[int]

    def validate(self) -> None:
        _validate_id(self.case_id, "A/B case_id")
        for name, value in (
            ("baseline_audit_envelope_sha256", self.baseline_audit_envelope_sha256),
            ("guardrail_audit_envelope_sha256", self.guardrail_audit_envelope_sha256),
            ("guardrail_trace_sha256", self.guardrail_trace_sha256),
        ):
            _validate_sha(value, f"A/B {name}")
        if not isinstance(self.baseline_final_verdict, RunVerdict) or not isinstance(
            self.guardrail_final_verdict, RunVerdict
        ):
            raise ValueError("A/B final verdict is invalid")
        if not isinstance(
            self.guardrail_operational_status, GuardrailOperationalStatus
        ):
            raise ValueError("A/B operational status is invalid")
        for name, value in (
            ("correction_success", self.correction_success),
            ("false_intervention", self.false_intervention),
            ("forced_stop", self.forced_stop),
        ):
            if not isinstance(value, bool):
                raise ValueError(f"A/B {name} must be boolean")
        for name, value in (
            ("state_corruption_count", self.state_corruption_count),
            ("criterion_oscillation_count", self.criterion_oscillation_count),
            ("intervention_count", self.intervention_count),
            ("extra_steps", self.extra_steps),
        ):
            _validate_non_negative_int(value, f"A/B {name}")
        if self.intervention_count > HARD_MAX_INTERVENTIONS:
            raise ValueError("A/B intervention_count exceeds hard ceiling")
        expected_correction = (
            self.baseline_final_verdict is not RunVerdict.PASS
            and self.guardrail_final_verdict is RunVerdict.PASS
            and self.intervention_count > 0
        )
        if self.correction_success is not expected_correction:
            raise ValueError("A/B correction_success is not recomputable")
        expected_false_intervention = (
            self.baseline_final_verdict is RunVerdict.PASS
            and self.intervention_count > 0
        )
        if self.false_intervention is not expected_false_intervention:
            raise ValueError("A/B false_intervention is not recomputable")
        _validate_optional_number(self.latency_ms, "A/B latency_ms")
        _validate_optional_budget(self.tokens_used, "A/B tokens_used")
        _validate_optional_budget(self.model_calls_used, "A/B model_calls_used")


def guardrail_ab_case_payload(value: GuardrailAbCaseReport) -> dict[str, Any]:
    value.validate()
    return {
        "case_id": value.case_id,
        "baseline_audit_envelope_sha256": value.baseline_audit_envelope_sha256,
        "guardrail_audit_envelope_sha256": value.guardrail_audit_envelope_sha256,
        "guardrail_trace_sha256": value.guardrail_trace_sha256,
        "baseline_final_verdict": value.baseline_final_verdict.value,
        "guardrail_final_verdict": value.guardrail_final_verdict.value,
        "guardrail_operational_status": value.guardrail_operational_status.value,
        "correction_success": value.correction_success,
        "false_intervention": value.false_intervention,
        "state_corruption_count": value.state_corruption_count,
        "criterion_oscillation_count": value.criterion_oscillation_count,
        "intervention_count": value.intervention_count,
        "extra_steps": value.extra_steps,
        "forced_stop": value.forced_stop,
        "latency_ms": value.latency_ms,
        "tokens_used": value.tokens_used,
        "model_calls_used": value.model_calls_used,
    }


def build_guardrail_ab_case_report(
    *,
    case_id: str,
    baseline_audit_envelope: AuditReportEnvelope,
    guardrail_audit_envelope: AuditReportEnvelope,
    execution: GuardrailExecutionResult,
    claimed_baseline_verdict: Optional[RunVerdict] = None,
    claimed_guardrail_verdict: Optional[RunVerdict] = None,
) -> GuardrailAbCaseReport:
    """Bind an A/B row to independent Audit envelopes and Guardrail facts.

    Optional claimed verdicts exist only to make caller self-certification drift
    fail closed; authoritative values are always read from the envelopes.
    """

    baseline_audit_envelope.validate()
    guardrail_audit_envelope.validate()
    execution.validate()
    if (
        claimed_baseline_verdict is not None
        and claimed_baseline_verdict is not baseline_audit_envelope.verdict
    ):
        raise ValueError("claimed baseline verdict disagrees with Audit envelope")
    if (
        claimed_guardrail_verdict is not None
        and claimed_guardrail_verdict is not guardrail_audit_envelope.verdict
    ):
        raise ValueError("claimed Guardrail verdict disagrees with Audit envelope")
    regression_ids = {
        criterion_id
        for event in execution.trace.events
        if event.event_kind is GuardrailTraceEventKind.STATE_REGRESSION_DETECTED
        for criterion_id in event.protected_criterion_ids
    }
    oscillation_ids = {
        criterion_id
        for event in execution.trace.events
        if event.event_kind is GuardrailTraceEventKind.CRITERION_OSCILLATION_DETECTED
        for criterion_id in event.criterion_ids
    }
    forced_stop = any(
        event.event_kind is GuardrailTraceEventKind.FORCED_STOP
        for event in execution.trace.events
    )
    row = GuardrailAbCaseReport(
        case_id=case_id,
        baseline_audit_envelope_sha256=audit_report_envelope_sha256(
            baseline_audit_envelope
        ),
        guardrail_audit_envelope_sha256=audit_report_envelope_sha256(
            guardrail_audit_envelope
        ),
        guardrail_trace_sha256=guardrail_trace_sha256(execution.trace),
        baseline_final_verdict=baseline_audit_envelope.verdict,
        guardrail_final_verdict=guardrail_audit_envelope.verdict,
        guardrail_operational_status=execution.operational_status,
        correction_success=(
            baseline_audit_envelope.verdict is not RunVerdict.PASS
            and guardrail_audit_envelope.verdict is RunVerdict.PASS
            and execution.intervention_count > 0
        ),
        false_intervention=(
            baseline_audit_envelope.verdict is RunVerdict.PASS
            and execution.intervention_count > 0
        ),
        state_corruption_count=len(regression_ids),
        criterion_oscillation_count=len(oscillation_ids),
        intervention_count=execution.intervention_count,
        extra_steps=execution.extra_steps,
        forced_stop=forced_stop,
        latency_ms=None,
        tokens_used=execution.tokens_used,
        model_calls_used=execution.model_calls_used,
    )
    row.validate()
    return row


@dataclass(frozen=True)
class GuardrailAbMetrics:
    case_count: int
    baseline_audit_pass_count: int
    guardrail_audit_pass_count: int
    audit_success_delta: float
    correction_success_count: int
    correction_success_rate: float
    false_intervention_count: int
    false_intervention_rate: float
    state_corruption_count: int
    state_corruption_rate: float
    criterion_oscillation_count: int
    intervention_count_distribution: Tuple[int, ...]
    extra_steps_total: int
    forced_stop_count: int
    forced_stop_rate: float
    latency_ms: Optional[float]
    tokens_used: Optional[int]
    model_calls_used: Optional[int]

    def validate(self) -> None:
        for name, value in (
            ("case_count", self.case_count),
            ("baseline_audit_pass_count", self.baseline_audit_pass_count),
            ("guardrail_audit_pass_count", self.guardrail_audit_pass_count),
            ("correction_success_count", self.correction_success_count),
            ("false_intervention_count", self.false_intervention_count),
            ("state_corruption_count", self.state_corruption_count),
            ("criterion_oscillation_count", self.criterion_oscillation_count),
            ("extra_steps_total", self.extra_steps_total),
            ("forced_stop_count", self.forced_stop_count),
        ):
            _validate_non_negative_int(value, f"metrics {name}")
        if self.case_count < 1:
            raise ValueError("metrics case_count must be positive")
        for name, value in (
            ("audit_success_delta", self.audit_success_delta),
            ("correction_success_rate", self.correction_success_rate),
            ("false_intervention_rate", self.false_intervention_rate),
            ("state_corruption_rate", self.state_corruption_rate),
            ("forced_stop_rate", self.forced_stop_rate),
        ):
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(value)
                or value < -1
                or value > 1
            ):
                raise ValueError(f"metrics {name} is invalid")
        if not isinstance(self.intervention_count_distribution, tuple):
            raise ValueError("intervention distribution must be immutable")
        if any(
            not isinstance(value, int)
            or isinstance(value, bool)
            or value < 0
            or value > HARD_MAX_INTERVENTIONS
            for value in self.intervention_count_distribution
        ):
            raise ValueError("intervention distribution is invalid")
        _validate_optional_number(self.latency_ms, "metrics latency_ms")
        _validate_optional_budget(self.tokens_used, "metrics tokens_used")
        _validate_optional_budget(self.model_calls_used, "metrics model_calls_used")


def _metrics_payload(value: GuardrailAbMetrics) -> dict[str, Any]:
    value.validate()
    return {
        "case_count": value.case_count,
        "baseline_audit_pass_count": value.baseline_audit_pass_count,
        "guardrail_audit_pass_count": value.guardrail_audit_pass_count,
        "audit_success_delta": value.audit_success_delta,
        "correction_success_count": value.correction_success_count,
        "correction_success_rate": value.correction_success_rate,
        "false_intervention_count": value.false_intervention_count,
        "false_intervention_rate": value.false_intervention_rate,
        "state_corruption_count": value.state_corruption_count,
        "state_corruption_rate": value.state_corruption_rate,
        "criterion_oscillation_count": value.criterion_oscillation_count,
        "intervention_count_distribution": list(value.intervention_count_distribution),
        "extra_steps_total": value.extra_steps_total,
        "forced_stop_count": value.forced_stop_count,
        "forced_stop_rate": value.forced_stop_rate,
        "latency_ms": value.latency_ms,
        "tokens_used": value.tokens_used,
        "model_calls_used": value.model_calls_used,
    }


@dataclass(frozen=True)
class GuardrailAbComparisonReport:
    experiment_id: str
    cases: Tuple[GuardrailAbCaseReport, ...]
    metrics: GuardrailAbMetrics
    real_agent_calls: int = 0
    network_requests: int = 0
    api_key_reads: int = 0
    device_actions: int = 0
    guardrail_free_text_generations: int = 0
    schema_version: str = GUARDRAIL_AB_REPORT_SCHEMA_VERSION

    def validate(self) -> None:
        if self.schema_version != GUARDRAIL_AB_REPORT_SCHEMA_VERSION:
            raise ValueError("unsupported Guardrail A/B report schema")
        _validate_id(self.experiment_id, "A/B experiment_id")
        if not isinstance(self.cases, tuple) or not self.cases:
            raise ValueError("A/B cases must be a non-empty tuple")
        for case in self.cases:
            if not isinstance(case, GuardrailAbCaseReport):
                raise ValueError("A/B case is invalid")
            case.validate()
        ids = tuple(case.case_id for case in self.cases)
        if ids != tuple(sorted(set(ids))):
            raise ValueError("A/B cases must be sorted with unique ids")
        expected = derive_guardrail_ab_metrics(self.cases)
        if self.metrics != expected:
            raise ValueError("A/B metrics are not recomputable from case facts")
        for name, value in (
            ("real_agent_calls", self.real_agent_calls),
            ("network_requests", self.network_requests),
            ("api_key_reads", self.api_key_reads),
            ("device_actions", self.device_actions),
            ("guardrail_free_text_generations", self.guardrail_free_text_generations),
        ):
            if value != 0:
                raise ValueError(f"local Mock A/B {name} must be zero")


def derive_guardrail_ab_metrics(
    cases: Tuple[GuardrailAbCaseReport, ...],
) -> GuardrailAbMetrics:
    if not cases:
        raise ValueError("cannot derive A/B metrics without cases")
    for case in cases:
        case.validate()
    count = len(cases)
    baseline_pass = sum(
        case.baseline_final_verdict is RunVerdict.PASS for case in cases
    )
    guardrail_pass = sum(
        case.guardrail_final_verdict is RunVerdict.PASS for case in cases
    )
    corrections = sum(case.correction_success for case in cases)
    false_interventions = sum(case.false_intervention for case in cases)
    corruptions = sum(case.state_corruption_count for case in cases)
    oscillations = sum(case.criterion_oscillation_count for case in cases)
    forced = sum(case.forced_stop for case in cases)
    return GuardrailAbMetrics(
        case_count=count,
        baseline_audit_pass_count=baseline_pass,
        guardrail_audit_pass_count=guardrail_pass,
        audit_success_delta=(guardrail_pass - baseline_pass) / count,
        correction_success_count=corrections,
        correction_success_rate=corrections / count,
        false_intervention_count=false_interventions,
        false_intervention_rate=false_interventions / count,
        state_corruption_count=corruptions,
        state_corruption_rate=corruptions / count,
        criterion_oscillation_count=oscillations,
        intervention_count_distribution=tuple(
            sorted(case.intervention_count for case in cases)
        ),
        extra_steps_total=sum(case.extra_steps for case in cases),
        forced_stop_count=forced,
        forced_stop_rate=forced / count,
        latency_ms=None,
        tokens_used=None,
        model_calls_used=None,
    )


def guardrail_ab_report_payload(
    report: GuardrailAbComparisonReport,
) -> dict[str, Any]:
    report.validate()
    return {
        "schema_version": report.schema_version,
        "experiment_id": report.experiment_id,
        "cases": [guardrail_ab_case_payload(case) for case in report.cases],
        "metrics": _metrics_payload(report.metrics),
        "execution_boundary": {
            "real_agent_calls": report.real_agent_calls,
            "network_requests": report.network_requests,
            "api_key_reads": report.api_key_reads,
            "device_actions": report.device_actions,
            "guardrail_free_text_generations": report.guardrail_free_text_generations,
        },
        "claim_boundary": "SYNTHETIC_PROTOCOL_AND_FAILURE_HANDLING_ONLY",
    }


def guardrail_ab_report_sha256(report: GuardrailAbComparisonReport) -> str:
    return _digest(guardrail_ab_report_payload(report))


def _ab_case_from_payload(value: Any) -> GuardrailAbCaseReport:
    expected = {
        "case_id",
        "baseline_audit_envelope_sha256",
        "guardrail_audit_envelope_sha256",
        "guardrail_trace_sha256",
        "baseline_final_verdict",
        "guardrail_final_verdict",
        "guardrail_operational_status",
        "correction_success",
        "false_intervention",
        "state_corruption_count",
        "criterion_oscillation_count",
        "intervention_count",
        "extra_steps",
        "forced_stop",
        "latency_ms",
        "tokens_used",
        "model_calls_used",
    }
    item = _keys(value, expected, "A/B case")
    case = GuardrailAbCaseReport(
        case_id=item["case_id"],
        baseline_audit_envelope_sha256=item["baseline_audit_envelope_sha256"],
        guardrail_audit_envelope_sha256=item["guardrail_audit_envelope_sha256"],
        guardrail_trace_sha256=item["guardrail_trace_sha256"],
        baseline_final_verdict=RunVerdict(item["baseline_final_verdict"]),
        guardrail_final_verdict=RunVerdict(item["guardrail_final_verdict"]),
        guardrail_operational_status=GuardrailOperationalStatus(
            item["guardrail_operational_status"]
        ),
        correction_success=item["correction_success"],
        false_intervention=item["false_intervention"],
        state_corruption_count=item["state_corruption_count"],
        criterion_oscillation_count=item["criterion_oscillation_count"],
        intervention_count=item["intervention_count"],
        extra_steps=item["extra_steps"],
        forced_stop=item["forced_stop"],
        latency_ms=item["latency_ms"],
        tokens_used=item["tokens_used"],
        model_calls_used=item["model_calls_used"],
    )
    case.validate()
    return case


def _ab_metrics_from_payload(value: Any) -> GuardrailAbMetrics:
    expected = {
        "case_count",
        "baseline_audit_pass_count",
        "guardrail_audit_pass_count",
        "audit_success_delta",
        "correction_success_count",
        "correction_success_rate",
        "false_intervention_count",
        "false_intervention_rate",
        "state_corruption_count",
        "state_corruption_rate",
        "criterion_oscillation_count",
        "intervention_count_distribution",
        "extra_steps_total",
        "forced_stop_count",
        "forced_stop_rate",
        "latency_ms",
        "tokens_used",
        "model_calls_used",
    }
    item = _keys(value, expected, "A/B metrics")
    distribution = item["intervention_count_distribution"]
    if not isinstance(distribution, list):
        raise ValueError("A/B intervention distribution must be an array")
    metrics = GuardrailAbMetrics(
        case_count=item["case_count"],
        baseline_audit_pass_count=item["baseline_audit_pass_count"],
        guardrail_audit_pass_count=item["guardrail_audit_pass_count"],
        audit_success_delta=item["audit_success_delta"],
        correction_success_count=item["correction_success_count"],
        correction_success_rate=item["correction_success_rate"],
        false_intervention_count=item["false_intervention_count"],
        false_intervention_rate=item["false_intervention_rate"],
        state_corruption_count=item["state_corruption_count"],
        state_corruption_rate=item["state_corruption_rate"],
        criterion_oscillation_count=item["criterion_oscillation_count"],
        intervention_count_distribution=tuple(distribution),
        extra_steps_total=item["extra_steps_total"],
        forced_stop_count=item["forced_stop_count"],
        forced_stop_rate=item["forced_stop_rate"],
        latency_ms=item["latency_ms"],
        tokens_used=item["tokens_used"],
        model_calls_used=item["model_calls_used"],
    )
    metrics.validate()
    return metrics


def guardrail_ab_report_from_json_bytes(data: bytes) -> GuardrailAbComparisonReport:
    item = _keys(
        _strict_json(data),
        {
            "schema_version",
            "experiment_id",
            "cases",
            "metrics",
            "execution_boundary",
            "claim_boundary",
        },
        "Guardrail A/B report",
    )
    if item["claim_boundary"] != "SYNTHETIC_PROTOCOL_AND_FAILURE_HANDLING_ONLY":
        raise ValueError("A/B claim boundary drift")
    cases_raw = item["cases"]
    if not isinstance(cases_raw, list):
        raise ValueError("A/B cases must be an array")
    boundary = _keys(
        item["execution_boundary"],
        {
            "real_agent_calls",
            "network_requests",
            "api_key_reads",
            "device_actions",
            "guardrail_free_text_generations",
        },
        "A/B execution boundary",
    )
    report = GuardrailAbComparisonReport(
        experiment_id=item["experiment_id"],
        cases=tuple(_ab_case_from_payload(raw) for raw in cases_raw),
        metrics=_ab_metrics_from_payload(item["metrics"]),
        real_agent_calls=boundary["real_agent_calls"],
        network_requests=boundary["network_requests"],
        api_key_reads=boundary["api_key_reads"],
        device_actions=boundary["device_actions"],
        guardrail_free_text_generations=boundary["guardrail_free_text_generations"],
        schema_version=item["schema_version"],
    )
    report.validate()
    return report


def guardrail_json_bytes(value: Mapping[str, Any]) -> bytes:
    return _canonical_bytes(value) + b"\n"


def _enum_schema(enum_type: type[Enum]) -> dict[str, Any]:
    return {"type": "string", "enum": [item.value for item in enum_type]}


def _object(properties: Mapping[str, Any], required: Iterable[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": dict(properties),
        "required": list(required),
    }


def _evidence_pointer_schema() -> dict[str, Any]:
    return _object(
        {
            "frame_index": {"type": "integer", "minimum": 0},
            "source": {"type": "string", "minLength": 1},
            "timestamp": {
                "oneOf": [{"type": "number", "minimum": 0}, {"type": "null"}]
            },
        },
        ("frame_index", "source", "timestamp"),
    )


def _criterion_state_schema() -> dict[str, Any]:
    return _object(
        {
            "criterion_id": {"type": "string", "minLength": 1},
            "temporal_semantics": _enum_schema(TemporalSemantics),
            "status": _enum_schema(CriterionStatus),
            "required": {"type": "boolean"},
            "protected": {"type": "boolean"},
            "evidence": {"type": "array", "items": _evidence_pointer_schema()},
        },
        (
            "criterion_id",
            "temporal_semantics",
            "status",
            "required",
            "protected",
            "evidence",
        ),
    )


def guardrail_feedback_json_schema() -> dict[str, Any]:
    properties = {
        "schema_version": {"const": GUARDRAIL_FEEDBACK_SCHEMA_VERSION},
        "status": {"const": CriterionStatus.VIOLATED.value},
        "decision": {"const": GuardrailDecision.INTERVENE_CONTINUE.value},
        "reason_code": {
            "const": GuardrailReasonCode.HIGH_CONFIDENCE_CRITERION_VIOLATION.value
        },
        "intervention_index": {
            "type": "integer",
            "minimum": 1,
            "maximum": HARD_MAX_INTERVENTIONS,
        },
        "failed_criteria": {
            "type": "array",
            "minItems": 1,
            "uniqueItems": True,
            "items": {"type": "string", "minLength": 1},
        },
        "criterion_state_vector": {
            "type": "array",
            "minItems": 1,
            "items": _criterion_state_schema(),
        },
        "evidence_snapshot_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "action_required": {"const": GuardrailActionRequired.CONTINUE.value},
        "contract_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "observable_prefix_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "policy_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://harmony-eval.local/schemas/guardrail_feedback_v1.schema.json",
        **_object(properties, properties.keys()),
    }


def guardrail_trace_json_schema() -> dict[str, Any]:
    event_properties = {
        "event_ordinal": {"type": "integer", "minimum": 0},
        "event_kind": _enum_schema(GuardrailTraceEventKind),
        "candidate_ordinal": {"type": "integer", "minimum": 1},
        "candidate_step_index": {"type": "integer", "minimum": 0},
        "candidate_frame_index": {"type": "integer", "minimum": 0},
        "candidate_timestamp": {"type": "number", "minimum": 0},
        "intervention_index": {
            "type": "integer",
            "minimum": 0,
            "maximum": HARD_MAX_INTERVENTIONS,
        },
        "decision": {"oneOf": [_enum_schema(GuardrailDecision), {"type": "null"}]},
        "reason_code": {"oneOf": [_enum_schema(GuardrailReasonCode), {"type": "null"}]},
        "operational_status": _enum_schema(GuardrailOperationalStatus),
        "contract_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "observable_prefix_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "policy_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "evidence_snapshot_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "criterion_ids": {
            "type": "array",
            "uniqueItems": True,
            "items": {"type": "string", "minLength": 1},
        },
        "protected_criterion_ids": {
            "type": "array",
            "uniqueItems": True,
            "items": {"type": "string", "minLength": 1},
        },
        "feedback": {
            "oneOf": [
                {
                    key: value
                    for key, value in guardrail_feedback_json_schema().items()
                    if key not in {"$schema", "$id"}
                },
                {"type": "null"},
            ]
        },
    }
    properties = {
        "schema_version": {"const": GUARDRAIL_TRACE_SCHEMA_VERSION},
        "trace_id": {"type": "string", "minLength": 1},
        "run_id": {"type": "string", "minLength": 1},
        "session_id": {"type": "string", "minLength": 1},
        "contract_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "policy_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "events": {
            "type": "array",
            "items": _object(event_properties, event_properties.keys()),
        },
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://harmony-eval.local/schemas/guardrail_trace_v1.schema.json",
        **_object(properties, properties.keys()),
    }


def guardrail_ab_report_json_schema() -> dict[str, Any]:
    nullable_number = {"oneOf": [{"type": "number", "minimum": 0}, {"type": "null"}]}
    nullable_integer = {"oneOf": [{"type": "integer", "minimum": 0}, {"type": "null"}]}
    case_properties = {
        "case_id": {"type": "string", "minLength": 1},
        "baseline_audit_envelope_sha256": {
            "type": "string",
            "pattern": "^[0-9a-f]{64}$",
        },
        "guardrail_audit_envelope_sha256": {
            "type": "string",
            "pattern": "^[0-9a-f]{64}$",
        },
        "guardrail_trace_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "baseline_final_verdict": _enum_schema(RunVerdict),
        "guardrail_final_verdict": _enum_schema(RunVerdict),
        "guardrail_operational_status": _enum_schema(GuardrailOperationalStatus),
        "correction_success": {"type": "boolean"},
        "false_intervention": {"type": "boolean"},
        "state_corruption_count": {"type": "integer", "minimum": 0},
        "criterion_oscillation_count": {"type": "integer", "minimum": 0},
        "intervention_count": {
            "type": "integer",
            "minimum": 0,
            "maximum": HARD_MAX_INTERVENTIONS,
        },
        "extra_steps": {"type": "integer", "minimum": 0},
        "forced_stop": {"type": "boolean"},
        "latency_ms": nullable_number,
        "tokens_used": nullable_integer,
        "model_calls_used": nullable_integer,
    }
    metrics_properties = {
        "case_count": {"type": "integer", "minimum": 1},
        "baseline_audit_pass_count": {"type": "integer", "minimum": 0},
        "guardrail_audit_pass_count": {"type": "integer", "minimum": 0},
        "audit_success_delta": {"type": "number", "minimum": -1, "maximum": 1},
        "correction_success_count": {"type": "integer", "minimum": 0},
        "correction_success_rate": {"type": "number", "minimum": 0, "maximum": 1},
        "false_intervention_count": {"type": "integer", "minimum": 0},
        "false_intervention_rate": {"type": "number", "minimum": 0, "maximum": 1},
        "state_corruption_count": {"type": "integer", "minimum": 0},
        "state_corruption_rate": {"type": "number", "minimum": 0, "maximum": 1},
        "criterion_oscillation_count": {"type": "integer", "minimum": 0},
        "intervention_count_distribution": {
            "type": "array",
            "items": {
                "type": "integer",
                "minimum": 0,
                "maximum": HARD_MAX_INTERVENTIONS,
            },
        },
        "extra_steps_total": {"type": "integer", "minimum": 0},
        "forced_stop_count": {"type": "integer", "minimum": 0},
        "forced_stop_rate": {"type": "number", "minimum": 0, "maximum": 1},
        "latency_ms": nullable_number,
        "tokens_used": nullable_integer,
        "model_calls_used": nullable_integer,
    }
    boundary_properties = {
        "real_agent_calls": {"const": 0},
        "network_requests": {"const": 0},
        "api_key_reads": {"const": 0},
        "device_actions": {"const": 0},
        "guardrail_free_text_generations": {"const": 0},
    }
    properties = {
        "schema_version": {"const": GUARDRAIL_AB_REPORT_SCHEMA_VERSION},
        "experiment_id": {"type": "string", "minLength": 1},
        "cases": {
            "type": "array",
            "minItems": 1,
            "items": _object(case_properties, case_properties.keys()),
        },
        "metrics": _object(metrics_properties, metrics_properties.keys()),
        "execution_boundary": _object(boundary_properties, boundary_properties.keys()),
        "claim_boundary": {"const": "SYNTHETIC_PROTOCOL_AND_FAILURE_HANDLING_ONLY"},
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://harmony-eval.local/schemas/guardrail_ab_report_v1.schema.json",
        **_object(properties, properties.keys()),
    }
