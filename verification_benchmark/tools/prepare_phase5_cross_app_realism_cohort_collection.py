"""Prepare or execute one callback-free Phase 5 final cross-App realism task."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping, Optional

from verification_benchmark.evaluation_framework.phase5_cross_app_realism_cohort import (
    AUTHORIZED_KEY_ENV,
    AUTHORIZED_PROVIDER_BASE_URL,
    AUTHORIZED_PROVIDER_MODEL,
    AUTHORIZED_SERIAL,
    AUTHORIZED_TRANSPORT,
    COLLECTION_SCHEMA_VERSION,
    COLLECTOR_VERSION,
    PACKAGE_PROBE_REPORT_SHA256,
    PILOT_GT_FILE_SHA256,
    PENDING_GROUND_TRUTH,
    RUN_ID,
    Phase5IntakeError,
    file_sha256,
    find_task,
    load_experiment_manifest,
    semantic_sha256,
    validate_collection_run_manifest,
    validate_package_probe_report,
    validate_pilot_ground_truth,
)
from verification_benchmark.evaluation_framework.phase5_intake import write_new_json
from verification_benchmark.evaluation_framework.harmony_device_readiness import (
    wait_for_hdc_shell,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MANIFEST = (
    REPO_ROOT
    / "verification_benchmark/experiments/phase5_cross_app_realism_cohort_v1.json"
)
DEFAULT_PILOT_GT = (
    Path("D:/Lab")
    / "phase5-harmony-gt/p5r-2026071700000003/phase5_cross_app_realism_single_operator_ground_truth.json"
)
DEFAULT_RUNNER_ROOT = REPO_ROOT
RUNNER_RELATIVE_PATH = Path("runner") / "mobiagent" / "mobiagent.py"
START_RECORD = "phase5_realism_cohort_collection_start.json"
RUN_MANIFEST = "phase5_realism_cohort_collection_run_manifest.json"
TASK_FILE = "phase5_realism_cohort_runner_task.json"


def _git_head(root: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _runner_command(*, task_file: Path, output_dir: Path) -> list[str]:
    return [
        sys.executable,
        "-m",
        "runner.mobiagent.mobiagent",
        "--device",
        "Harmony",
        "--device_serial",
        AUTHORIZED_SERIAL,
        "--task_file",
        str(task_file),
        "--data_dir",
        str(output_dir),
        "--use_qwen3",
        "on",
        "--use_experience",
        "off",
        "--user_profile",
        "off",
        "--use_graphrag",
        "off",
        "--accept_planner_changes",
        "off",
        "--decider_protocol",
        "qwen_json",
        "--coord_mode",
        "resized_pixel",
        "--e2e",
    ]


def _record(
    *,
    manifest: Mapping[str, Any],
    task: Mapping[str, Any],
    run_id: str,
    attempt_ordinal: int,
    runner_sha256: str,
    collector_sha256: str,
    evaluation_git_head: str,
    runner_repository_git_head: str,
    status: str,
    runner_exit_code: Optional[int],
) -> Mapping[str, Any]:
    cohort = manifest["cohort"]
    agent = manifest["agent"]
    return validate_collection_run_manifest(
        {
            "schema_version": COLLECTION_SCHEMA_VERSION,
            "collector_version": COLLECTOR_VERSION,
            "experiment_id": manifest["experiment_id"],
            "experiment_manifest_sha256": semantic_sha256(manifest),
            "package_probe_report_sha256": PACKAGE_PROBE_REPORT_SHA256,
            "pilot_ground_truth_sha256": PILOT_GT_FILE_SHA256,
            "run_id": run_id,
            "task_id": task["task_id"],
            "task_text_sha256": hashlib.sha256(
                task["task_text"].encode("utf-8")
            ).hexdigest(),
            "initial_app": task["initial_app"],
            "initial_package": task["initial_package"],
            "target_app": task["target_app"],
            "target_package": task["target_package"],
            "trace_relpath": f"{task['initial_app']}/{task['task_family']}/1",
            "device": {
                "serial": cohort["device_serial"],
                "model": cohort["device_model"],
                "device_type": cohort["device_type"],
                "posture": cohort["posture"],
                "resolution": cohort["resolution"],
                "os_version": cohort["os_version"],
            },
            "installed_apps": manifest["package_probe"]["installed_apps"],
            "agent": {
                "provider_base_url": agent["provider_base_url"],
                "model": agent["model"],
                "transport": agent["transport"],
                "runner_module_sha256": runner_sha256,
                "collector_source_sha256": collector_sha256,
                "evaluation_git_head": evaluation_git_head,
                "runner_repository_git_head": runner_repository_git_head,
            },
            "collection_status": status,
            "attempt_ordinal": attempt_ordinal,
            "oracle_database_dependency": False,
            "ground_truth_status": PENDING_GROUND_TRUTH,
            "guardrail_callback_enabled": False,
            "start_state_guard_enabled": False,
            "runner_exit_code": runner_exit_code,
        }
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--pilot-ground-truth", type=Path, default=DEFAULT_PILOT_GT)
    parser.add_argument("--package-probe-report", type=Path, required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--os-version")
    parser.add_argument("--attempt-ordinal", type=int, default=1)
    parser.add_argument("--runner-root", type=Path, default=DEFAULT_RUNNER_ROOT)
    parser.add_argument("--execute", action="store_true")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = _parser().parse_args(argv)
    manifest = load_experiment_manifest(args.manifest)
    task = find_task(manifest, args.task_id)
    validate_pilot_ground_truth(
        manifest=manifest, ground_truth_path=args.pilot_ground_truth
    )
    validate_package_probe_report(
        manifest=manifest, report_path=args.package_probe_report
    )
    if not RUN_ID.fullmatch(args.run_id):
        raise Phase5IntakeError("run-id must match p5r-[0-9a-f]{16}")
    output_dir = args.output_dir.resolve()
    if output_dir.name != args.run_id:
        raise Phase5IntakeError("output directory basename must equal run-id")
    if output_dir.exists():
        raise Phase5IntakeError(f"refusing to reuse output directory: {output_dir}")
    if args.attempt_ordinal <= 0:
        raise Phase5IntakeError("attempt-ordinal must be positive")
    runner_root = args.runner_root.resolve(strict=True)
    runner_path = runner_root / RUNNER_RELATIVE_PATH
    runner_sha = file_sha256(runner_path)
    if runner_sha != manifest["agent"]["runner_module_sha256"]:
        raise Phase5IntakeError("Runner hash drift; freeze a new manifest")
    command = _runner_command(task_file=output_dir / TASK_FILE, output_dir=output_dir)
    summary = {
        "status": "PREFLIGHT_OK" if not args.execute else "READY_TO_EXECUTE",
        "paid_provider_call": args.execute,
        "device_mutation": args.execute,
        "experiment_id": manifest["experiment_id"],
        "experiment_manifest_sha256": semantic_sha256(manifest),
        "pilot_ground_truth_sha256": PILOT_GT_FILE_SHA256,
        "package_probe_report_sha256": PACKAGE_PROBE_REPORT_SHA256,
        "task_id": task["task_id"],
        "task_text": task["task_text"],
        "run_id": args.run_id,
        "output_dir": str(output_dir),
        "runner_module_sha256": runner_sha,
        "model": manifest["agent"]["model"],
        "transport": manifest["agent"]["transport"],
        "initial_app": task["initial_app"],
        "target_app": task["target_app"],
        "guardrail_callback_enabled": False,
        "start_state_guard_enabled": False,
        "ground_truth_status": PENDING_GROUND_TRUTH,
        "os_version": args.os_version or "UNVERIFIED_DRY_RUN",
        "command": command,
    }
    if not args.execute:
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        return 0
    if args.os_version != manifest["cohort"]["os_version"]:
        raise Phase5IntakeError("--os-version must exactly match the frozen cohort")
    if not os.getenv(AUTHORIZED_KEY_ENV, "").strip():
        raise Phase5IntakeError(f"{AUTHORIZED_KEY_ENV} is required for execution")
    readiness = wait_for_hdc_shell(AUTHORIZED_SERIAL)
    summary["hdc_readiness"] = {
        "serial": readiness.serial,
        "attempts_used": readiness.attempts_used,
    }
    common = {
        "manifest": manifest,
        "task": task,
        "run_id": args.run_id,
        "attempt_ordinal": args.attempt_ordinal,
        "runner_sha256": runner_sha,
        "collector_sha256": file_sha256(Path(__file__)),
        "evaluation_git_head": _git_head(REPO_ROOT),
        "runner_repository_git_head": _git_head(runner_root),
    }
    output_dir.mkdir(parents=True, exist_ok=False)
    write_new_json(
        output_dir / TASK_FILE,
        [
            {
                "app": task["initial_app"],
                "type": task["task_family"],
                "tasks": [task["task_text"]],
            }
        ],
    )
    write_new_json(
        output_dir / START_RECORD,
        _record(**common, status="RUN_IN_PROGRESS", runner_exit_code=None),
    )
    env = os.environ.copy()
    env.update(
        {
            "MOBIAGENT_BASE_URL": AUTHORIZED_PROVIDER_BASE_URL,
            "MOBIAGENT_DECIDER_BASE_URL": AUTHORIZED_PROVIDER_BASE_URL,
            "MOBIAGENT_GROUNDER_BASE_URL": AUTHORIZED_PROVIDER_BASE_URL,
            "MOBIAGENT_PLANNER_BASE_URL": AUTHORIZED_PROVIDER_BASE_URL,
            "MOBIAGENT_MODEL": AUTHORIZED_PROVIDER_MODEL,
            "MOBIAGENT_DECIDER_MODEL": AUTHORIZED_PROVIDER_MODEL,
            "MOBIAGENT_GROUNDER_MODEL": AUTHORIZED_PROVIDER_MODEL,
            "MOBIAGENT_PLANNER_MODEL": AUTHORIZED_PROVIDER_MODEL,
            "MOBIAGENT_LLM_TRANSPORT": AUTHORIZED_TRANSPORT,
        }
    )
    try:
        result = subprocess.run(command, cwd=runner_root, env=env, check=False)
        status = "RUN_COMPLETE" if result.returncode == 0 else "RUN_FAILED"
        write_new_json(
            output_dir / RUN_MANIFEST,
            _record(**common, status=status, runner_exit_code=result.returncode),
        )
        print(
            json.dumps(
                {**summary, "status": status, "runner_exit_code": result.returncode},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return result.returncode
    except KeyboardInterrupt:
        write_new_json(
            output_dir / RUN_MANIFEST,
            _record(**common, status="RUN_ABORTED", runner_exit_code=130),
        )
        raise
    except Exception:
        write_new_json(
            output_dir / RUN_MANIFEST,
            _record(**common, status="RUN_FAILED", runner_exit_code=1),
        )
        raise


if __name__ == "__main__":
    raise SystemExit(main())
