"""Generate the deterministic local Phase 4 Guardrail MVP A/B artifacts."""

from __future__ import annotations

import argparse
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional, Tuple

from verification_benchmark.evaluation_framework.audit_envelope import (
    build_audit_report_envelope,
)
from verification_benchmark.evaluation_framework.event_log import (
    CriterionObservationEvent,
    DurableEventTrace,
    FrameEvidenceEvent,
    TerminationEvent,
    contract_sha256,
    event_trace_sha256,
)
from verification_benchmark.evaluation_framework.models import (
    ContractIR,
    ContractProvenanceIR,
    ContractSourceType,
    CriterionIR,
    CriterionObservation,
    CriterionStatus,
    EvidenceCapabilityProfile,
    EvidencePointer,
    ObservationState,
    RunMode,
    TemporalSemantics,
    TerminationQuality,
    TraceIntegrity,
)
from verification_benchmark.evaluation_framework.online_guardrail import (
    GuardrailAbComparisonReport,
    GuardrailAbstainAction,
    GuardrailDecision,
    GuardrailPolicy,
    GuardrailSafetyAction,
    GuardrailTraceEventKind,
    OnlineDoneInterceptor,
    DoneCandidate,
    build_guardrail_ab_case_report,
    derive_guardrail_ab_metrics,
    guardrail_ab_report_json_schema,
    guardrail_ab_report_payload,
    guardrail_ab_report_sha256,
    guardrail_feedback_json_schema,
    guardrail_feedback_payload,
    guardrail_json_bytes,
    guardrail_trace_json_schema,
    guardrail_trace_payload,
    project_observable_trace_for_audit,
)
from verification_benchmark.evaluation_framework.replay import replay_event_trace


ROOT = Path(__file__).resolve().parents[2]
SCHEMA_DIR = ROOT / "verification_benchmark" / "schemas"
OUTPUT_DIR = (
    ROOT
    / "verification_benchmark"
    / "reports"
    / "guardrail"
    / "development"
    / "mock_guardrail_ab_v1"
)


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _contract() -> ContractIR:
    return ContractIR.from_criteria(
        "development.guardrail.mock",
        (
            CriterionIR("a", TemporalSemantics.PERSISTENT_STATE),
            CriterionIR("b", TemporalSemantics.PERSISTENT_STATE),
        ),
        source="frozen-registry",
        compiler_provenance=ContractProvenanceIR(
            ContractSourceType.REGISTRY,
            "guardrail-development-registry",
            "1",
            _sha("guardrail-registry"),
            "registry/development_guardrail.json",
            "task:guardrail-development",
        ),
    )


@dataclass(frozen=True)
class ScriptedScenario:
    case_id: str
    states: Tuple[Mapping[str, CriterionStatus], ...]
    max_interventions: int = 3
    protected_criteria: Tuple[str, ...] = ()
    oscillation_threshold: Optional[int] = None
    on_abstain: GuardrailAbstainAction = GuardrailAbstainAction.FORCE_STOP_UNJUDGED
    integrity: TraceIntegrity = TraceIntegrity.VALID


def _scenarios() -> Tuple[ScriptedScenario, ...]:
    sat = CriterionStatus.SATISFIED
    violated = CriterionStatus.VIOLATED
    unknown = CriterionStatus.UNKNOWN_EVIDENCE
    missing = CriterionStatus.SOURCE_EVIDENCE_MISSING
    unsupported = CriterionStatus.UNSUPPORTED_CAPABILITY
    return tuple(
        sorted(
            (
                ScriptedScenario("already_successful", ({"a": sat, "b": sat},)),
                ScriptedScenario(
                    "invalid_force_stop",
                    ({"a": unknown, "b": unknown},),
                    integrity=TraceIntegrity.INVALID,
                ),
                ScriptedScenario(
                    "never_repairs",
                    tuple({"a": sat, "b": violated} for _ in range(4)),
                ),
                ScriptedScenario(
                    "nonprotected_regression_continues",
                    (
                        {"a": sat, "b": violated},
                        {"a": violated, "b": sat},
                        {"a": sat, "b": sat},
                    ),
                ),
                ScriptedScenario(
                    "oscillation_forced_stop",
                    (
                        {"a": sat, "b": violated},
                        {"a": unknown, "b": violated},
                        {"a": violated, "b": sat},
                        {"a": sat, "b": violated},
                    ),
                    oscillation_threshold=2,
                ),
                ScriptedScenario(
                    "premature_done_corrected",
                    (
                        {"a": sat, "b": violated},
                        {"a": sat, "b": sat},
                    ),
                ),
                ScriptedScenario(
                    "protected_regression_forced_stop",
                    (
                        {"a": sat, "b": violated},
                        {"a": violated, "b": sat},
                    ),
                    protected_criteria=("a",),
                ),
                ScriptedScenario(
                    "source_missing_force_stop_unjudged",
                    ({"a": missing, "b": missing},),
                ),
                ScriptedScenario(
                    "unknown_allow_done_unjudged",
                    ({"a": unknown, "b": unknown},),
                    on_abstain=GuardrailAbstainAction.ALLOW_DONE_UNJUDGED,
                ),
                ScriptedScenario(
                    "unsupported_escalate_human",
                    ({"a": unsupported, "b": unsupported},),
                    on_abstain=GuardrailAbstainAction.ESCALATE_HUMAN,
                ),
            ),
            key=lambda item: item.case_id,
        )
    )


