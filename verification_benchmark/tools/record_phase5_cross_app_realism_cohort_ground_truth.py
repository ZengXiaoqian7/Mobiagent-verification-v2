"""Record one live-observed Phase 5 final-cohort verdict."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Optional

from verification_benchmark.evaluation_framework.phase5_cross_app_realism_cohort import (
    Phase5IntakeError,
    load_experiment_manifest,
)
from verification_benchmark.evaluation_framework.phase5_cross_app_realism_cohort_single_operator import (
    FAILURE_CODES,
    OBSERVATION_KEYS,
    OBSERVATION_STATES,
    VERDICTS,
    build_cohort_single_operator_ground_truth,
)
from verification_benchmark.evaluation_framework.phase5_intake import write_new_json


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MANIFEST = (
    REPO_ROOT
    / "verification_benchmark/experiments/phase5_cross_app_realism_cohort_v1.json"
)
OUTPUT_FILE = "phase5_cross_app_realism_cohort_single_operator_ground_truth.json"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--intake-receipt", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--operator-alias", required=True)
    parser.add_argument("--verdict", choices=sorted(VERDICTS), required=True)
    parser.add_argument(
        "--failure-code", choices=sorted(FAILURE_CODES), action="append", default=[]
    )
    parser.add_argument(
        "--source-evidence-frame", type=int, action="append", default=[]
    )
    parser.add_argument(
        "--target-evidence-frame", type=int, action="append", default=[]
    )
    parser.add_argument("--observed-transfer-slot", action="append", default=[])
    parser.add_argument("--observed-target-query", required=True)
    for key in sorted(OBSERVATION_KEYS):
        parser.add_argument(
            f"--{key.replace('_', '-')}",
            dest=key,
            choices=sorted(OBSERVATION_STATES),
            required=True,
        )
    parser.add_argument("--notes", required=True)
    parser.add_argument("--observed-live", action="store_true")
    parser.add_argument(
        "--acknowledge-single-operator-nonpublication", action="store_true"
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = _parser().parse_args(argv)
    output_dir = args.output_dir.resolve()
    if output_dir.exists():
        raise Phase5IntakeError(
            f"refusing to overwrite final-cohort Ground Truth directory: {output_dir}"
        )
    result = build_cohort_single_operator_ground_truth(
        experiment_manifest=load_experiment_manifest(args.manifest),
        intake_receipt_path=args.intake_receipt.resolve(strict=True),
        run_dir=args.run_dir.resolve(strict=True),
        operator_alias=args.operator_alias,
        verdict=args.verdict,
        failure_codes=args.failure_code,
        source_evidence_frames=args.source_evidence_frame,
        target_evidence_frames=args.target_evidence_frame,
        observed_transfer_slots=args.observed_transfer_slot,
        observed_target_query=args.observed_target_query,
        rubric_observations={key: getattr(args, key) for key in OBSERVATION_KEYS},
        notes=args.notes,
        observed_live=args.observed_live,
        acknowledged_nonpublication=args.acknowledge_single_operator_nonpublication,
    )
    write_new_json(output_dir / OUTPUT_FILE, result)
    print(
        json.dumps(
            {
                "status": result["ground_truth_status"],
                "run_id": result["run_id"],
                "task_id": result["task_id"],
                "verdict": result["verdict"],
                "publication_eligible": result["publication_eligible"],
                "output": str(output_dir / OUTPUT_FILE),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
