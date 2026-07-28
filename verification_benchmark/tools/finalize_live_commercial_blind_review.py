"""Validate independent blind responses and finalize human-grounded A/B output."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Mapping, Optional


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from verification_benchmark.evaluation_framework.audit_envelope import (  # noqa: E402
    audit_report_envelope_from_json_bytes,
)
from verification_benchmark.evaluation_framework.human_adjudication import (  # noqa: E402
    HumanAdjudication,
    HumanAdjudicationResolution,
    HumanGroundedGuardrailAbComparisonReport,
    HumanOutcomeVerdict,
    HumanReviewerDecision,
    build_human_grounded_guardrail_ab_case_report,
    derive_human_grounded_guardrail_ab_metrics,
    human_adjudication_payload,
    human_grounded_ab_report_payload,
    human_json_bytes,
)
from verification_benchmark.evaluation_framework.online_guardrail import (  # noqa: E402
    GuardrailExecutionResult,
    GuardrailTraceEventKind,
    guardrail_trace_from_json_bytes,
)
from verification_benchmark.tools.prepare_live_commercial_blind_review import (  # noqa: E402
    BLIND_REVIEW_RESPONSE_SCHEMA_VERSION,
    SEALED_MAPPING_SCHEMA_VERSION,
    _file_sha256,
)
from verification_benchmark.tools.run_live_commercial_guardrail_ab import (  # noqa: E402
    _canonical_bytes,
    _strict_json_bytes,
)


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _exact_keys(value: Mapping[str, Any], expected: set[str], context: str) -> None:
    if set(value) != expected:
        raise ValueError(f"{context} keys mismatch")


def _load_response(
    path: Path,
    *,
    blind_run_id: str,
    package_sha256: str,
    rubric_sha256: str,
    available_frames: set[int],
) -> HumanReviewerDecision:
    item = _strict_json_bytes(path.read_bytes(), f"blind response {path}")
    _exact_keys(
        item,
        {
            "schema_version",
            "blind_run_id",
            "blind_package_sha256",
            "rubric_sha256",
            "reviewer_id_hash",
            "verdict",
            "evidence_frame_indices",
        },
        "blind reviewer response",
    )
    expected = {
        "schema_version": BLIND_REVIEW_RESPONSE_SCHEMA_VERSION,
        "blind_run_id": blind_run_id,
        "blind_package_sha256": package_sha256,
        "rubric_sha256": rubric_sha256,
    }
    for key, value in expected.items():
        if item[key] != value:
            raise ValueError(f"blind reviewer response drift at {key}")
    reviewer_hash = item["reviewer_id_hash"]
    if not isinstance(reviewer_hash, str) or not _SHA256.fullmatch(reviewer_hash):
        raise ValueError("blind reviewer_id_hash is invalid")
    raw_frames = item["evidence_frame_indices"]
    if not isinstance(raw_frames, list) or not raw_frames:
        raise ValueError("blind response requires evidence frames")
    frames = tuple(raw_frames)
    if frames != tuple(sorted(set(frames))) or any(
        not isinstance(value, int) or isinstance(value, bool) for value in frames
    ):
        raise ValueError("blind response evidence frames are invalid")
    if not set(frames).issubset(available_frames):
        raise ValueError("blind response cites a frame outside its package")
    try:
        verdict = HumanOutcomeVerdict(item["verdict"])
    except (TypeError, ValueError) as exc:
        raise ValueError("blind reviewer verdict is invalid") from exc
    decision = HumanReviewerDecision(
        reviewer_id_hash=reviewer_hash,
        blind_package_sha256=package_sha256,
        rubric_sha256=rubric_sha256,
        verdict=verdict,
        evidence_frame_indices=frames,
    )
    decision.validate()
    return decision


def _load_response_set(
    directory: Path,
    *,
    mapping_runs: tuple[Mapping[str, Any], ...],
    review_dir: Path,
) -> tuple[Mapping[str, HumanReviewerDecision], Mapping[str, str]]:
    expected_files = {f"{run['blind_run_id']}.json" for run in mapping_runs}
    observed_files = {path.name for path in directory.glob("*.json")}
    if observed_files != expected_files:
        raise ValueError("blind response set filenames mismatch")
    decisions = {}
    file_hashes = {}
    reviewer_hash: Optional[str] = None
    for run in mapping_runs:
        blind_id = run["blind_run_id"]
        package_path = review_dir / "packages" / blind_id / "blind_package.json"
        package = _strict_json_bytes(
            package_path.read_bytes(), f"blind package {blind_id}"
        )
        package_sha = hashlib.sha256(_canonical_bytes(package)).hexdigest()
        if package_sha != run["blind_package_sha256"]:
            raise ValueError("blind package/mapping hash mismatch")
        frames_raw = package.get("frames")
        if not isinstance(frames_raw, list):
            raise ValueError("blind package frames are invalid")
        available_frames = {
            frame["frame_index"]
            for frame in frames_raw
            if isinstance(frame, Mapping)
            and isinstance(frame.get("frame_index"), int)
            and not isinstance(frame.get("frame_index"), bool)
        }
        response_path = directory / f"{blind_id}.json"
        decision = _load_response(
            response_path,
            blind_run_id=blind_id,
            package_sha256=package_sha,
            rubric_sha256=run["rubric_sha256"],
            available_frames=available_frames,
        )
        if reviewer_hash is None:
            reviewer_hash = decision.reviewer_id_hash
        elif decision.reviewer_id_hash != reviewer_hash:
            raise ValueError("one response set must use one reviewer identity hash")
        decisions[blind_id] = decision
        file_hashes[blind_id] = _file_sha256(response_path)
    return decisions, file_hashes


def _adjudication(
    *,
    experiment_id: str,
    case_id: str,
    run: Mapping[str, Any],
    first: HumanReviewerDecision,
    second: HumanReviewerDecision,
    arbiter: Optional[HumanReviewerDecision],
) -> HumanAdjudication:
    reviewers = tuple(
        sorted((first, second), key=lambda decision: decision.reviewer_id_hash)
    )
    if reviewers[0].reviewer_id_hash == reviewers[1].reviewer_id_hash:
        raise ValueError("the two response sets must come from distinct reviewers")
    if reviewers[0].verdict is reviewers[1].verdict:
        resolution = HumanAdjudicationResolution.AGREEMENT
        final_verdict = reviewers[0].verdict
        if arbiter is not None:
            raise ValueError("arbiter response is forbidden when reviewers agree")
    else:
        if arbiter is None:
            raise ValueError(
                f"arbitration required for blind run {run['blind_run_id']}"
            )
        resolution = HumanAdjudicationResolution.ARBITRATION
        final_verdict = arbiter.verdict
    result = HumanAdjudication(
        adjudication_id=(
            f"{experiment_id}.{case_id}.{run['blind_run_id']}.human-adjudication"
        ),
        blind_run_id=run["blind_run_id"],
        observable_trace_sha256=run["observable_trace_sha256"],
        blind_package_sha256=run["blind_package_sha256"],
        rubric_sha256=run["rubric_sha256"],
        reviewer_decisions=reviewers,
        resolution=resolution,
        final_verdict=final_verdict,
        arbiter_decision=arbiter,
    )
    result.validate()
    return result


def _execution(live_case_dir: Path) -> GuardrailExecutionResult:
    trace = guardrail_trace_from_json_bytes(
        (live_case_dir / "guardrail/guardrail_trace.json").read_bytes()
    )
    events = trace.events
    if not events:
        raise ValueError("Guardrail trace has no events")
    candidate_steps = [event.candidate_step_index for event in events]
    interventions = sum(
        event.event_kind is GuardrailTraceEventKind.INTERVENTION_ISSUED
        for event in events
    )
    forced = next(
        (
            event.reason_code
            for event in reversed(events)
            if event.event_kind is GuardrailTraceEventKind.FORCED_STOP
        ),
        None,
    )
    result = GuardrailExecutionResult(
        operational_status=events[-1].operational_status,
        intervention_count=interventions,
        extra_steps=max(candidate_steps) - min(candidate_steps),
        tokens_used=None,
        model_calls_used=None,
        forced_stop_reason=forced,
        trace=trace,
        final_observable_trace_sha256=events[-1].observable_prefix_sha256,
    )
    result.validate()
    return result


def finalize_review(
    *,
    review_dir: Path,
    live_case_dir: Path,
    first_response_dir: Path,
    second_response_dir: Path,
    output_dir: Path,
    arbiter_response_dir: Optional[Path] = None,
) -> Mapping[str, Any]:
    if output_dir.exists() and any(output_dir.iterdir()):
        raise ValueError(f"refusing to overwrite human adjudication: {output_dir}")
    mapping = _strict_json_bytes(
        (review_dir / "operator_sealed/mapping.json").read_bytes(),
        "sealed blind mapping",
    )
    if mapping.get("schema_version") != SEALED_MAPPING_SCHEMA_VERSION:
        raise ValueError("sealed blind mapping version drift")
    raw_runs = mapping.get("runs")
    if (
        not isinstance(raw_runs, list)
        or len(raw_runs) != 2
        or any(not isinstance(run, Mapping) for run in raw_runs)
    ):
        raise ValueError("sealed blind mapping runs are invalid")
    runs = tuple(raw_runs)
    first, first_hashes = _load_response_set(
        first_response_dir, mapping_runs=runs, review_dir=review_dir
    )
    second, second_hashes = _load_response_set(
        second_response_dir, mapping_runs=runs, review_dir=review_dir
    )
    arbiter = None
    arbiter_hashes: Mapping[str, str] = {}
    if arbiter_response_dir is not None:
        arbiter, arbiter_hashes = _load_response_set(
            arbiter_response_dir, mapping_runs=runs, review_dir=review_dir
        )

    adjudications = {}
    for run in runs:
        blind_id = run["blind_run_id"]
        needs_arbiter = first[blind_id].verdict is not second[blind_id].verdict
        adjudications[run["arm"]] = _adjudication(
            experiment_id=mapping["experiment_id"],
            case_id=mapping["case_id"],
            run=run,
            first=first[blind_id],
            second=second[blind_id],
            arbiter=(
                arbiter[blind_id] if arbiter is not None and needs_arbiter else None
            ),
        )

    baseline_audit = audit_report_envelope_from_json_bytes(
        (live_case_dir / "baseline/audit_envelope.json").read_bytes()
    )
    guardrail_audit = audit_report_envelope_from_json_bytes(
        (live_case_dir / "guardrail/audit_envelope.json").read_bytes()
    )
    case_report = build_human_grounded_guardrail_ab_case_report(
        case_id=mapping["case_id"],
        baseline_audit_envelope=baseline_audit,
        guardrail_audit_envelope=guardrail_audit,
        baseline_adjudication=adjudications["baseline"],
        guardrail_adjudication=adjudications["guardrail"],
        execution=_execution(live_case_dir),
    )
    cases = (case_report,)
    report = HumanGroundedGuardrailAbComparisonReport(
        experiment_id=mapping["experiment_id"],
        cases=cases,
        metrics=derive_human_grounded_guardrail_ab_metrics(cases),
    )
    report.validate()

    output_dir.mkdir(parents=True, exist_ok=True)
    for arm, adjudication in adjudications.items():
        (output_dir / f"{arm}_human_adjudication.json").write_bytes(
            human_json_bytes(human_adjudication_payload(adjudication))
        )
    (output_dir / "human_grounded_guardrail_ab_report.json").write_bytes(
        human_json_bytes(human_grounded_ab_report_payload(report))
    )
    provenance = {
        "schema_version": "harmony-eval-blind-review-provenance-v1",
        "experiment_id": mapping["experiment_id"],
        "case_id": mapping["case_id"],
        "arm_hidden_during_review": True,
        "automated_verdict_hidden_during_review": True,
        "first_response_file_sha256s": first_hashes,
        "second_response_file_sha256s": second_hashes,
        "arbiter_response_file_sha256s": dict(arbiter_hashes),
    }
    (output_dir / "review_provenance.json").write_bytes(
        _canonical_bytes(provenance) + b"\n"
    )
    return human_grounded_ab_report_payload(report)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--review-dir", type=Path, required=True)
    parser.add_argument("--live-case-dir", type=Path, required=True)
    parser.add_argument("--first-response-dir", type=Path, required=True)
    parser.add_argument("--second-response-dir", type=Path, required=True)
    parser.add_argument("--arbiter-response-dir", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = finalize_review(
        review_dir=args.review_dir,
        live_case_dir=args.live_case_dir,
        first_response_dir=args.first_response_dir,
        second_response_dir=args.second_response_dir,
        arbiter_response_dir=args.arbiter_response_dir,
        output_dir=args.output_dir,
    )
    print(json.dumps(result["metrics"], ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
