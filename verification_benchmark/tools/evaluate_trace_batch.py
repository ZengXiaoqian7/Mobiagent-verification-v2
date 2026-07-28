"""Evaluate a batch after primary trace decisions using optional frozen GT."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Optional

from verification_benchmark.evaluation_framework.mobiflow_compat import (
    MobiFlowBaselineAdapter,
)
from verification_benchmark.evaluation_framework.offline_trace_verifier import (
    VerifierConfig,
    evaluate_trace_batch,
)
from verification_benchmark.evaluation_framework.phase5_intake import (
    Phase5IntakeError,
    write_new_json,
)
from verification_benchmark.evaluation_framework.phase5_trace_case import CasePaths
from verification_benchmark.evaluation_framework.phase5_full_verifier_comparison import (
    DEFAULT_API_KEY_ENV,
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    ProviderConfig,
)


REPORT_FILE = "offline_trace_evaluation_report.json"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--with-mobiflow-comparison", action="store_true")
    parser.add_argument("--mobiflow-root", type=Path)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--api-key-env", default=DEFAULT_API_KEY_ENV)
    parser.add_argument("--transport", default=None)
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--max-retries", type=int, default=1)
    parser.add_argument("--fail-fast", action="store_true")
    parser.add_argument(
        "--case",
        nargs=3,
        action="append",
        metavar=("RUN_DIR", "INTAKE_RECEIPT", "GROUND_TRUTH"),
        required=True,
    )
    parser.add_argument(
        "--case-task-contract",
        type=Path,
        action="append",
        help="Optional per-case contract path; repeat once per --case.",
    )
    return parser


def _provider(args: argparse.Namespace) -> ProviderConfig:
    api_key = os.environ.get(args.api_key_env)
    if not api_key:
        raise Phase5IntakeError(
            f"required API key environment variable is unset: {args.api_key_env}"
        )
    return ProviderConfig(
        base_url=args.base_url,
        model=args.model,
        api_key_env=args.api_key_env,
        api_key=api_key,
        timeout=args.timeout,
        max_retries=args.max_retries,
        transport=args.transport
        or os.environ.get("MOBIAGENT_LLM_TRANSPORT", "raw_http"),
    )


def main(argv: Optional[list[str]] = None) -> int:
    args = _parser().parse_args(argv)
    output_dir = args.output_dir.resolve()
    if output_dir.exists():
        raise Phase5IntakeError(
            f"refusing to overwrite evaluation output directory: {output_dir}"
        )
    if args.with_mobiflow_comparison and args.mobiflow_root is None:
        raise Phase5IntakeError(
            "--mobiflow-root is required with --with-mobiflow-comparison"
        )
    if args.mobiflow_root is not None and not args.with_mobiflow_comparison:
        raise Phase5IntakeError(
            "--mobiflow-root has no effect without --with-mobiflow-comparison"
        )
    contracts = args.case_task_contract or []
    if contracts and len(contracts) != len(args.case):
        raise Phase5IntakeError(
            "--case-task-contract must be omitted or repeated once per --case"
        )
    if not contracts:
        contracts = [None] * len(args.case)
    cases = [
        CasePaths(
            Path(run_dir),
            Path(intake_receipt),
            Path(ground_truth),
            contract,
        )
        for (run_dir, intake_receipt, ground_truth), contract in zip(
            args.case, contracts
        )
    ]
    provider = _provider(args)
    baseline = (
        MobiFlowBaselineAdapter(args.mobiflow_root, output_dir)
        if args.with_mobiflow_comparison
        else None
    )
    report = evaluate_trace_batch(
        cases,
        VerifierConfig(provider, continue_on_error=not args.fail_fast),
        baseline_adapter=baseline,
    )
    report_path = output_dir / REPORT_FILE
    write_new_json(report_path, report)
    print(
        json.dumps(
            {
                "status": "OFFLINE_TRACE_EVALUATION_COMPLETE",
                "report": str(report_path),
                "summary": report["summary"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
