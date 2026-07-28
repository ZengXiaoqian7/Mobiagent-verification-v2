"""Run a development-only Phase 5 verifier smoke on frozen single-operator GT traces."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Optional

from verification_benchmark.evaluation_framework.phase5_development_verifier_smoke import (
    CasePaths,
    build_report,
)
from verification_benchmark.evaluation_framework.phase5_intake import Phase5IntakeError
from verification_benchmark.evaluation_framework.phase5_intake import write_new_json


REPORT_FILE = "phase5_development_verifier_smoke_report.json"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--case",
        nargs=3,
        action="append",
        metavar=("RUN_DIR", "INTAKE_RECEIPT", "GROUND_TRUTH"),
        required=True,
        help="One frozen-GT case. Repeat for multiple cases.",
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = _parser().parse_args(argv)
    output_dir = args.output_dir.resolve()
    if output_dir.exists():
        raise Phase5IntakeError(f"refusing to overwrite smoke report directory: {output_dir}")
    cases = [
        CasePaths(Path(run_dir), Path(intake_receipt), Path(ground_truth))
        for run_dir, intake_receipt, ground_truth in args.case
    ]
    report = build_report(cases)
    write_new_json(output_dir / REPORT_FILE, report)
    print(
        json.dumps(
            {
                "status": "PHASE5_DEVELOPMENT_VERIFIER_SMOKE_COMPLETE",
                "report": str(output_dir / REPORT_FILE),
                "summary": report["summary"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
