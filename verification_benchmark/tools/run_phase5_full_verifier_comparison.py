"""Run Phase 5 full VLM verifier and external MobiFlow engine comparison."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Optional

from verification_benchmark.evaluation_framework.phase5_full_verifier_comparison import (
    DEFAULT_API_KEY_ENV,
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    CasePaths,
    ProviderConfig,
    build_full_comparison_report,
)
from verification_benchmark.evaluation_framework.phase5_intake import (
    Phase5IntakeError,
    write_new_json,
)


REPORT_FILE = "phase5_full_verifier_comparison_report.json"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--case",
        nargs=3,
        action="append",
        metavar=("RUN_DIR", "INTAKE_RECEIPT", "GROUND_TRUTH"),
        required=True,
    )
    parser.add_argument("--mobiflow-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--api-key-env", default=DEFAULT_API_KEY_ENV)
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--max-retries", type=int, default=1)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = _parser().parse_args(argv)
    output_dir = args.output_dir.resolve()
    if output_dir.exists():
        raise Phase5IntakeError(f"refusing to overwrite comparison output directory: {output_dir}")
    api_key = os.environ.get(args.api_key_env)
    if not api_key:
        raise Phase5IntakeError(f"required API key environment variable is unset: {args.api_key_env}")
    cases = [
        CasePaths(Path(run_dir), Path(intake_receipt), Path(ground_truth))
        for run_dir, intake_receipt, ground_truth in args.case
    ]
    report = build_full_comparison_report(
        cases=cases,
        provider=ProviderConfig(
            base_url=args.base_url,
            model=args.model,
            api_key_env=args.api_key_env,
            api_key=api_key,
            timeout=args.timeout,
            max_retries=args.max_retries,
        ),
        mobiflow_root=args.mobiflow_root,
        output_dir=output_dir,
    )
    write_new_json(output_dir / REPORT_FILE, report)
    print(
        json.dumps(
            {
                "status": "PHASE5_FULL_VERIFIER_COMPARISON_COMPLETE",
                "report": str(output_dir / REPORT_FILE),
                "summary": report["summary"],
                "external_model_calls": report["external_model_calls"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
