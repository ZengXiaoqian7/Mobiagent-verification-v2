"""Hash-bound human Ground Truth for black-box Guardrail A/B experiments.

Automated Audit envelopes remain immutable diagnostic facts.  This module adds
an independent, blinded human adjudication chain whose final verdict is the
authoritative A/B Ground Truth for commercial-App experiments.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Optional, Tuple

from .audit_envelope import AuditReportEnvelope, audit_report_envelope_sha256
from .models import RunVerdict
from .online_guardrail import (
    GuardrailExecutionResult,
    GuardrailOperationalStatus,
    GuardrailTraceEventKind,
    guardrail_trace_sha256,
)


HUMAN_ADJUDICATION_SCHEMA_VERSION = "harmony-eval-human-adjudication-v1"
HUMAN_GROUNDED_AB_REPORT_SCHEMA_VERSION = (
    "harmony-eval-human-grounded-guardrail-ab-report-v1"
)
HUMAN_GROUND_TRUTH_AUTHORITY = "HUMAN_ADJUDICATION_OVERRIDE"
HUMAN_ADJUDICATION_CANONICALIZER_VERSION = (
    "harmony-eval-human-adjudication-canonical-json-v1"
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class HumanOutcomeVerdict(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    AMBIGUOUS = "AMBIGUOUS"


class HumanAdjudicationResolution(str, Enum):
    AGREEMENT = "AGREEMENT"
    ARBITRATION = "ARBITRATION"


def _canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"value is not canonical JSON: {exc}") from exc


def human_json_bytes(value: Any) -> bytes:
    return _canonical_bytes(value) + b"\n"


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _validate_sha(value: Any, context: str) -> None:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError(f"{context} must be a lowercase SHA-256")


def _validate_id(value: Any, context: str) -> None:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{context} must be a canonical non-empty string")


def _validate_non_negative_int(value: Any, context: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{context} must be a non-negative integer")


@dataclass(frozen=True)
class HumanReviewerDecision:
    reviewer_id_hash: str
    blind_package_sha256: str
    rubric_sha256: str
    verdict: HumanOutcomeVerdict
    evidence_frame_indices: Tuple[int, ...]

    def validate(self) -> None:
        _validate_sha(self.reviewer_id_hash, "reviewer_id_hash")
        _validate_sha(self.blind_package_sha256, "blind_package_sha256")
        _validate_sha(self.rubric_sha256, "rubric_sha256")
        if not isinstance(self.verdict, HumanOutcomeVerdict):
            raise ValueError("human reviewer verdict is invalid")
        if (
            not isinstance(self.evidence_frame_indices, tuple)
            or not self.evidence_frame_indices
        ):
            raise ValueError("human decision requires immutable evidence frames")
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in self.evidence_frame_indices
        ):
            raise ValueError("human evidence frame index is invalid")
        if self.evidence_frame_indices != tuple(
            sorted(set(self.evidence_frame_indices))
        ):
            raise ValueError("human evidence frames must be sorted and unique")


def human_reviewer_decision_payload(value: HumanReviewerDecision) -> dict[str, Any]:
    value.validate()
    return {
        "reviewer_id_hash": value.reviewer_id_hash,
        "blind_package_sha256": value.blind_package_sha256,
        "rubric_sha256": value.rubric_sha256,
        "verdict": value.verdict.value,
        "evidence_frame_indices": list(value.evidence_frame_indices),
    }


@dataclass(frozen=True)
class HumanAdjudication:
    adjudication_id: str
    blind_run_id: str
    observable_trace_sha256: str
    blind_package_sha256: str
    rubric_sha256: str
    reviewer_decisions: Tuple[HumanReviewerDecision, HumanReviewerDecision]
    resolution: HumanAdjudicationResolution
    final_verdict: HumanOutcomeVerdict
    arbiter_decision: Optional[HumanReviewerDecision] = None
    arm_hidden_during_review: bool = True
    automated_verdict_hidden_during_review: bool = True
    schema_version: str = HUMAN_ADJUDICATION_SCHEMA_VERSION

    def validate(self) -> None:
        if self.schema_version != HUMAN_ADJUDICATION_SCHEMA_VERSION:
            raise ValueError("unsupported human adjudication schema")
        _validate_id(self.adjudication_id, "adjudication_id")
        _validate_id(self.blind_run_id, "blind_run_id")
        _validate_sha(self.observable_trace_sha256, "observable_trace_sha256")
        _validate_sha(self.blind_package_sha256, "blind_package_sha256")
        _validate_sha(self.rubric_sha256, "rubric_sha256")
        if self.arm_hidden_during_review is not True:
            raise ValueError("A/B arm must remain hidden during review")
        if self.automated_verdict_hidden_during_review is not True:
            raise ValueError("automated verdict must remain hidden during review")
        if (
            not isinstance(self.reviewer_decisions, tuple)
            or len(self.reviewer_decisions) != 2
        ):
            raise ValueError("exactly two independent reviewer decisions are required")
        for decision in self.reviewer_decisions:
            if not isinstance(decision, HumanReviewerDecision):
                raise ValueError("reviewer decision is invalid")
            decision.validate()
            if not hmac.compare_digest(
                decision.blind_package_sha256, self.blind_package_sha256
            ):
                raise ValueError("reviewer blind package hash mismatch")
            if not hmac.compare_digest(decision.rubric_sha256, self.rubric_sha256):
                raise ValueError("reviewer rubric hash mismatch")
        reviewer_hashes = tuple(
            decision.reviewer_id_hash for decision in self.reviewer_decisions
        )
        if reviewer_hashes != tuple(sorted(reviewer_hashes)):
            raise ValueError("reviewer decisions must be canonically sorted")
        if len(set(reviewer_hashes)) != 2:
            raise ValueError("reviewers must be independent and distinct")
        if not isinstance(self.resolution, HumanAdjudicationResolution):
            raise ValueError("human adjudication resolution is invalid")
        if not isinstance(self.final_verdict, HumanOutcomeVerdict):
            raise ValueError("human final verdict is invalid")

        first, second = self.reviewer_decisions
        reviewers_agree = first.verdict is second.verdict
        if reviewers_agree:
            if self.resolution is not HumanAdjudicationResolution.AGREEMENT:
                raise ValueError("reviewer agreement requires AGREEMENT resolution")
            if self.arbiter_decision is not None:
                raise ValueError("reviewer agreement cannot include an arbiter")
            if self.final_verdict is not first.verdict:
                raise ValueError("agreement verdict is not recomputable")
            return

        if self.resolution is not HumanAdjudicationResolution.ARBITRATION:
            raise ValueError("reviewer disagreement requires ARBITRATION")
        if not isinstance(self.arbiter_decision, HumanReviewerDecision):
            raise ValueError("reviewer disagreement requires an arbiter decision")
        self.arbiter_decision.validate()
        if self.arbiter_decision.reviewer_id_hash in set(reviewer_hashes):
            raise ValueError("arbiter must be distinct from both reviewers")
        if not hmac.compare_digest(
            self.arbiter_decision.blind_package_sha256, self.blind_package_sha256
        ):
            raise ValueError("arbiter blind package hash mismatch")
        if not hmac.compare_digest(
            self.arbiter_decision.rubric_sha256, self.rubric_sha256
        ):
            raise ValueError("arbiter rubric hash mismatch")
        if self.final_verdict is not self.arbiter_decision.verdict:
            raise ValueError("arbitrated verdict is not recomputable")


def human_adjudication_payload(value: HumanAdjudication) -> dict[str, Any]:
    value.validate()
    return {
        "schema_version": value.schema_version,
        "adjudication_id": value.adjudication_id,
        "blind_run_id": value.blind_run_id,
        "observable_trace_sha256": value.observable_trace_sha256,
        "blind_package_sha256": value.blind_package_sha256,
        "rubric_sha256": value.rubric_sha256,
        "reviewer_decisions": [
            human_reviewer_decision_payload(item) for item in value.reviewer_decisions
        ],
        "resolution": value.resolution.value,
        "final_verdict": value.final_verdict.value,
        "arbiter_decision": (
            human_reviewer_decision_payload(value.arbiter_decision)
            if value.arbiter_decision is not None
            else None
        ),
        "arm_hidden_during_review": value.arm_hidden_during_review,
        "automated_verdict_hidden_during_review": (
            value.automated_verdict_hidden_during_review
        ),
    }


def human_adjudication_sha256(value: HumanAdjudication) -> str:
    return _digest(human_adjudication_payload(value))


@dataclass(frozen=True)
class HumanGroundedGuardrailAbCaseReport:
    case_id: str
    baseline_audit_envelope_sha256: str
    guardrail_audit_envelope_sha256: str
    baseline_adjudication_sha256: str
    guardrail_adjudication_sha256: str
    guardrail_trace_sha256: str
    baseline_automated_verdict: RunVerdict
    guardrail_automated_verdict: RunVerdict
    baseline_human_ground_truth: HumanOutcomeVerdict
    guardrail_human_ground_truth: HumanOutcomeVerdict
    guardrail_operational_status: GuardrailOperationalStatus
    intervention_count: int
    correction_success: bool
    false_intervention: bool
    strict_state_regression_count: int
    observable_state_corruption_count: int
    criterion_oscillation_count: int
    ground_truth_authority: str = HUMAN_GROUND_TRUTH_AUTHORITY

    def validate(self) -> None:
        _validate_id(self.case_id, "human-grounded A/B case_id")
        for name, value in (
            ("baseline_audit_envelope_sha256", self.baseline_audit_envelope_sha256),
            ("guardrail_audit_envelope_sha256", self.guardrail_audit_envelope_sha256),
            ("baseline_adjudication_sha256", self.baseline_adjudication_sha256),
            ("guardrail_adjudication_sha256", self.guardrail_adjudication_sha256),
            ("guardrail_trace_sha256", self.guardrail_trace_sha256),
        ):
            _validate_sha(value, name)
        if not isinstance(self.baseline_automated_verdict, RunVerdict) or not isinstance(
            self.guardrail_automated_verdict, RunVerdict
        ):
            raise ValueError("automated A/B verdict is invalid")
        if not isinstance(
            self.baseline_human_ground_truth, HumanOutcomeVerdict
        ) or not isinstance(self.guardrail_human_ground_truth, HumanOutcomeVerdict):
            raise ValueError("human A/B Ground Truth is invalid")
        if not isinstance(self.guardrail_operational_status, GuardrailOperationalStatus):
            raise ValueError("Guardrail operational status is invalid")
        for name, value in (
            ("intervention_count", self.intervention_count),
            ("strict_state_regression_count", self.strict_state_regression_count),
            (
                "observable_state_corruption_count",
                self.observable_state_corruption_count,
            ),
            ("criterion_oscillation_count", self.criterion_oscillation_count),
        ):
            _validate_non_negative_int(value, name)
        if self.observable_state_corruption_count < self.strict_state_regression_count:
            raise ValueError("observable corruption cannot omit strict regressions")
        expected_correction = (
            self.baseline_human_ground_truth is HumanOutcomeVerdict.FAIL
            and self.guardrail_human_ground_truth is HumanOutcomeVerdict.PASS
            and self.intervention_count > 0
        )
        if self.correction_success is not expected_correction:
            raise ValueError("human-grounded correction_success is not recomputable")
        expected_false_intervention = (
            self.baseline_human_ground_truth is HumanOutcomeVerdict.PASS
            and self.intervention_count > 0
        )
        if self.false_intervention is not expected_false_intervention:
            raise ValueError("human-grounded false_intervention is not recomputable")
        if self.ground_truth_authority != HUMAN_GROUND_TRUTH_AUTHORITY:
            raise ValueError("A/B Ground Truth authority is invalid")


def human_grounded_ab_case_payload(
    value: HumanGroundedGuardrailAbCaseReport,
) -> dict[str, Any]:
    value.validate()
    return {
        "case_id": value.case_id,
        "baseline_audit_envelope_sha256": value.baseline_audit_envelope_sha256,
        "guardrail_audit_envelope_sha256": value.guardrail_audit_envelope_sha256,
        "baseline_adjudication_sha256": value.baseline_adjudication_sha256,
        "guardrail_adjudication_sha256": value.guardrail_adjudication_sha256,
        "guardrail_trace_sha256": value.guardrail_trace_sha256,
        "baseline_automated_verdict": value.baseline_automated_verdict.value,
        "guardrail_automated_verdict": value.guardrail_automated_verdict.value,
        "baseline_human_ground_truth": value.baseline_human_ground_truth.value,
        "guardrail_human_ground_truth": value.guardrail_human_ground_truth.value,
        "guardrail_operational_status": value.guardrail_operational_status.value,
        "intervention_count": value.intervention_count,
        "correction_success": value.correction_success,
        "false_intervention": value.false_intervention,
        "strict_state_regression_count": value.strict_state_regression_count,
        "observable_state_corruption_count": (
            value.observable_state_corruption_count
        ),
        "criterion_oscillation_count": value.criterion_oscillation_count,
        "ground_truth_authority": value.ground_truth_authority,
    }


def build_human_grounded_guardrail_ab_case_report(
    *,
    case_id: str,
    baseline_audit_envelope: AuditReportEnvelope,
    guardrail_audit_envelope: AuditReportEnvelope,
    baseline_adjudication: HumanAdjudication,
    guardrail_adjudication: HumanAdjudication,
    execution: GuardrailExecutionResult,
) -> HumanGroundedGuardrailAbCaseReport:
    baseline_audit_envelope.validate()
    guardrail_audit_envelope.validate()
    baseline_adjudication.validate()
    guardrail_adjudication.validate()
    execution.validate()
    if not hmac.compare_digest(
        baseline_adjudication.observable_trace_sha256,
        baseline_audit_envelope.trace.trace_sha256,
    ):
        raise ValueError("baseline adjudication/Audit trace hash mismatch")
    if not hmac.compare_digest(
        guardrail_adjudication.observable_trace_sha256,
        guardrail_audit_envelope.trace.trace_sha256,
    ):
        raise ValueError("Guardrail adjudication/Audit trace hash mismatch")
    strict_regressions = sum(
        len(event.criterion_ids)
        for event in execution.trace.events
        if event.event_kind is GuardrailTraceEventKind.STATE_REGRESSION_DETECTED
    )
    unknown_evidence_losses = sum(
        len(event.criterion_ids)
        for event in execution.trace.events
        if event.event_kind
        is GuardrailTraceEventKind.OBSERVABLE_STATE_CORRUPTION_DETECTED
    )
    oscillations = sum(
        len(event.criterion_ids)
        for event in execution.trace.events
        if event.event_kind is GuardrailTraceEventKind.CRITERION_OSCILLATION_DETECTED
    )
    baseline_gt = baseline_adjudication.final_verdict
    guardrail_gt = guardrail_adjudication.final_verdict
    row = HumanGroundedGuardrailAbCaseReport(
        case_id=case_id,
        baseline_audit_envelope_sha256=audit_report_envelope_sha256(
            baseline_audit_envelope
        ),
        guardrail_audit_envelope_sha256=audit_report_envelope_sha256(
            guardrail_audit_envelope
        ),
        baseline_adjudication_sha256=human_adjudication_sha256(
            baseline_adjudication
        ),
        guardrail_adjudication_sha256=human_adjudication_sha256(
            guardrail_adjudication
        ),
        guardrail_trace_sha256=guardrail_trace_sha256(execution.trace),
        baseline_automated_verdict=baseline_audit_envelope.verdict,
        guardrail_automated_verdict=guardrail_audit_envelope.verdict,
        baseline_human_ground_truth=baseline_gt,
        guardrail_human_ground_truth=guardrail_gt,
        guardrail_operational_status=execution.operational_status,
        intervention_count=execution.intervention_count,
        correction_success=(
            baseline_gt is HumanOutcomeVerdict.FAIL
            and guardrail_gt is HumanOutcomeVerdict.PASS
            and execution.intervention_count > 0
        ),
        false_intervention=(
            baseline_gt is HumanOutcomeVerdict.PASS
            and execution.intervention_count > 0
        ),
        strict_state_regression_count=strict_regressions,
        observable_state_corruption_count=(
            strict_regressions + unknown_evidence_losses
        ),
        criterion_oscillation_count=oscillations,
    )
    row.validate()
    return row


@dataclass(frozen=True)
class HumanGroundedGuardrailAbMetrics:
    case_count: int
    eligible_case_count: int
    ambiguous_case_count: int
    baseline_human_pass_count: int
    guardrail_human_pass_count: int
    human_success_delta: Optional[float]
    correction_success_count: int
    correction_success_rate: Optional[float]
    false_intervention_count: int
    false_intervention_rate: Optional[float]
    strict_state_regression_count: int
    observable_state_corruption_count: int
    observable_state_corruption_case_count: int
    observable_state_corruption_rate: float
    criterion_oscillation_count: int
    intervention_count_distribution: Tuple[int, ...]

    def validate(self) -> None:
        for name, value in (
            ("case_count", self.case_count),
            ("eligible_case_count", self.eligible_case_count),
            ("ambiguous_case_count", self.ambiguous_case_count),
            ("baseline_human_pass_count", self.baseline_human_pass_count),
            ("guardrail_human_pass_count", self.guardrail_human_pass_count),
            ("correction_success_count", self.correction_success_count),
            ("false_intervention_count", self.false_intervention_count),
            ("strict_state_regression_count", self.strict_state_regression_count),
            (
                "observable_state_corruption_count",
                self.observable_state_corruption_count,
            ),
            (
                "observable_state_corruption_case_count",
                self.observable_state_corruption_case_count,
            ),
            ("criterion_oscillation_count", self.criterion_oscillation_count),
        ):
            _validate_non_negative_int(value, name)
        if self.case_count < 1:
            raise ValueError("human-grounded metrics require cases")
        if self.eligible_case_count + self.ambiguous_case_count != self.case_count:
            raise ValueError("eligible and ambiguous case counts do not partition cases")
        for name, value in (
            ("human_success_delta", self.human_success_delta),
            ("correction_success_rate", self.correction_success_rate),
            ("false_intervention_rate", self.false_intervention_rate),
        ):
            if self.eligible_case_count == 0:
                if value is not None:
                    raise ValueError(f"{name} must be null without eligible cases")
            elif (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(value)
                or value < -1
                or value > 1
            ):
                raise ValueError(f"{name} is invalid")
        if (
            not isinstance(self.observable_state_corruption_rate, (int, float))
            or isinstance(self.observable_state_corruption_rate, bool)
            or not math.isfinite(self.observable_state_corruption_rate)
            or self.observable_state_corruption_rate < 0
            or self.observable_state_corruption_rate > 1
        ):
            raise ValueError("observable_state_corruption_rate is invalid")
        if not isinstance(self.intervention_count_distribution, tuple) or any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in self.intervention_count_distribution
        ):
            raise ValueError("intervention distribution is invalid")


def derive_human_grounded_guardrail_ab_metrics(
    cases: Tuple[HumanGroundedGuardrailAbCaseReport, ...],
) -> HumanGroundedGuardrailAbMetrics:
    if not cases:
        raise ValueError("cannot derive human-grounded metrics without cases")
    for case in cases:
        case.validate()
    eligible = tuple(
        case
        for case in cases
        if HumanOutcomeVerdict.AMBIGUOUS
        not in (
            case.baseline_human_ground_truth,
            case.guardrail_human_ground_truth,
        )
    )
    eligible_count = len(eligible)
    baseline_pass = sum(
        case.baseline_human_ground_truth is HumanOutcomeVerdict.PASS
        for case in eligible
    )
    guardrail_pass = sum(
        case.guardrail_human_ground_truth is HumanOutcomeVerdict.PASS
        for case in eligible
    )
    corrections = sum(case.correction_success for case in eligible)
    false_interventions = sum(case.false_intervention for case in eligible)
    corruption_cases = sum(
        case.observable_state_corruption_count > 0 for case in cases
    )
    metrics = HumanGroundedGuardrailAbMetrics(
        case_count=len(cases),
        eligible_case_count=eligible_count,
        ambiguous_case_count=len(cases) - eligible_count,
        baseline_human_pass_count=baseline_pass,
        guardrail_human_pass_count=guardrail_pass,
        human_success_delta=(
            (guardrail_pass - baseline_pass) / eligible_count
            if eligible_count
            else None
        ),
        correction_success_count=corrections,
        correction_success_rate=(
            corrections / eligible_count if eligible_count else None
        ),
        false_intervention_count=false_interventions,
        false_intervention_rate=(
            false_interventions / eligible_count if eligible_count else None
        ),
        strict_state_regression_count=sum(
            case.strict_state_regression_count for case in cases
        ),
        observable_state_corruption_count=sum(
            case.observable_state_corruption_count for case in cases
        ),
        observable_state_corruption_case_count=corruption_cases,
        observable_state_corruption_rate=corruption_cases / len(cases),
        criterion_oscillation_count=sum(
            case.criterion_oscillation_count for case in cases
        ),
        intervention_count_distribution=tuple(
            sorted(case.intervention_count for case in cases)
        ),
    )
    metrics.validate()
    return metrics


def _human_metrics_payload(value: HumanGroundedGuardrailAbMetrics) -> dict[str, Any]:
    value.validate()
    return {
        "case_count": value.case_count,
        "eligible_case_count": value.eligible_case_count,
        "ambiguous_case_count": value.ambiguous_case_count,
        "baseline_human_pass_count": value.baseline_human_pass_count,
        "guardrail_human_pass_count": value.guardrail_human_pass_count,
        "human_success_delta": value.human_success_delta,
        "correction_success_count": value.correction_success_count,
        "correction_success_rate": value.correction_success_rate,
        "false_intervention_count": value.false_intervention_count,
        "false_intervention_rate": value.false_intervention_rate,
        "strict_state_regression_count": value.strict_state_regression_count,
        "observable_state_corruption_count": (
            value.observable_state_corruption_count
        ),
        "observable_state_corruption_case_count": (
            value.observable_state_corruption_case_count
        ),
        "observable_state_corruption_rate": value.observable_state_corruption_rate,
        "criterion_oscillation_count": value.criterion_oscillation_count,
        "intervention_count_distribution": list(
            value.intervention_count_distribution
        ),
    }


@dataclass(frozen=True)
class HumanGroundedGuardrailAbComparisonReport:
    experiment_id: str
    cases: Tuple[HumanGroundedGuardrailAbCaseReport, ...]
    metrics: HumanGroundedGuardrailAbMetrics
    oracle_database_dependency: bool = False
    schema_version: str = HUMAN_GROUNDED_AB_REPORT_SCHEMA_VERSION

    def validate(self) -> None:
        if self.schema_version != HUMAN_GROUNDED_AB_REPORT_SCHEMA_VERSION:
            raise ValueError("unsupported human-grounded A/B report schema")
        _validate_id(self.experiment_id, "human-grounded experiment_id")
        if not isinstance(self.cases, tuple) or not self.cases:
            raise ValueError("human-grounded A/B cases must be non-empty")
        for case in self.cases:
            if not isinstance(case, HumanGroundedGuardrailAbCaseReport):
                raise ValueError("human-grounded A/B case is invalid")
            case.validate()
        ids = tuple(case.case_id for case in self.cases)
        if ids != tuple(sorted(set(ids))):
            raise ValueError("human-grounded cases must be sorted with unique ids")
        if self.metrics != derive_human_grounded_guardrail_ab_metrics(self.cases):
            raise ValueError("human-grounded metrics are not recomputable")
        if self.oracle_database_dependency is not False:
            raise ValueError("commercial-App A/B cannot depend on an Oracle database")


def human_grounded_ab_report_payload(
    value: HumanGroundedGuardrailAbComparisonReport,
) -> dict[str, Any]:
    value.validate()
    return {
        "schema_version": value.schema_version,
        "experiment_id": value.experiment_id,
        "cases": [human_grounded_ab_case_payload(case) for case in value.cases],
        "metrics": _human_metrics_payload(value.metrics),
        "ground_truth_authority": HUMAN_GROUND_TRUTH_AUTHORITY,
        "oracle_database_dependency": value.oracle_database_dependency,
        "claim_boundary": "BLACK_BOX_OBSERVABLE_STATE_ONLY_NO_BACKEND_CORRUPTION_CLAIM",
    }


def human_grounded_ab_report_sha256(
    value: HumanGroundedGuardrailAbComparisonReport,
) -> str:
    return _digest(human_grounded_ab_report_payload(value))


def _strict_json(data: bytes) -> Mapping[str, Any]:
    def pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON constant: {value}")

    try:
        value = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=pairs_hook,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid JSON: {exc}") from exc
    if not isinstance(value, Mapping):
        raise ValueError("human adjudication payload must be an object")
    return value


def _keys(value: Any, expected: set[str], context: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ValueError(f"{context} keys mismatch")
    return value


def _reviewer_from_payload(value: Any) -> HumanReviewerDecision:
    item = _keys(
        value,
        {
            "reviewer_id_hash",
            "blind_package_sha256",
            "rubric_sha256",
            "verdict",
            "evidence_frame_indices",
        },
        "reviewer decision",
    )
    frames = item["evidence_frame_indices"]
    if not isinstance(frames, list):
        raise ValueError("reviewer evidence frames must be an array")
    decision = HumanReviewerDecision(
        reviewer_id_hash=item["reviewer_id_hash"],
        blind_package_sha256=item["blind_package_sha256"],
        rubric_sha256=item["rubric_sha256"],
        verdict=HumanOutcomeVerdict(item["verdict"]),
        evidence_frame_indices=tuple(frames),
    )
    decision.validate()
    return decision


def human_adjudication_from_json_bytes(data: bytes) -> HumanAdjudication:
    item = _keys(
        _strict_json(data),
        {
            "schema_version",
            "adjudication_id",
            "blind_run_id",
            "observable_trace_sha256",
            "blind_package_sha256",
            "rubric_sha256",
            "reviewer_decisions",
            "resolution",
            "final_verdict",
            "arbiter_decision",
            "arm_hidden_during_review",
            "automated_verdict_hidden_during_review",
        },
        "human adjudication",
    )
    reviewers = item["reviewer_decisions"]
    if not isinstance(reviewers, list):
        raise ValueError("reviewer decisions must be an array")
    arbiter_raw = item["arbiter_decision"]
    result = HumanAdjudication(
        adjudication_id=item["adjudication_id"],
        blind_run_id=item["blind_run_id"],
        observable_trace_sha256=item["observable_trace_sha256"],
        blind_package_sha256=item["blind_package_sha256"],
        rubric_sha256=item["rubric_sha256"],
        reviewer_decisions=tuple(_reviewer_from_payload(value) for value in reviewers),
        resolution=HumanAdjudicationResolution(item["resolution"]),
        final_verdict=HumanOutcomeVerdict(item["final_verdict"]),
        arbiter_decision=(
            _reviewer_from_payload(arbiter_raw) if arbiter_raw is not None else None
        ),
        arm_hidden_during_review=item["arm_hidden_during_review"],
        automated_verdict_hidden_during_review=(
            item["automated_verdict_hidden_during_review"]
        ),
        schema_version=item["schema_version"],
    )
    result.validate()
    return result


def human_adjudication_json_schema() -> dict[str, Any]:
    sha = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
    reviewer = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "reviewer_id_hash",
            "blind_package_sha256",
            "rubric_sha256",
            "verdict",
            "evidence_frame_indices",
        ],
        "properties": {
            "reviewer_id_hash": sha,
            "blind_package_sha256": sha,
            "rubric_sha256": sha,
            "verdict": {
                "type": "string",
                "enum": [value.value for value in HumanOutcomeVerdict],
            },
            "evidence_frame_indices": {
                "type": "array",
                "minItems": 1,
                "uniqueItems": True,
                "items": {"type": "integer", "minimum": 0},
            },
        },
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://harmony-eval.local/schemas/human_adjudication_v1.schema.json",
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema_version",
            "adjudication_id",
            "blind_run_id",
            "observable_trace_sha256",
            "blind_package_sha256",
            "rubric_sha256",
            "reviewer_decisions",
            "resolution",
            "final_verdict",
            "arbiter_decision",
            "arm_hidden_during_review",
            "automated_verdict_hidden_during_review",
        ],
        "properties": {
            "schema_version": {"const": HUMAN_ADJUDICATION_SCHEMA_VERSION},
            "adjudication_id": {"type": "string", "minLength": 1},
            "blind_run_id": {"type": "string", "minLength": 1},
            "observable_trace_sha256": sha,
            "blind_package_sha256": sha,
            "rubric_sha256": sha,
            "reviewer_decisions": {
                "type": "array",
                "minItems": 2,
                "maxItems": 2,
                "items": reviewer,
            },
            "resolution": {
                "type": "string",
                "enum": [value.value for value in HumanAdjudicationResolution],
            },
            "final_verdict": {
                "type": "string",
                "enum": [value.value for value in HumanOutcomeVerdict],
            },
            "arbiter_decision": {"oneOf": [reviewer, {"type": "null"}]},
            "arm_hidden_during_review": {"const": True},
            "automated_verdict_hidden_during_review": {"const": True},
        },
    }


def human_grounded_ab_report_json_schema() -> dict[str, Any]:
    sha = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
    verdict = {"type": "string", "enum": [value.value for value in RunVerdict]}
    human_verdict = {
        "type": "string",
        "enum": [value.value for value in HumanOutcomeVerdict],
    }
    nullable_number = {"oneOf": [{"type": "number"}, {"type": "null"}]}
    case = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "case_id",
            "baseline_audit_envelope_sha256",
            "guardrail_audit_envelope_sha256",
            "baseline_adjudication_sha256",
            "guardrail_adjudication_sha256",
            "guardrail_trace_sha256",
            "baseline_automated_verdict",
            "guardrail_automated_verdict",
            "baseline_human_ground_truth",
            "guardrail_human_ground_truth",
            "guardrail_operational_status",
            "intervention_count",
            "correction_success",
            "false_intervention",
            "strict_state_regression_count",
            "observable_state_corruption_count",
            "criterion_oscillation_count",
            "ground_truth_authority",
        ],
        "properties": {
            "case_id": {"type": "string", "minLength": 1},
            "baseline_audit_envelope_sha256": sha,
            "guardrail_audit_envelope_sha256": sha,
            "baseline_adjudication_sha256": sha,
            "guardrail_adjudication_sha256": sha,
            "guardrail_trace_sha256": sha,
            "baseline_automated_verdict": verdict,
            "guardrail_automated_verdict": verdict,
            "baseline_human_ground_truth": human_verdict,
            "guardrail_human_ground_truth": human_verdict,
            "guardrail_operational_status": {
                "type": "string",
                "enum": [value.value for value in GuardrailOperationalStatus],
            },
            "intervention_count": {"type": "integer", "minimum": 0},
            "correction_success": {"type": "boolean"},
            "false_intervention": {"type": "boolean"},
            "strict_state_regression_count": {"type": "integer", "minimum": 0},
            "observable_state_corruption_count": {
                "type": "integer",
                "minimum": 0,
            },
            "criterion_oscillation_count": {"type": "integer", "minimum": 0},
            "ground_truth_authority": {"const": HUMAN_GROUND_TRUTH_AUTHORITY},
        },
    }
    metric_names = [
        "case_count",
        "eligible_case_count",
        "ambiguous_case_count",
        "baseline_human_pass_count",
        "guardrail_human_pass_count",
        "correction_success_count",
        "false_intervention_count",
        "strict_state_regression_count",
        "observable_state_corruption_count",
        "observable_state_corruption_case_count",
        "criterion_oscillation_count",
    ]
    metrics = {
        "type": "object",
        "additionalProperties": False,
        "required": metric_names
        + [
            "human_success_delta",
            "correction_success_rate",
            "false_intervention_rate",
            "observable_state_corruption_rate",
            "intervention_count_distribution",
        ],
        "properties": {
            **{
                name: {"type": "integer", "minimum": 0}
                for name in metric_names
            },
            "human_success_delta": nullable_number,
            "correction_success_rate": nullable_number,
            "false_intervention_rate": nullable_number,
            "observable_state_corruption_rate": {
                "type": "number",
                "minimum": 0,
                "maximum": 1,
            },
            "intervention_count_distribution": {
                "type": "array",
                "items": {"type": "integer", "minimum": 0},
            },
        },
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": (
            "https://harmony-eval.local/schemas/"
            "human_grounded_guardrail_ab_report_v1.schema.json"
        ),
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema_version",
            "experiment_id",
            "cases",
            "metrics",
            "ground_truth_authority",
            "oracle_database_dependency",
            "claim_boundary",
        ],
        "properties": {
            "schema_version": {"const": HUMAN_GROUNDED_AB_REPORT_SCHEMA_VERSION},
            "experiment_id": {"type": "string", "minLength": 1},
            "cases": {"type": "array", "minItems": 1, "items": case},
            "metrics": metrics,
            "ground_truth_authority": {"const": HUMAN_GROUND_TRUTH_AUTHORITY},
            "oracle_database_dependency": {"const": False},
            "claim_boundary": {
                "const": "BLACK_BOX_OBSERVABLE_STATE_ONLY_NO_BACKEND_CORRUPTION_CLAIM"
            },
        },
    }
