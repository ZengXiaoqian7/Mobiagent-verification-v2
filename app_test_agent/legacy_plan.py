"""Bridge from app-test-case-v1 to the existing automated evaluation plan."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from .schema import TestCaseSpec, dump_json


LEGACY_PLAN_SCHEMA_VERSION = "mobiagent-automated-evaluation-plan-v1"


def build_legacy_generic_runner_plan(
    *,
    test_case: TestCaseSpec,
    run_id: str,
    raw_trace_root: Path,
    intake_root: Path,
    device_serial: str,
    os_version: str,
    runner_model: str = "gpt-5.4",
    contract_model: str = "gpt-5.4-mini",
    provider_base_url: str = "https://api.horizon1123.top/v1",
    transport: str = "raw_http",
) -> dict[str, Any]:
    return {
        "schema_version": LEGACY_PLAN_SCHEMA_VERSION,
        "raw_trace_root": str(raw_trace_root),
        "intake_root": str(intake_root),
        "metadata": {
            "purpose": "app-test-agent compatibility bridge",
            "app_test_case_id": test_case.test_case_id,
            "app_test_case_sha256": test_case.sha256,
            "semantic_warning": (
                "This runs the existing whole-task MobiAgent path. "
                "Use the App-test report for App functional attribution."
            ),
        },
        "tasks": [
            {
                "kind": "generic_runner_trace",
                "run_id": run_id,
                "task_id": test_case.test_case_id,
                "task_text": test_case.strict_runner_instruction(),
                "app": test_case.app_under_test.name,
                "target_apps": [test_case.app_under_test.name],
                "runner_task_type": test_case.feature,
                "task_family": "composite_workflow",
                "os_version": os_version,
                "device_serial": device_serial,
                "provider_base_url": provider_base_url,
                "runner_model": runner_model,
                "contract_model": contract_model,
                "transport": transport,
            }
        ],
    }


def write_legacy_generic_runner_plan(path: Path, plan: Mapping[str, Any]) -> None:
    dump_json(path, plan)
