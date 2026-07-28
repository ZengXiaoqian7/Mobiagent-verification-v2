"""Prepare or execute one generic read-only MobiAgent Runner task."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

from verification_benchmark.evaluation_framework.harmony_device_readiness import (
    wait_for_hdc_shell,
)
from verification_benchmark.evaluation_framework.phase5_intake import (
    Phase5IntakeError,
    file_sha256,
    write_new_json,
)
from verification_benchmark.evaluation_framework.task_spec import TaskSpec


RUN_MANIFEST = "run_manifest.json"
TASK_FILE = "runner_task.json"
START_RECORD = "collection_start.json"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--task-text", required=True)
    parser.add_argument("--task-family", required=True)
    parser.add_argument("--app", required=True)
    parser.add_argument("--runner-task-type", required=True)
    parser.add_argument("--target-app", action="append", default=[])
    parser.add_argument("--device-serial", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--os-version", required=True)
    parser.add_argument("--provider-base-url", default="https://api.horizon1123.top/v1")
    # Match the frozen-v2 Runner collection baseline.  Mini produces materially
    # different visual bboxes and must be selected explicitly for a separate arm.
    parser.add_argument("--model", default="gpt-5.4")
    parser.add_argument("--transport", default="raw_http")
    parser.add_argument("--runner-root", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    return parser


def _command(
    *, runner_task: Path, output: Path, device_serial: str
) -> list[str]:
    return [
        sys.executable,
        "-m",
        "runner.mobiagent.mobiagent",
        "--device",
        "Harmony",
        "--device_serial",
        device_serial,
        "--task_file",
        str(runner_task),
        "--data_dir",
        str(output),
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


def main(argv: Optional[list[str]] = None) -> int:
    args = _parser().parse_args(argv)
    target_apps = tuple(dict.fromkeys(args.target_app or [args.app]))
    spec = TaskSpec.from_run_manifest(
        {
            "task_id": args.task_id,
            "task_text": args.task_text,
            "task_family": args.task_family,
            "initial_app": args.app,
            "target_apps": list(target_apps),
        }
    )
    spec.validate()
    output = args.output_dir.resolve()
    if output.name != args.run_id:
        raise Phase5IntakeError("output directory basename must equal run-id")
    if output.exists():
        raise Phase5IntakeError(f"refusing to reuse output directory: {output}")
    runner_root = args.runner_root.resolve(strict=True)
    runner_module = runner_root / "runner/mobiagent/mobiagent.py"
    if not runner_module.is_file():
        raise Phase5IntakeError("MobiAgent Runner module is unavailable")
    command = _command(
        runner_task=output / TASK_FILE,
        output=output,
        device_serial=args.device_serial,
    )
    summary: dict[str, Any] = {
        "status": "PREFLIGHT_OK" if not args.execute else "READY_TO_EXECUTE",
        "paid_provider_call": args.execute,
        "device_mutation": args.execute,
        "run_id": args.run_id,
        "task_id": args.task_id,
        "task_spec_sha256": spec.sha256,
        "task_family": args.task_family,
        "output_dir": str(output),
        "runner_module_sha256": file_sha256(runner_module),
        "provider_base_url": args.provider_base_url,
        "model": args.model,
        "transport": args.transport,
        "command": command,
    }
    if not args.execute:
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        return 0
    if not os.getenv("MOBIAGENT_API_KEY", "").strip():
        raise Phase5IntakeError("MOBIAGENT_API_KEY is required for Runner execution")
    readiness = wait_for_hdc_shell(args.device_serial)
    output.mkdir(parents=True, exist_ok=False)
    write_new_json(
        output / TASK_FILE,
        [{"app": args.app, "type": args.runner_task_type, "tasks": [args.task_text]}],
    )
    start = {
        **summary,
        "status": "RUN_IN_PROGRESS",
        "hdc_readiness": {
            "serial": readiness.serial,
            "attempts_used": readiness.attempts_used,
        },
    }
    write_new_json(output / START_RECORD, start)
    try:
        env = os.environ.copy()
        pythonpath_parts = [str(runner_root)]
        existing_pythonpath = env.get("PYTHONPATH", "").strip()
        if existing_pythonpath:
            pythonpath_parts.append(existing_pythonpath)
        env.update(
            {
                "PYTHONPATH": os.pathsep.join(pythonpath_parts),
                "MOBIAGENT_BASE_URL": args.provider_base_url,
                "MOBIAGENT_DECIDER_BASE_URL": args.provider_base_url,
                "MOBIAGENT_GROUNDER_BASE_URL": args.provider_base_url,
                "MOBIAGENT_PLANNER_BASE_URL": args.provider_base_url,
                "MOBIAGENT_MODEL": args.model,
                "MOBIAGENT_DECIDER_MODEL": args.model,
                "MOBIAGENT_GROUNDER_MODEL": args.model,
                "MOBIAGENT_PLANNER_MODEL": args.model,
                "MOBIAGENT_LLM_TRANSPORT": args.transport,
            }
        )
        result = subprocess.run(command, cwd=output, env=env, check=False)
        status = "RUN_COMPLETE" if result.returncode == 0 else "RUN_FAILED"
        manifest = {
            "schema_version": "mobiagent-generic-run-manifest-v1",
            "run_id": args.run_id,
            "task_id": args.task_id,
            "task_text": args.task_text,
            "task_spec_sha256": spec.sha256,
            "task_family": args.task_family,
            "initial_app": args.app,
            "target_app": args.app,
            "target_apps": list(target_apps),
            "trace_relpath": f"{args.app}/{args.runner_task_type}/1",
            "os_version": args.os_version,
            "device_serial": args.device_serial,
            "runner_module_sha256": file_sha256(runner_module),
            "provider_base_url": args.provider_base_url,
            "model": args.model,
            "transport": args.transport,
            "runner_exit_code": result.returncode,
            "status": status,
            "guardrail_callback_enabled": False,
        }
        write_new_json(output / RUN_MANIFEST, manifest)
        print(json.dumps({**summary, **manifest}, ensure_ascii=False, sort_keys=True))
        return result.returncode
    except BaseException:
        if not (output / RUN_MANIFEST).exists():
            write_new_json(
                output / RUN_MANIFEST,
                {
                    "schema_version": "mobiagent-generic-run-manifest-v1",
                    "run_id": args.run_id,
                    "task_id": args.task_id,
                    "task_text": args.task_text,
                    "task_spec_sha256": spec.sha256,
                    "task_family": args.task_family,
                    "initial_app": args.app,
                    "target_app": args.app,
                    "target_apps": list(target_apps),
                    "trace_relpath": f"{args.app}/{args.runner_task_type}/1",
                    "os_version": args.os_version,
                    "device_serial": args.device_serial,
                    "runner_module_sha256": file_sha256(runner_module),
                    "provider_base_url": args.provider_base_url,
                    "model": args.model,
                    "transport": args.transport,
                    "runner_exit_code": 1,
                    "status": "RUN_FAILED",
                    "guardrail_callback_enabled": False,
                },
            )
        raise


if __name__ == "__main__":
    raise SystemExit(main())
