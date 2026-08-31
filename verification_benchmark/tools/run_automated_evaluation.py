"""Run the elastic trace-to-verifier-to-user-review evaluation loop."""

from __future__ import annotations

import argparse
import json
from dataclasses import replace
from pathlib import Path
from typing import Optional

from verification_benchmark.evaluation_framework.elastic_evaluation import (
    ElasticEvaluationConfig,
    run_elastic_evaluation,
)
from verification_benchmark.evaluation_framework.automated_evaluation_plan import (
    load_automated_evaluation_plan,
    plan_audit_payload,
    prepare_evaluation_cases,
    select_plan_tasks,
)
from verification_benchmark.evaluation_framework.phase5_intake import Phase5IntakeError
from verification_benchmark.evaluation_framework.phase5_trace_case import CasePaths
from verification_benchmark.evaluation_framework.mobiflow_compat import (
    MobiFlowBaselineAdapter,
)
from verification_benchmark.tools.verify_trace import _provider
from app_test_agent.mobiagent_executor import (
    MobiAgentStepExecutor,
    prepare_mobiagent_preflight,
)
from app_test_agent.harmony_native_runner import run_native_harmony_test
from app_test_agent.mock_executor import MOCK_SCENARIOS, MockStepExecutor, ScriptedStepExecutor
from app_test_agent.orchestrator import run_app_test
from app_test_agent.schema import TestCaseError, load_test_case
from app_test_agent.verification_runner import MobiAgentVerificationRunner
from console_compat import configure_utf8_console
from verification_benchmark.evaluation_framework.app_test_manifest_intake import (
    load_app_test_manifest_evidence,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--case",
        nargs=2,
        action="append",
        metavar=("RUN_DIR", "INTAKE_RECEIPT"),
    )
    parser.add_argument(
        "--plan",
        type=Path,
        help="typed task list containing existing traces and/or Runner tasks",
    )
    parser.add_argument(
        "--app-test-case",
        type=Path,
        help=(
            "app-test-case-v1 JSON; runs the App functional testing agent path "
            "instead of the legacy trace verifier path"
        ),
    )
    parser.add_argument(
        "--app-test-executor",
        choices=("mock", "manifest", "mobiagent", "harmony"),
        default="mock",
        help="executor backend for --app-test-case",
    )
    parser.add_argument(
        "--execution-manifest",
        type=Path,
        help="app-test-execution-manifest-v1 JSON used by --app-test-executor manifest",
    )
    parser.add_argument(
        "--recompute-step-gates",
        action="store_true",
        help=(
            "for manifest replay, recompute current Step Gate decisions from raw "
            "actions and observation frames instead of trusting historical gate labels"
        ),
    )
    parser.add_argument(
        "--mock-scenario",
        choices=MOCK_SCENARIOS,
        help="mock executor scenario for --app-test-case",
    )
    parser.add_argument(
        "--app-test-device",
        default="Harmony",
        help="device label written into MobiAgent App-test preflight payload",
    )
    parser.add_argument(
        "--app-test-device-serial",
        help="device serial used by --app-test-executor mobiagent --execute-runner",
    )
    parser.add_argument(
        "--execute-runner",
        action="store_true",
        help="execute device/provider Runner tasks; without this flag only preflight runs",
    )
    parser.add_argument(
        "--task-id",
        action="append",
        help="run only the selected task_id values from --plan; may be repeated",
    )
    parser.add_argument(
        "--runner-root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--raw-trace-root",
        type=Path,
        help="override the plan raw_trace_root for this run",
    )
    parser.add_argument(
        "--intake-root",
        type=Path,
        help="override the plan intake_root for this run",
    )
    parser.add_argument(
        "--progress-log",
        type=Path,
        help=(
            "append per-task orchestration events as JSONL; defaults beside "
            "--output-dir when --plan is used"
        ),
    )
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--diagnostics", action="store_true")
    parser.add_argument("--deterministic-only", action="store_true")
    parser.add_argument(
        "--enable-validated-jit",
        action="store_true",
        help=(
            "compile task-only validated JIT contracts for existing trace cases "
            "when registry/template routing misses"
        ),
    )
    parser.add_argument("--selection-key")
    parser.add_argument("--cache-dir", type=Path)
    parser.add_argument("--with-mobiflow-comparison", action="store_true")
    parser.add_argument("--mobiflow-root", type=Path)
    parser.add_argument("--base-url", default="https://api.horizon1123.top/v1")
    parser.add_argument("--model", default="gpt-5.4-mini")
    parser.add_argument("--api-key-env", default="MOBIAGENT_API_KEY")
    parser.add_argument("--transport", default=None)
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--max-retries", type=int, default=1)
    parser.add_argument("--fail-fast", action="store_true")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    configure_utf8_console()
    args = _parser().parse_args(argv)
    if args.app_test_case is not None:
        if args.case or args.plan is not None:
            raise Phase5IntakeError(
                "--app-test-case cannot be combined with --case or --plan"
            )
        test_case = load_test_case(args.app_test_case)
        if args.app_test_executor == "harmony":
            if not args.execute_runner:
                raise Phase5IntakeError(
                    "--app-test-executor harmony requires --execute-runner"
                )
            if args.app_test_device != "Harmony":
                raise Phase5IntakeError(
                    "--app-test-executor harmony requires --app-test-device Harmony"
                )
            if not args.app_test_device_serial:
                raise Phase5IntakeError(
                    "--app-test-executor harmony requires --app-test-device-serial"
                )
            result = run_native_harmony_test(
                testcase_path=args.app_test_case,
                serial=args.app_test_device_serial,
                output_dir=args.output_dir,
                timeout_seconds=60,
            )
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            return 0
        if args.app_test_executor == "mobiagent":
            if args.execute_runner:
                executor = MobiAgentStepExecutor(
                    output_dir=args.output_dir,
                    device=args.app_test_device,
                    device_serial=args.app_test_device_serial,
                    runner_root=args.runner_root,
                )
                verification_runner = MobiAgentVerificationRunner(
                    output_dir=args.output_dir,
                    device=args.app_test_device,
                    device_serial=args.app_test_device_serial,
                    runner_root=args.runner_root,
                )
                report = run_app_test(
                    test_case,
                    executor,
                    args.output_dir,
                    verification_runner=verification_runner,
                )
                print(
                    json.dumps(
                        {
                            "status": "APP_TEST_EVALUATION_COMPLETE",
                            "output_dir": str(args.output_dir.resolve()),
                            "summary": {
                                "total": 1,
                                "overall_result_counts": {
                                    str(report["overall_result"]): 1
                                },
                                "overall_result": report["overall_result"],
                                "attribution": report["attribution"]["attribution"],
                            },
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                )
                return 0
            result = prepare_mobiagent_preflight(
                test_case,
                args.output_dir,
                device=args.app_test_device,
                device_serial=args.app_test_device_serial,
                runner_root=args.runner_root,
            )
            print(
                json.dumps(
                    {
                        "status": "MOBIAGENT_PREFLIGHT_COMPLETE",
                        "paid_provider_call": result.paid_provider_call,
                        "device_mutation": result.device_mutation,
                        "output_dir": str(args.output_dir.resolve()),
                        "summary": {
                            "total": 1,
                            "step_count": result.step_count,
                            "run_id": result.run_id,
                            "payload_path": str(result.payload_path),
                            "manifest_path": str(result.manifest_path),
                        },
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return 0
        if args.app_test_executor == "manifest":
            if args.execution_manifest is None:
                raise Phase5IntakeError(
                    "--app-test-executor manifest requires --execution-manifest"
                )
            intake = load_app_test_manifest_evidence(
                test_case=test_case,
                test_case_path=args.app_test_case,
                manifest_path=args.execution_manifest,
                recompute_step_gates=args.recompute_step_gates,
            )
            executor = ScriptedStepExecutor(
                intake.execution_record,
                name="verification_benchmark_manifest_replay",
            )
        else:
            executor = MockStepExecutor(
                scenario=args.mock_scenario
                or str(test_case.metadata.get("mock_scenario") or "pass")
            )
        report = run_app_test(test_case, executor, args.output_dir)
        print(
            json.dumps(
                {
                    "status": "APP_TEST_EVALUATION_COMPLETE",
                    "output_dir": str(args.output_dir.resolve()),
                    "summary": {
                        "total": 1,
                        "overall_result_counts": {
                            str(report["overall_result"]): 1
                        },
                        "overall_result": report["overall_result"],
                        "attribution": report["attribution"]["attribution"],
                    },
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    if not args.case and args.plan is None:
        raise Phase5IntakeError(
            "at least one --case, --plan, or --app-test-case is required"
        )
    if args.with_mobiflow_comparison and args.mobiflow_root is None:
        raise Phase5IntakeError(
            "--mobiflow-root is required with --with-mobiflow-comparison"
        )
    if args.mobiflow_root is not None and not args.with_mobiflow_comparison:
        raise Phase5IntakeError(
            "--mobiflow-root has no effect without --with-mobiflow-comparison"
        )
    if args.deterministic_only and args.with_mobiflow_comparison:
        raise Phase5IntakeError(
            "MobiFlow comparison is unavailable in --deterministic-only mode"
        )
    if args.enable_validated_jit and args.deterministic_only:
        raise Phase5IntakeError(
            "--enable-validated-jit requires model access; remove --deterministic-only"
        )
    cases = [
        CasePaths(Path(run_dir), Path(intake_receipt))
        for run_dir, intake_receipt in (args.case or ())
    ]
    orchestration = None
    if args.plan is not None:
        plan = load_automated_evaluation_plan(args.plan)
        if args.task_id:
            plan = select_plan_tasks(plan, args.task_id)
        if args.raw_trace_root is not None or args.intake_root is not None:
            plan = replace(
                plan,
                raw_trace_root=(
                    args.raw_trace_root.resolve()
                    if args.raw_trace_root is not None
                    else plan.raw_trace_root
                ),
                intake_root=(
                    args.intake_root.resolve()
                    if args.intake_root is not None
                    else plan.intake_root
                ),
                metadata={
                    **dict(plan.metadata),
                    "runtime_root_overrides": {
                        "raw_trace_root": (
                            str(args.raw_trace_root.resolve())
                            if args.raw_trace_root is not None
                            else None
                        ),
                        "intake_root": (
                            str(args.intake_root.resolve())
                            if args.intake_root is not None
                            else None
                        ),
                    },
                },
            )
            if plan.raw_trace_root is not None and plan.intake_root is not None:
                raw_root = plan.raw_trace_root
                intake_root = plan.intake_root
                if (
                    raw_root == intake_root
                    or raw_root.is_relative_to(intake_root)
                    or intake_root.is_relative_to(raw_root)
                ):
                    raise Phase5IntakeError(
                        "raw_trace_root and intake_root must not overlap"
                    )
        progress_log = args.progress_log or args.output_dir.with_name(
            f"{args.output_dir.name}.orchestration_progress.jsonl"
        )

        def emit_progress(event: dict[str, object]) -> None:
            print(
                json.dumps(
                    {"status": "ORCHESTRATION_PROGRESS", "event": event},
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                flush=True,
            )

        prepared = prepare_evaluation_cases(
            plan,
            execute_runner=args.execute_runner,
            runner_root=args.runner_root,
            continue_on_runner_error=not args.fail_fast,
            progress_log=progress_log,
            progress_callback=emit_progress,
        )
        orchestration = {
            **plan_audit_payload(plan),
            "execute_runner": args.execute_runner,
            "progress_log": str(progress_log.resolve()),
            "tasks": list(prepared.task_records),
        }
        if prepared.runner_preflight_only:
            print(
                json.dumps(
                    {
                        "status": "RUNNER_PREFLIGHT_COMPLETE",
                        "paid_provider_call": False,
                        "device_mutation": False,
                        "plan": orchestration,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return 0
        cases.extend(prepared.cases)
    provider = None if args.deterministic_only else _provider(args)
    baseline_adapter = (
        MobiFlowBaselineAdapter(args.mobiflow_root, args.output_dir.resolve())
        if args.with_mobiflow_comparison
        else None
    )
    result = run_elastic_evaluation(
        cases,
        ElasticEvaluationConfig(
            provider=provider,
            include_diagnostics=args.diagnostics,
            continue_on_error=not args.fail_fast,
            selection_key=args.selection_key,
            orchestration=orchestration,
            baseline_adapter=baseline_adapter,
            cache_dir=args.cache_dir,
            enable_validated_jit=args.enable_validated_jit,
        ),
        args.output_dir,
        resume=args.resume,
    )
    runner_errors = result["summary"].get("runner_execution", {}).get(
        "error_count", 0
    )
    print(
        json.dumps(
            {
                "status": (
                    "AUTOMATED_EVALUATION_RUNNER_FAILED"
                    if runner_errors
                    else "AUTOMATED_EVALUATION_COMPLETE"
                ),
                "output_dir": result["output_dir"],
                "summary": result["summary"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    if runner_errors:
        return 2
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (Phase5IntakeError, TestCaseError) as exc:
        raise SystemExit(str(exc)) from exc
