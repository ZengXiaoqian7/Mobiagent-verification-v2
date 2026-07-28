"""Create an explicitly non-publication single-author Ground Truth override.

This is a development-only escape hatch for live commercial-App A/B iteration.
It intentionally does not emit ``HumanAdjudication`` objects and cannot satisfy
the formal two-reviewer blind-review schema used for academic results.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Optional


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from verification_benchmark.evaluation_framework.audit_envelope import (  # noqa: E402
    audit_report_envelope_from_json_bytes,
    audit_report_envelope_sha256,
)
from verification_benchmark.evaluation_framework.human_adjudication import (  # noqa: E402
    HumanOutcomeVerdict,
)
from verification_benchmark.evaluation_framework.online_guardrail import (  # noqa: E402
    GuardrailTraceEventKind,
    guardrail_trace_from_json_bytes,
    guardrail_trace_sha256,
)
from verification_benchmark.tools.run_live_commercial_guardrail_ab import (  # noqa: E402
    _canonical_bytes,
    _strict_json_bytes,
)


OVERRIDE_SCHEMA_VERSION = "harmony-eval-development-solo-ground-truth-override-v1"
REPORT_SCHEMA_VERSION = "harmony-eval-development-solo-ground-truth-ab-report-v1"
PROVENANCE_SCHEMA_VERSION = "harmony-eval-development-solo-ground-truth-provenance-v1"
GROUND_TRUTH_AUTHORITY = "DEVELOPMENT_SINGLE_AUTHOR_OVERRIDE"
WARNING = (
    "WARNING: Single-author overridden Ground Truth. "
    "NOT FOR FINAL ACADEMIC PUBLICATION."
)
CLAIM_BOUNDARY = "BLACK_BOX_OBSERVABLE_STATE_ONLY_NO_BACKEND_CORRUPTION_CLAIM"


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _write_json(path: Path, value: Any) -> None:
    path.write_bytes(_canonical_bytes(value) + b"\n")


def _manifest_case(manifest: Mapping[str, Any], case_id: str) -> Mapping[str, Any]:
    cases = manifest.get("cases")
    if not isinstance(cases, list):
        raise ValueError("manifest cases are invalid")
    matches = [
        item
        for item in cases
        if isinstance(item, Mapping) and item.get("case_id") == case_id
    ]
    if len(matches) != 1:
        raise ValueError(f"case_id must resolve exactly once: {case_id}")
    return matches[0]


def _validate_run_identity(
    path: Path,
    *,
    experiment_id: str,
    case_id: str,
    arm: str,
    manifest_sha256: str,
) -> Mapping[str, Any]:
    value = _strict_json_bytes(path.read_bytes(), f"{arm} run identity")
    expected = {
        "experiment_id": experiment_id,
        "case_id": case_id,
        "arm": arm,
        "manifest_sha256": manifest_sha256,
        "status": "RUN_COMPLETE",
    }
    for key, expected_value in expected.items():
        if value.get(key) != expected_value:
            raise ValueError(f"{arm} run identity drift at {key}")
    if value.get("oracle_database_dependency") is not False:
        raise ValueError(f"{arm} run unexpectedly depends on an Oracle database")
    return value


def _event_count(trace: Any, kind: GuardrailTraceEventKind) -> int:
    return sum(event.event_kind is kind for event in trace.events)


def _criterion_event_count(trace: Any, kind: GuardrailTraceEventKind) -> int:
    return sum(
        len(event.criterion_ids) for event in trace.events if event.event_kind is kind
    )


def _override_payload(
    *,
    experiment_id: str,
    case_id: str,
    arm: str,
    verdict: HumanOutcomeVerdict,
    reason: str,
    audit_envelope_sha256: str,
    observable_trace_sha256: str,
) -> dict[str, Any]:
    return {
        "schema_version": OVERRIDE_SCHEMA_VERSION,
        "experiment_id": experiment_id,
        "case_id": case_id,
        "arm": arm,
        "verdict": verdict.value,
        "reason": reason,
        "ground_truth_authority": GROUND_TRUTH_AUTHORITY,
        "review_protocol": "SOLO_UNBLINDED_DEVELOPMENT_OVERRIDE",
        "single_author_assertion": True,
        "blind_review_bypassed": True,
        "reviewer_hash_validation_bypassed": True,
        "formal_human_adjudication_emitted": False,
        "publication_eligible": False,
        "warning": WARNING,
        "audit_envelope_sha256": audit_envelope_sha256,
        "observable_trace_sha256": observable_trace_sha256,
    }


def force_override_ground_truth(
    *,
    manifest_path: Path,
    case_id: str,
    baseline_verdict: HumanOutcomeVerdict,
    guardrail_verdict: HumanOutcomeVerdict,
    reason: str,
    live_output_root: Optional[Path] = None,
    output_dir: Optional[Path] = None,
    replace: bool = False,
) -> Mapping[str, Any]:
    if not isinstance(reason, str) or not reason.strip() or reason != reason.strip():
        raise ValueError("override reason must be a canonical non-empty string")
    if not isinstance(baseline_verdict, HumanOutcomeVerdict) or not isinstance(
        guardrail_verdict, HumanOutcomeVerdict
    ):
        raise ValueError("solo override verdict is invalid")

    manifest = _strict_json_bytes(manifest_path.read_bytes(), "live A/B manifest")
    experiment_id = manifest.get("experiment_id")
    if not isinstance(experiment_id, str) or not experiment_id:
        raise ValueError("manifest experiment_id is invalid")
    if manifest.get("oracle_database_dependency") is not False:
        raise ValueError("development override requires Oracle-free black-box input")
    if manifest.get("claim_boundary") != CLAIM_BOUNDARY:
        raise ValueError("manifest claim boundary drift")
    _manifest_case(manifest, case_id)
    manifest_sha = _sha256(manifest)

    live_root = live_output_root or (
        ROOT / "verification_benchmark/reports/guardrail/live" / experiment_id
    )
    live_case_dir = live_root / case_id
    destination = output_dir or live_case_dir / "development_ground_truth_override"
    if destination.exists() and any(destination.iterdir()) and not replace:
        raise ValueError(f"refusing to overwrite development override: {destination}")

    for arm in ("baseline", "guardrail"):
        _validate_run_identity(
            live_case_dir / arm / "run_identity.json",
            experiment_id=experiment_id,
            case_id=case_id,
            arm=arm,
            manifest_sha256=manifest_sha,
        )

    baseline_audit = audit_report_envelope_from_json_bytes(
        (live_case_dir / "baseline/audit_envelope.json").read_bytes()
    )
    guardrail_audit = audit_report_envelope_from_json_bytes(
        (live_case_dir / "guardrail/audit_envelope.json").read_bytes()
    )
    trace = guardrail_trace_from_json_bytes(
        (live_case_dir / "guardrail/guardrail_trace.json").read_bytes()
    )
    if not trace.events:
        raise ValueError("Guardrail trace has no events")

    baseline_audit_sha = audit_report_envelope_sha256(baseline_audit)
    guardrail_audit_sha = audit_report_envelope_sha256(guardrail_audit)
    trace_sha = guardrail_trace_sha256(trace)
    if trace.contract_sha256 != guardrail_audit.contract.contract_sha256:
        raise ValueError("Guardrail trace/Audit contract binding mismatch")
    expected_guardrail_run_id = f"{experiment_id}.{case_id}.guardrail"
    if trace.run_id != expected_guardrail_run_id:
        raise ValueError("Guardrail trace run_id drift")

    baseline_override = _override_payload(
        experiment_id=experiment_id,
        case_id=case_id,
        arm="baseline",
        verdict=baseline_verdict,
        reason=reason,
        audit_envelope_sha256=baseline_audit_sha,
        observable_trace_sha256=baseline_audit.trace.trace_sha256,
    )
    guardrail_override = _override_payload(
        experiment_id=experiment_id,
        case_id=case_id,
        arm="guardrail",
        verdict=guardrail_verdict,
        reason=reason,
        audit_envelope_sha256=guardrail_audit_sha,
        observable_trace_sha256=guardrail_audit.trace.trace_sha256,
    )

    intervention_count = _event_count(
        trace, GuardrailTraceEventKind.INTERVENTION_ISSUED
    )
    strict_regressions = _criterion_event_count(
        trace, GuardrailTraceEventKind.STATE_REGRESSION_DETECTED
    )
    unknown_losses = _criterion_event_count(
        trace, GuardrailTraceEventKind.OBSERVABLE_STATE_CORRUPTION_DETECTED
    )
    oscillations = _criterion_event_count(
        trace, GuardrailTraceEventKind.CRITERION_OSCILLATION_DETECTED
    )
    observable_corruptions = strict_regressions + unknown_losses
    eligible = HumanOutcomeVerdict.AMBIGUOUS not in {
        baseline_verdict,
        guardrail_verdict,
    }
    human_success_delta = (
        float(
            (guardrail_verdict is HumanOutcomeVerdict.PASS)
            - (baseline_verdict is HumanOutcomeVerdict.PASS)
        )
        if eligible
        else None
    )

    report = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "experiment_id": experiment_id,
        "case_id": case_id,
        "ground_truth_authority": GROUND_TRUTH_AUTHORITY,
        "development_only": True,
        "publication_eligible": False,
        "formal_blind_review_preserved": True,
        "formal_human_adjudication_emitted": False,
        "blind_review_bypassed": True,
        "reviewer_hash_validation_bypassed": True,
        "warning": WARNING,
        "oracle_database_dependency": False,
        "claim_boundary": CLAIM_BOUNDARY,
        "source_manifest_semantic_sha256": manifest_sha,
        "overrides": {
            "baseline": {
                "verdict": baseline_verdict.value,
                "override_semantic_sha256": _sha256(baseline_override),
            },
            "guardrail": {
                "verdict": guardrail_verdict.value,
                "override_semantic_sha256": _sha256(guardrail_override),
            },
        },
        "automated_observations": {
            "baseline_verdict": baseline_audit.verdict.value,
            "guardrail_verdict": guardrail_audit.verdict.value,
            "guardrail_operational_status": trace.events[-1].operational_status.value,
            "guardrail_trace_sha256": trace_sha,
            "intervention_count": intervention_count,
            "strict_state_regression_count": strict_regressions,
            "observable_state_corruption_count": observable_corruptions,
            "criterion_oscillation_count": oscillations,
        },
        "development_metrics": {
            "eligible": eligible,
            "human_success_delta": human_success_delta,
            "correction_success": (
                baseline_verdict is HumanOutcomeVerdict.FAIL
                and guardrail_verdict is HumanOutcomeVerdict.PASS
                and intervention_count > 0
            ),
            "false_intervention": (
                baseline_verdict is HumanOutcomeVerdict.PASS and intervention_count > 0
            ),
        },
    }
    provenance = {
        "schema_version": PROVENANCE_SCHEMA_VERSION,
        "experiment_id": experiment_id,
        "case_id": case_id,
        "source_manifest": manifest_path.as_posix(),
        "source_manifest_semantic_sha256": manifest_sha,
        "source_live_case_dir": live_case_dir.as_posix(),
        "baseline_audit_envelope_sha256": baseline_audit_sha,
        "guardrail_audit_envelope_sha256": guardrail_audit_sha,
        "guardrail_trace_sha256": trace_sha,
        "blind_review_bypassed": True,
        "reviewer_hash_validation_bypassed": True,
        "publication_eligible": False,
        "warning": WARNING,
    }

    destination.mkdir(parents=True, exist_ok=True)
    _write_json(destination / "baseline_solo_override.json", baseline_override)
    _write_json(destination / "guardrail_solo_override.json", guardrail_override)
    _write_json(destination / "development_ground_truth_ab_report.json", report)
    _write_json(destination / "override_provenance.json", provenance)
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Inject development-only single-author Ground Truth without changing "
            "the formal two-reviewer blind-adjudication pipeline."
        )
    )
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--case-id", required=True)
    parser.add_argument(
        "--baseline-verdict",
        type=HumanOutcomeVerdict,
        choices=list(HumanOutcomeVerdict),
        required=True,
    )
    parser.add_argument(
        "--guardrail-verdict",
        type=HumanOutcomeVerdict,
        choices=list(HumanOutcomeVerdict),
        required=True,
    )
    parser.add_argument("--reason", required=True)
    parser.add_argument("--live-output-root", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--replace", action="store_true")
    parser.add_argument(
        "--acknowledge-development-only",
        action="store_true",
        help="Required acknowledgement that the output is not publication eligible.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.acknowledge_development_only:
        raise ValueError(
            "--acknowledge-development-only is required; "
            "this output is not valid for final academic publication"
        )
    report = force_override_ground_truth(
        manifest_path=args.manifest,
        case_id=args.case_id,
        baseline_verdict=args.baseline_verdict,
        guardrail_verdict=args.guardrail_verdict,
        reason=args.reason,
        live_output_root=args.live_output_root,
        output_dir=args.output_dir,
        replace=args.replace,
    )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