def _policy(contract: ContractIR, scenario: ScriptedScenario) -> GuardrailPolicy:
    digest = contract_sha256(contract)
    return GuardrailPolicy(
        allowlist_id="development-low-risk-mock-v1",
        allowlist_sha256=_sha("development-low-risk-mock-v1"),
        allowed_contract_sha256s=(digest,),
        max_interventions=scenario.max_interventions,
        max_extra_steps=20,
        logical_deadline_seconds=30.0,
        max_tokens=None,
        max_model_calls=None,
        protected_criteria=scenario.protected_criteria,
        regression_action=GuardrailSafetyAction.FORCE_STOP,
        max_oscillations_per_criterion=scenario.oscillation_threshold,
        oscillation_action=GuardrailSafetyAction.FORCE_STOP,
        on_abstain=scenario.on_abstain,
    )


def _prefix(
    contract: ContractIR,
    states: Tuple[Mapping[str, CriterionStatus], ...],
    *,
    integrity: TraceIntegrity,
    case_id: str,
) -> DurableEventTrace:
    events = []
    sequence = 0
    for frame_index, vector in enumerate(states):
        timestamp = float(frame_index + 1)
        events.append(
            FrameEvidenceEvent(
                sequence,
                frame_index,
                observation_state=ObservationState.STABLE_SEMANTIC,
                screenshot_ref=f"frames/{frame_index}.jpg",
                timestamp=timestamp,
            )
        )
        sequence += 1
        for criterion_id in sorted(vector):
            events.append(
                CriterionObservationEvent(
                    sequence,
                    CriterionObservation(
                        criterion_id,
                        vector[criterion_id],
                        frame_index,
                        ObservationState.STABLE_SEMANTIC,
                        evidence=EvidencePointer(frame_index, "screenshot", timestamp),
                    ),
                )
            )
            sequence += 1
    final_frame = len(states) - 1
    events.append(
        TerminationEvent(
            sequence,
            TerminationQuality.ON_TIME,
            declared_done_frame=final_frame,
            declared_done_timestamp=float(final_frame + 1),
        )
    )
    trace = DurableEventTrace(
        trace_id=f"development.guardrail.{case_id}",
        contract_sha256=contract_sha256(contract),
        capability_profile=EvidenceCapabilityProfile(
            screenshot_frames=tuple(range(len(states))),
            timestamp_sources=("scripted_logical_time",),
            integrity=integrity,
        ),
        events=tuple(events),
        mode=RunMode.ONLINE_GUARDRAIL,
        source_trace_ref=f"guardrail/development/{case_id}",
        run_timestamp="2026-07-17T00:00:00Z",
    )
    trace.validate()
    return trace


def _candidate(
    contract: ContractIR,
    trace: DurableEventTrace,
    scenario: ScriptedScenario,
    ordinal: int,
) -> DoneCandidate:
    return DoneCandidate(
        run_id=f"mock.{scenario.case_id}",
        session_id=f"session.{scenario.case_id}",
        candidate_ordinal=ordinal,
        step_index=ordinal - 1,
        frame_index=ordinal - 1,
        timestamp=float(ordinal),
        contract_sha256=contract_sha256(contract),
        observable_prefix_sha256=event_trace_sha256(trace),
        tokens_used=None,
        model_calls_used=None,
    )


