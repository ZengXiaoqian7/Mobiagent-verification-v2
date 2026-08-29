"""CLI for the App functional testing agent."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Optional

from .legacy_plan import build_legacy_generic_runner_plan, write_legacy_generic_runner_plan
from .manifest_executor import ManifestReplayExecutor
from .mobiagent_executor import (
    MobiAgentStepExecutor,
    prepare_mobiagent_preflight,
)
from .mock_executor import MOCK_SCENARIOS, MockStepExecutor
from .orchestrator import run_app_test
from .schema import TestCaseError, load_test_case
from .verification_runner import MobiAgentVerificationRunner


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--test-case", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--executor",
        choices=("mock", "manifest", "mobiagent"),
        default="mock",
    )
    parser.add_argument(
        "--execution-manifest",
        type=Path,
        help="app-test-execution-manifest-v1 JSON used by --executor manifest",
    )
    parser.add_argument("--run-id")
    parser.add_argument("--device", default="Harmony")
    parser.add_argument("--device-serial")
    parser.add_argument("--runner-root", type=Path)
    parser.add_argument(
        "--execute-runner",
        action="store_true",
        help="execute the real step-bound MobiAgent path; omit for non-mutating preflight",
    )
    parser.add_argument(
        "--mock-scenario",
        choices=MOCK_SCENARIOS,
        default=None,
    )
    parser.add_argument(
        "--emit-legacy-plan",
        type=Path,
        help="write a compatibility plan for verification_benchmark.tools.run_automated_evaluation",
    )
    parser.add_argument("--legacy-run-id")
    parser.add_argument("--legacy-raw-trace-root", type=Path)
    parser.add_argument("--legacy-intake-root", type=Path)
    parser.add_argument("--os-version", default="OpenHarmony")
    parser.add_argument("--runner-model", default="gpt-5.4")
    parser.add_argument("--contract-model", default="gpt-5.4-mini")
    parser.add_argument("--provider-base-url", default="https://api.horizon1123.top/v1")
    parser.add_argument("--transport", default="raw_http")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = _parser().parse_args(argv)
    test_case = load_test_case(args.test_case)
    verification_runner = None
    if args.executor == "mobiagent":
        if args.execute_runner:
            executor = MobiAgentStepExecutor(
                output_dir=args.output_dir,
                device=args.device,
                device_serial=args.device_serial,
                runner_root=args.runner_root,
            )
            verification_runner = MobiAgentVerificationRunner(
                output_dir=args.output_dir,
                device=args.device,
                device_serial=args.device_serial,
                runner_root=args.runner_root,
            )
        else:
            result = prepare_mobiagent_preflight(
                test_case,
                args.output_dir,
                run_id=args.run_id,
                device=args.device,
                device_serial=args.device_serial,
                runner_root=args.runner_root,
            )
            print(
                json.dumps(
                    {
                        "status": "MOBIAGENT_PREFLIGHT_COMPLETE",
                        **result.as_dict(),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return 0
    elif args.executor == "manifest":
        if args.execution_manifest is None:
            raise TestCaseError("--executor manifest requires --execution-manifest")
        executor = ManifestReplayExecutor(args.execution_manifest)
    else:
        executor = MockStepExecutor(
            scenario=args.mock_scenario
            or str(test_case.metadata.get("mock_scenario") or "pass")
        )
        verification_runner = None
    report = run_app_test(
        test_case,
        executor,
        args.output_dir,
        run_id=args.run_id,
        verification_runner=verification_runner,
    )
    emitted_plan = None
    if args.emit_legacy_plan is not None:
        missing = [
            name
            for name, value in (
                ("--legacy-run-id", args.legacy_run_id),
                ("--legacy-raw-trace-root", args.legacy_raw_trace_root),
                ("--legacy-intake-root", args.legacy_intake_root),
                ("--device-serial", args.device_serial),
            )
            if value is None
        ]
        if missing:
            raise TestCaseError(
                "--emit-legacy-plan requires " + ", ".join(missing)
            )
        plan = build_legacy_generic_runner_plan(
            test_case=test_case,
            run_id=str(args.legacy_run_id),
            raw_trace_root=args.legacy_raw_trace_root,
            intake_root=args.legacy_intake_root,
            device_serial=str(args.device_serial),
            os_version=args.os_version,
            runner_model=args.runner_model,
            contract_model=args.contract_model,
            provider_base_url=args.provider_base_url,
            transport=args.transport,
        )
        write_legacy_generic_runner_plan(args.emit_legacy_plan, plan)
        emitted_plan = str(args.emit_legacy_plan.resolve())
    print(
        json.dumps(
            {
                "status": "APP_TEST_COMPLETE",
                "overall_result": report["overall_result"],
                "output_dir": str(args.output_dir.resolve()),
                "legacy_plan": emitted_plan,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except TestCaseError as exc:
        raise SystemExit(str(exc)) from exc