def _audit(contract: ContractIR, trace: DurableEventTrace):
    projected = project_observable_trace_for_audit(trace)
    report = replay_event_trace(contract, projected)
    return build_audit_report_envelope(contract, projected, report)


def build_mock_artifacts() -> tuple[GuardrailAbComparisonReport, dict[Path, bytes]]:
    contract = _contract()
    rows = []
    artifacts: dict[Path, bytes] = {
        SCHEMA_DIR
        / "guardrail_feedback_v1.schema.json": guardrail_json_bytes(
            guardrail_feedback_json_schema()
        ),
        SCHEMA_DIR
        / "guardrail_trace_v1.schema.json": guardrail_json_bytes(
            guardrail_trace_json_schema()
        ),
        SCHEMA_DIR
        / "guardrail_ab_report_v1.schema.json": guardrail_json_bytes(
            guardrail_ab_report_json_schema()
        ),
    }
    for scenario in _scenarios():
        policy = _policy(contract, scenario)
        interceptor = OnlineDoneInterceptor(
            policy=policy,
            contract=contract,
            run_id=f"mock.{scenario.case_id}",
            session_id=f"session.{scenario.case_id}",
        )
        baseline_trace = _prefix(
            contract,
            scenario.states[:1],
            integrity=scenario.integrity,
            case_id=scenario.case_id,
        )
        final_trace = baseline_trace
        for ordinal in range(1, len(scenario.states) + 1):
            final_trace = _prefix(
                contract,
                scenario.states[:ordinal],
                integrity=scenario.integrity,
                case_id=scenario.case_id,
            )
            decision = interceptor.handle_done(
                _candidate(contract, final_trace, scenario, ordinal),
                final_trace,
            )
            if decision.decision is not GuardrailDecision.INTERVENE_CONTINUE:
                break
        execution = interceptor.result()
        rows.append(
            build_guardrail_ab_case_report(
                case_id=scenario.case_id,
                baseline_audit_envelope=_audit(contract, baseline_trace),
                guardrail_audit_envelope=_audit(contract, final_trace),
                execution=execution,
            )
        )
        case_dir = OUTPUT_DIR / "cases" / scenario.case_id
        artifacts[case_dir / "guardrail_trace.json"] = guardrail_json_bytes(
            guardrail_trace_payload(execution.trace)
        )
        feedback_index = 0
        for event in execution.trace.events:
            if event.event_kind is GuardrailTraceEventKind.INTERVENTION_ISSUED:
                feedback_index += 1
                assert event.feedback is not None
                artifacts[case_dir / f"feedback_{feedback_index:02d}.json"] = (
                    guardrail_json_bytes(guardrail_feedback_payload(event.feedback))
                )
    cases = tuple(sorted(rows, key=lambda item: item.case_id))
    report = GuardrailAbComparisonReport(
        experiment_id="phase4-local-scripted-mock-guardrail-ab-v1",
        cases=cases,
        metrics=derive_guardrail_ab_metrics(cases),
    )
    report.validate()
    artifacts[OUTPUT_DIR / "guardrail_ab_report.json"] = guardrail_json_bytes(
        guardrail_ab_report_payload(report)
    )
    return report, artifacts


def _write_or_check(artifacts: Mapping[Path, bytes], *, check: bool) -> None:
    for path, expected in sorted(artifacts.items(), key=lambda item: str(item[0])):
        if check:
            if not path.is_file():
                raise SystemExit(
                    f"missing deterministic artifact: {path.relative_to(ROOT)}"
                )
            actual = path.read_bytes()
            if actual != expected:
                raise SystemExit(
                    f"deterministic artifact drift: {path.relative_to(ROOT)}"
                )
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(expected)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    report, artifacts = build_mock_artifacts()
    _write_or_check(artifacts, check=args.check)
    print(
        "MOCK_GUARDRAIL_AB_PASS; "
        f"cases={len(report.cases)}; "
        f"report_sha256={guardrail_ab_report_sha256(report)}; "
        "real_agent_calls=0; network_requests=0; api_key_reads=0; "
        "device_actions=0; guardrail_free_text_generations=0"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
