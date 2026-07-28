"""Typed, non-shell orchestration for Runner -> intake -> verifier batches."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence, Tuple

from .contract_freeze import write_contract_freeze
from .contract_router import route_contract
from .jit_contract_compiler import JitAppMetadata, JitCompileRequest
from .jit_model_proposer import OpenAICompatibleJitProposer
from .phase5_intake import Phase5IntakeError, strict_json_bytes
from .phase5_trace_case import CasePaths
from .task_family_catalog import route_candidate
from .task_spec import SUPPORTED_TASK_FAMILIES, TaskSpec, infer_task_family


AUTOMATED_EVALUATION_PLAN_VERSION = "mobiagent-automated-evaluation-plan-v1"
EXISTING_TRACE = "existing_trace"
PHASE5_REALISM_PILOT = "phase5_cross_app_realism"
PHASE5_REALISM_COHORT = "phase5_cross_app_realism_cohort"
GENERIC_RUNNER_TRACE = "generic_runner_trace"
RUNNER_KINDS = (PHASE5_REALISM_PILOT, PHASE5_REALISM_COHORT, GENERIC_RUNNER_TRACE)


@dataclass(frozen=True)
class AutomatedEvaluationPlan:
    source_path: Path
    source_sha256: str
    raw_trace_root: Path | None
    intake_root: Path | None
    tasks: Tuple[Mapping[str, Any], ...]
    metadata: Mapping[str, Any]


@dataclass(frozen=True)
class PreparedEvaluationCases:
    cases: Tuple[CasePaths, ...]
    runner_preflight_only: bool
    task_records: Tuple[Mapping[str, Any], ...]


CommandRunner = Callable[[Sequence[str], Path], int]
ProgressCallback = Callable[[Mapping[str, Any]], None]


def _progress_event(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        **dict(record),
    }


def _append_jsonl(path: Path | None, event: Mapping[str, Any]) -> None:
    if path is None:
        return
    target = path.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")


def _record_progress(
    records: list[Mapping[str, Any]],
    record: Mapping[str, Any],
    *,
    progress_log: Path | None,
    progress_callback: ProgressCallback | None,
) -> None:
    frozen = dict(record)
    records.append(frozen)
    event = _progress_event(frozen)
    _append_jsonl(progress_log, event)
    if progress_callback is not None:
        progress_callback(event)


def _emit_progress(
    record: Mapping[str, Any],
    *,
    progress_log: Path | None,
    progress_callback: ProgressCallback | None,
) -> None:
    event = _progress_event(record)
    _append_jsonl(progress_log, event)
    if progress_callback is not None:
        progress_callback(event)


def _task_record_base(index: int, task: Mapping[str, Any]) -> dict[str, Any]:
    record: dict[str, Any] = {
        "index": index,
        "kind": task["kind"],
        "status": "TASK_STARTED",
    }
    for key in ("run_id", "task_id"):
        if key in task:
            record[key] = task[key]
    if task["kind"] == GENERIC_RUNNER_TRACE:
        record["task_family"] = infer_task_family(
            str(task["task_text"]), str(task.get("task_family") or "")
        )
    return record


def _error_text(exc: BaseException) -> dict[str, str]:
    record = {"error": f"{type(exc).__name__}: {exc}"}
    cause = exc.__cause__
    if cause is not None:
        record["cause"] = f"{type(cause).__name__}: {cause}"
    return record


def _exact_keys(
    value: Mapping[str, Any], required: set[str], optional: set[str], context: str
) -> None:
    keys = set(value)
    missing = required - keys
    extra = keys - required - optional
    if missing or extra:
        raise Phase5IntakeError(
            f"{context} keys invalid; missing={sorted(missing)}, extra={sorted(extra)}"
        )


def _resolve(base: Path, value: Any, context: str, *, must_exist: bool) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise Phase5IntakeError(f"{context} must be a non-empty path string")
    path = Path(value)
    if not path.is_absolute():
        path = base / path
    return path.resolve(strict=must_exist)


def _validate_task(task: Mapping[str, Any], index: int) -> None:
    context = f"tasks[{index}]"
    kind = task.get("kind")
    if kind == EXISTING_TRACE:
        _exact_keys(
            task,
            {"kind", "run_dir", "intake_receipt"},
            {"task_contract", "contract_freeze"},
            context,
        )
        return
    if kind == PHASE5_REALISM_PILOT:
        _exact_keys(
            task,
            {
                "kind",
                "run_id",
                "task_id",
                "manifest",
                "package_probe_report",
                "predecessor_disposition",
                "os_version",
            },
            {"attempt_ordinal"},
            context,
        )
    elif kind == PHASE5_REALISM_COHORT:
        _exact_keys(
            task,
            {
                "kind",
                "run_id",
                "task_id",
                "manifest",
                "package_probe_report",
                "pilot_ground_truth",
                "os_version",
            },
            {"attempt_ordinal"},
            context,
        )
    elif kind == GENERIC_RUNNER_TRACE:
        _exact_keys(
            task,
            {
                "kind",
                "run_id",
                "task_id",
                "task_text",
                "app",
                "runner_task_type",
                "os_version",
                "device_serial",
            },
            {
                "attempt_ordinal",
                "task_family",
                "target_apps",
                "provider_base_url",
                "model",
                "runner_model",
                "contract_model",
                "transport",
            },
            context,
        )
        target_apps = task.get("target_apps", [task.get("app")])
        if not isinstance(target_apps, list) or any(
            not isinstance(item, str) or not item.strip() for item in target_apps
        ):
            raise Phase5IntakeError(f"{context}.target_apps must be a string list")
        TaskSpec(
            task_id=str(task.get("task_id") or ""),
            task_text=str(task.get("task_text") or ""),
            task_family=infer_task_family(
                str(task.get("task_text") or ""),
                str(task.get("task_family") or ""),
            ),
            initial_app=str(task.get("app") or ""),
            target_apps=tuple(dict.fromkeys(str(item) for item in target_apps)),
        ).validate()
    else:
        raise Phase5IntakeError(f"{context}.kind is unsupported: {kind!r}")
    for key in ("run_id", "task_id", "os_version"):
        if not isinstance(task.get(key), str) or not str(task[key]).strip():
            raise Phase5IntakeError(f"{context}.{key} must be a non-empty string")
    ordinal = task.get("attempt_ordinal", 1)
    if not isinstance(ordinal, int) or isinstance(ordinal, bool) or ordinal <= 0:
        raise Phase5IntakeError(f"{context}.attempt_ordinal must be positive")


def load_automated_evaluation_plan(path: Path) -> AutomatedEvaluationPlan:
    source = path.resolve(strict=True)
    raw = source.read_bytes()
    value = strict_json_bytes(raw, context="automated evaluation plan")
    _exact_keys(
        value,
        {"schema_version", "tasks"},
        {"raw_trace_root", "intake_root", "metadata"},
        "automated evaluation plan",
    )
    if value["schema_version"] != AUTOMATED_EVALUATION_PLAN_VERSION:
        raise Phase5IntakeError("unsupported automated evaluation plan schema_version")
    tasks = value["tasks"]
    if not isinstance(tasks, list) or not tasks:
        raise Phase5IntakeError("automated evaluation plan tasks must be non-empty")
    for index, task in enumerate(tasks):
        if not isinstance(task, Mapping):
            raise Phase5IntakeError(f"tasks[{index}] must be an object")
        _validate_task(task, index)
    needs_runner = any(task["kind"] in RUNNER_KINDS for task in tasks)
    base = source.parent
    raw_root = (
        _resolve(base, value.get("raw_trace_root"), "raw_trace_root", must_exist=False)
        if value.get("raw_trace_root") is not None
        else None
    )
    intake_root = (
        _resolve(base, value.get("intake_root"), "intake_root", must_exist=False)
        if value.get("intake_root") is not None
        else None
    )
    if needs_runner and (raw_root is None or intake_root is None):
        raise Phase5IntakeError(
            "raw_trace_root and intake_root are required for Runner tasks"
        )
    if raw_root is not None and intake_root is not None:
        if (
            raw_root == intake_root
            or raw_root.is_relative_to(intake_root)
            or intake_root.is_relative_to(raw_root)
        ):
            raise Phase5IntakeError("raw_trace_root and intake_root must not overlap")
    metadata = value.get("metadata", {})
    if not isinstance(metadata, Mapping):
        raise Phase5IntakeError("metadata must be an object")
    return AutomatedEvaluationPlan(
        source_path=source,
        source_sha256=hashlib.sha256(raw).hexdigest(),
        raw_trace_root=raw_root,
        intake_root=intake_root,
        tasks=tuple(dict(task) for task in tasks),
        metadata=dict(metadata),
    )


def select_plan_tasks(
    plan: AutomatedEvaluationPlan, task_ids: Sequence[str]
) -> AutomatedEvaluationPlan:
    """Select an elastic subset without changing the frozen source-plan hash."""

    requested = tuple(dict.fromkeys(str(item).strip() for item in task_ids))
    if not requested or any(not item for item in requested):
        raise Phase5IntakeError("task selection must contain non-empty task ids")
    by_id: dict[str, Mapping[str, Any]] = {}
    for task in plan.tasks:
        task_id = task.get("task_id")
        if isinstance(task_id, str) and task_id:
            if task_id in by_id:
                raise Phase5IntakeError(f"duplicate task_id in plan: {task_id}")
            by_id[task_id] = task
    missing = tuple(item for item in requested if item not in by_id)
    if missing:
        raise Phase5IntakeError(f"selected task ids are absent from plan: {missing}")
    metadata = dict(plan.metadata)
    metadata["selected_task_ids"] = list(requested)
    metadata["source_plan_task_count"] = len(plan.tasks)
    return replace(
        plan,
        tasks=tuple(by_id[item] for item in requested),
        metadata=metadata,
    )


def _default_command_runner(command: Sequence[str], cwd: Path) -> int:
    return subprocess.run(tuple(command), cwd=cwd, check=False).returncode


def _path_arg(base: Path, task: Mapping[str, Any], key: str) -> Path:
    return _resolve(base, task[key], key, must_exist=True)


def _pre_run_contract_freeze(
    plan: AutomatedEvaluationPlan,
    task: Mapping[str, Any],
    *,
    allow_model_calls: bool,
) -> tuple[Path, TaskSpec, str]:
    """Compile and persist a task-only Contract before invoking Runner."""

    from .upgraded_verifier import (
        PHASE5_CONTRACT_SELECTION_KEY,
        builtin_registry,
    )

    assert plan.intake_root is not None
    if task["kind"] == GENERIC_RUNNER_TRACE:
        target_apps = task.get("target_apps", [task["app"]])
        task_spec = TaskSpec.from_run_manifest(
            {
                "task_id": str(task["task_id"]),
                "task_text": str(task["task_text"]),
                "task_family": infer_task_family(
                    str(task["task_text"]), str(task.get("task_family") or "")
                ),
                "initial_app": str(task["app"]),
                "target_apps": list(dict.fromkeys(str(item) for item in target_apps)),
            }
        )
    else:
        manifest_path = _path_arg(plan.source_path.parent, task, "manifest")
        manifest = strict_json_bytes(
            manifest_path.read_bytes(), context="Runner experiment manifest"
        )
        rows = manifest.get("tasks")
        if not isinstance(rows, list):
            raise Phase5IntakeError("Runner experiment manifest tasks must be a list")
        matches = tuple(
            row
            for row in rows
            if isinstance(row, Mapping) and row.get("task_id") == task["task_id"]
        )
        if len(matches) != 1:
            raise Phase5IntakeError(
                "Runner task_id must resolve exactly once before execution"
            )
        row = dict(matches[0])
        row["experiment_id"] = manifest.get("experiment_id")
        task_spec = TaskSpec.from_run_manifest(row)
    phase5_sales = all(
        term in task_spec.task_text for term in ("淘宝", "销量", "小红书")
    )
    if phase5_sales:
        routed = route_contract(PHASE5_CONTRACT_SELECTION_KEY, builtin_registry())
    elif task_spec.task_family in SUPPORTED_TASK_FAMILIES:
        routed = route_contract(
            task_spec.selection_key,
            builtin_registry(),
            template_candidates=(route_candidate(task_spec),),
        )
    else:
        if not allow_model_calls:
            raise Phase5IntakeError(
                "unseen task requires a task-only Validated JIT model call; "
                "rerun with --execute-runner after setting MOBIAGENT_API_KEY"
            )
        api_key = os.getenv("MOBIAGENT_API_KEY", "").strip()
        if not api_key:
            raise Phase5IntakeError(
                "MOBIAGENT_API_KEY is required to compile an unseen task Contract"
            )
        request = JitCompileRequest(
            task_description=task_spec.task_text,
            app_metadata=JitAppMetadata(
                app_id=task_spec.initial_app or "unknown-app",
                app_name=task_spec.initial_app or "unknown-app",
                platform="HarmonyOS",
                task_family=(
                    None
                    if task_spec.task_family == "unseen"
                    else task_spec.task_family
                ),
                risk_tier={
                    "read_only": "LOW",
                    "low_risk_write": "MEDIUM",
                    "high_risk": "HIGH",
                }[task_spec.risk_level],
            ),
        )
        proposer = OpenAICompatibleJitProposer(
            base_url=str(
                task.get("provider_base_url", "https://api.horizon1123.top/v1")
            ),
            model=str(
                task.get("contract_model", task.get("model", "gpt-5.4-mini"))
            ),
            api_key=api_key,
        )
        routed = route_contract(
            request.selection_key,
            builtin_registry(),
            enable_validated_jit=True,
            jit_request=request,
            jit_proposer=proposer,
        )
    destination = (
        plan.intake_root / "_contract_freezes" / f"{task['run_id']}.json"
    )
    write_contract_freeze(destination, task_spec, routed)
    return destination, task_spec, routed.contract_sha256


def _collector_command(
    plan: AutomatedEvaluationPlan,
    task: Mapping[str, Any],
    runner_root: Path,
    *,
    execute_runner: bool,
) -> tuple[list[str], Path, Path, Path]:
    assert plan.raw_trace_root is not None and plan.intake_root is not None
    base = plan.source_path.parent
    run_dir = plan.raw_trace_root / str(task["run_id"])
    receipt_dir = plan.intake_root / str(task["run_id"])
    if task["kind"] == GENERIC_RUNNER_TRACE:
        task_family = infer_task_family(
            str(task["task_text"]), str(task.get("task_family") or "")
        )
        command = [
            sys.executable,
            "-m",
            "verification_benchmark.tools.prepare_generic_runner_collection",
            "--run-id", str(task["run_id"]),
            "--task-id", str(task["task_id"]),
            "--task-text", str(task["task_text"]),
            "--task-family", task_family,
            "--app", str(task["app"]),
            "--runner-task-type", str(task["runner_task_type"]),
            "--device-serial", str(task["device_serial"]),
            "--output-dir", str(run_dir),
            "--os-version", str(task["os_version"]),
            "--runner-root", str(runner_root),
            "--provider-base-url",
            str(task.get("provider_base_url", "https://api.horizon1123.top/v1")),
            "--model",
            # Generic Runner traces preserve the frozen-v2 agent baseline.
            # Verifier/JIT model selection is intentionally independent.
            str(task.get("runner_model", task.get("model", "gpt-5.4"))),
            "--transport",
            str(task.get("transport", "raw_http")),
        ]
        for target_app in task.get("target_apps", [task["app"]]):
            command.extend(("--target-app", str(target_app)))
        if execute_runner:
            command.append("--execute")
        return command, run_dir, receipt_dir, receipt_dir / "generic_intake_receipt.json"
    manifest = _path_arg(base, task, "manifest")
    common = [
        sys.executable,
        "-m",
        (
            "verification_benchmark.tools.prepare_phase5_cross_app_realism_collection"
            if task["kind"] == PHASE5_REALISM_PILOT
            else "verification_benchmark.tools.prepare_phase5_cross_app_realism_cohort_collection"
        ),
        "--manifest",
        str(manifest),
        "--package-probe-report",
        str(_path_arg(base, task, "package_probe_report")),
        "--task-id",
        str(task["task_id"]),
        "--run-id",
        str(task["run_id"]),
        "--output-dir",
        str(run_dir),
        "--os-version",
        str(task["os_version"]),
        "--attempt-ordinal",
        str(task.get("attempt_ordinal", 1)),
        "--runner-root",
        str(runner_root),
    ]
    if task["kind"] == PHASE5_REALISM_PILOT:
        common.extend(
            [
                "--predecessor-disposition",
                str(_path_arg(base, task, "predecessor_disposition")),
            ]
        )
        receipt_name = "phase5_cross_app_realism_intake_receipt.json"
    else:
        common.extend(
            [
                "--pilot-ground-truth",
                str(_path_arg(base, task, "pilot_ground_truth")),
            ]
        )
        receipt_name = "phase5_cross_app_realism_cohort_intake_receipt.json"
    if execute_runner:
        common.append("--execute")
    return common, run_dir, receipt_dir, receipt_dir / receipt_name


def _ingest_command(
    task: Mapping[str, Any], manifest: Path, run_dir: Path, receipt_dir: Path
) -> list[str]:
    if task["kind"] == GENERIC_RUNNER_TRACE:
        return [
            sys.executable,
            "-m",
            "verification_benchmark.tools.ingest_generic_runner_trace",
            "--run-dir",
            str(run_dir),
            "--receipt-dir",
            str(receipt_dir),
        ]
    module = (
        "verification_benchmark.tools.ingest_phase5_cross_app_realism_trace"
        if task["kind"] == PHASE5_REALISM_PILOT
        else "verification_benchmark.tools.ingest_phase5_cross_app_realism_cohort_trace"
    )
    return [
        sys.executable,
        "-m",
        module,
        "--manifest",
        str(manifest),
        "--run-dir",
        str(run_dir),
        "--receipt-dir",
        str(receipt_dir),
    ]


def prepare_evaluation_cases(
    plan: AutomatedEvaluationPlan,
    *,
    execute_runner: bool,
    runner_root: Path,
    command_runner: CommandRunner | None = None,
    continue_on_runner_error: bool = True,
    progress_log: Path | None = None,
    progress_callback: ProgressCallback | None = None,
) -> PreparedEvaluationCases:
    """Preflight or execute typed Runner tasks and return verifier cases."""

    root = runner_root.resolve(strict=True)
    orchestration_root = Path(__file__).resolve().parents[2]
    invoke = command_runner or _default_command_runner
    cases: list[CasePaths] = []
    records: list[Mapping[str, Any]] = []
    has_pending_runner = False
    for index, task in enumerate(plan.tasks):
        _emit_progress(
            _task_record_base(index, task),
            progress_log=progress_log,
            progress_callback=progress_callback,
        )
        if task["kind"] == EXISTING_TRACE:
            base = plan.source_path.parent
            contract = task.get("task_contract")
            freeze = task.get("contract_freeze")
            case = CasePaths(
                _resolve(base, task["run_dir"], "run_dir", must_exist=True),
                _resolve(
                    base,
                    task["intake_receipt"],
                    "intake_receipt",
                    must_exist=True,
                ),
                task_contract=(
                    _resolve(base, contract, "task_contract", must_exist=True)
                    if contract is not None
                    else None
                ),
                contract_freeze=(
                    _resolve(base, freeze, "contract_freeze", must_exist=True)
                    if freeze is not None
                    else None
                ),
            )
            cases.append(case)
            _record_progress(
                records,
                {"index": index, "kind": EXISTING_TRACE, "status": "READY"},
                progress_log=progress_log,
                progress_callback=progress_callback,
            )
            continue
        command, run_dir, receipt_dir, receipt = _collector_command(
            plan, task, root, execute_runner=execute_runner
        )
        _emit_progress(
            {
                "index": index,
                "kind": task["kind"],
                "run_id": task["run_id"],
                "task_id": task["task_id"],
                "status": "RUNNER_STARTING",
                "run_dir": str(run_dir),
                "execute_runner": execute_runner,
            },
            progress_log=progress_log,
            progress_callback=progress_callback,
        )
        try:
            exit_code = invoke(command, orchestration_root)
        except KeyboardInterrupt:
            _emit_progress(
                {
                    "index": index,
                    "kind": task["kind"],
                    "run_id": task["run_id"],
                    "task_id": task["task_id"],
                    "status": "INTERRUPTED",
                    "phase": "RUNNER",
                    "run_dir": str(run_dir),
                },
                progress_log=progress_log,
                progress_callback=progress_callback,
            )
            raise
        except Exception as exc:  # noqa: BLE001 - classify external Runner failure.
            if not execute_runner or not continue_on_runner_error:
                raise
            _record_progress(
                records,
                {
                    "index": index,
                    "kind": task["kind"],
                    "run_id": task["run_id"],
                    "task_id": task["task_id"],
                    "status": "RUNNER_ERROR",
                    "run_dir": str(run_dir),
                    **_error_text(exc),
                },
                progress_log=progress_log,
                progress_callback=progress_callback,
            )
            continue
        if exit_code != 0:
            if not execute_runner or not continue_on_runner_error:
                raise Phase5IntakeError(
                    f"Runner collector failed for {task['run_id']} with exit code {exit_code}"
                )
            _record_progress(
                records,
                {
                    "index": index,
                    "kind": task["kind"],
                    "run_id": task["run_id"],
                    "task_id": task["task_id"],
                    "status": "RUNNER_FAILED",
                    "run_dir": str(run_dir),
                    "runner_exit_code": exit_code,
                },
                progress_log=progress_log,
                progress_callback=progress_callback,
            )
            continue
        _emit_progress(
            {
                "index": index,
                "kind": task["kind"],
                "run_id": task["run_id"],
                "task_id": task["task_id"],
                "status": "RUNNER_COMPLETE",
                "run_dir": str(run_dir),
                "runner_exit_code": exit_code,
            },
            progress_log=progress_log,
            progress_callback=progress_callback,
        )
        if not execute_runner:
            has_pending_runner = True
            _record_progress(
                records,
                {
                    "index": index,
                    "kind": task["kind"],
                    "run_id": task["run_id"],
                    "task_id": task["task_id"],
                    "status": "PREFLIGHT_OK",
                    "run_dir": str(run_dir),
                },
                progress_log=progress_log,
                progress_callback=progress_callback,
            )
            continue
        manifest = (
            Path(".")
            if task["kind"] == GENERIC_RUNNER_TRACE
            else _path_arg(plan.source_path.parent, task, "manifest")
        )
        _emit_progress(
            {
                "index": index,
                "kind": task["kind"],
                "run_id": task["run_id"],
                "task_id": task["task_id"],
                "status": "INTAKE_STARTING",
                "run_dir": str(run_dir),
                "receipt_dir": str(receipt_dir),
            },
            progress_log=progress_log,
            progress_callback=progress_callback,
        )
        try:
            ingest_exit = invoke(
                _ingest_command(task, manifest, run_dir, receipt_dir),
                orchestration_root,
            )
        except KeyboardInterrupt:
            _emit_progress(
                {
                    "index": index,
                    "kind": task["kind"],
                    "run_id": task["run_id"],
                    "task_id": task["task_id"],
                    "status": "INTERRUPTED",
                    "phase": "INTAKE",
                    "run_dir": str(run_dir),
                    "receipt_dir": str(receipt_dir),
                },
                progress_log=progress_log,
                progress_callback=progress_callback,
            )
            raise
        except Exception as exc:  # noqa: BLE001 - classify strict-intake failure.
            if not continue_on_runner_error:
                raise
            _record_progress(
                records,
                {
                    "index": index,
                    "kind": task["kind"],
                    "run_id": task["run_id"],
                    "task_id": task["task_id"],
                    "status": "INTAKE_ERROR",
                    "run_dir": str(run_dir),
                    "receipt_dir": str(receipt_dir),
                    **_error_text(exc),
                },
                progress_log=progress_log,
                progress_callback=progress_callback,
            )
            continue
        if ingest_exit != 0:
            if not continue_on_runner_error:
                raise Phase5IntakeError(
                    f"strict intake failed for {task['run_id']} with exit code {ingest_exit}"
                )
            _record_progress(
                records,
                {
                    "index": index,
                    "kind": task["kind"],
                    "run_id": task["run_id"],
                    "task_id": task["task_id"],
                    "status": "INTAKE_FAILED",
                    "run_dir": str(run_dir),
                    "receipt_dir": str(receipt_dir),
                    "intake_exit_code": ingest_exit,
                },
                progress_log=progress_log,
                progress_callback=progress_callback,
            )
            continue
        if not receipt.is_file():
            if not continue_on_runner_error:
                raise Phase5IntakeError(
                    f"strict intake did not create receipt: {receipt}"
                )
            _record_progress(
                records,
                {
                    "index": index,
                    "kind": task["kind"],
                    "run_id": task["run_id"],
                    "task_id": task["task_id"],
                    "status": "INTAKE_RECEIPT_MISSING",
                    "run_dir": str(run_dir),
                    "receipt": str(receipt),
                },
                progress_log=progress_log,
                progress_callback=progress_callback,
            )
            continue
        _emit_progress(
            {
                "index": index,
                "kind": task["kind"],
                "run_id": task["run_id"],
                "task_id": task["task_id"],
                "status": "INTAKE_READY",
                "run_dir": str(run_dir),
                "intake_receipt": str(receipt),
            },
            progress_log=progress_log,
            progress_callback=progress_callback,
        )
        _emit_progress(
            {
                "index": index,
                "kind": task["kind"],
                "run_id": task["run_id"],
                "task_id": task["task_id"],
                "status": "CONTRACT_STARTING",
                "run_dir": str(run_dir),
                "intake_receipt": str(receipt),
            },
            progress_log=progress_log,
            progress_callback=progress_callback,
        )
        try:
            contract_freeze, frozen_task, frozen_contract_sha256 = (
                _pre_run_contract_freeze(
                    plan, task, allow_model_calls=execute_runner
                )
            )
        except KeyboardInterrupt:
            _emit_progress(
                {
                    "index": index,
                    "kind": task["kind"],
                    "run_id": task["run_id"],
                    "task_id": task["task_id"],
                    "status": "INTERRUPTED",
                    "phase": "CONTRACT",
                    "run_dir": str(run_dir),
                    "intake_receipt": str(receipt),
                },
                progress_log=progress_log,
                progress_callback=progress_callback,
            )
            raise
        except Exception as exc:  # noqa: BLE001 - classify task Contract failure.
            if not continue_on_runner_error:
                raise
            _record_progress(
                records,
                {
                    "index": index,
                    "kind": task["kind"],
                    "run_id": task["run_id"],
                    "task_id": task["task_id"],
                    "status": "CONTRACT_FAILED",
                    "run_dir": str(run_dir),
                    "intake_receipt": str(receipt),
                    **_error_text(exc),
                },
                progress_log=progress_log,
                progress_callback=progress_callback,
            )
            continue
        cases.append(CasePaths(run_dir, receipt, contract_freeze=contract_freeze))
        _record_progress(
            records,
            {
                "index": index,
                "kind": task["kind"],
                "run_id": task["run_id"],
                "task_id": task["task_id"],
                "status": "TRACE_INTAKE_AND_CONTRACT_READY",
                "run_dir": str(run_dir),
                "intake_receipt": str(receipt),
                "contract_freeze": str(contract_freeze),
                "contract_sha256": frozen_contract_sha256,
                "task_spec_sha256": frozen_task.sha256,
            },
            progress_log=progress_log,
            progress_callback=progress_callback,
        )
    return PreparedEvaluationCases(tuple(cases), has_pending_runner, tuple(records))


def plan_audit_payload(plan: AutomatedEvaluationPlan) -> Mapping[str, Any]:
    return {
        "schema_version": AUTOMATED_EVALUATION_PLAN_VERSION,
        "plan_path": str(plan.source_path),
        "plan_sha256": plan.source_sha256,
        "task_count": len(plan.tasks),
        "runner_task_count": sum(task["kind"] in RUNNER_KINDS for task in plan.tasks),
        "existing_trace_count": sum(
            task["kind"] == EXISTING_TRACE for task in plan.tasks
        ),
        "raw_trace_root": str(plan.raw_trace_root) if plan.raw_trace_root else None,
        "intake_root": str(plan.intake_root) if plan.intake_root else None,
        "metadata": dict(plan.metadata),
    }


__all__ = [
    "AUTOMATED_EVALUATION_PLAN_VERSION",
    "AutomatedEvaluationPlan",
    "EXISTING_TRACE",
    "PHASE5_REALISM_COHORT",
    "PHASE5_REALISM_PILOT",
    "GENERIC_RUNNER_TRACE",
    "PreparedEvaluationCases",
    "load_automated_evaluation_plan",
    "plan_audit_payload",
    "prepare_evaluation_cases",
    "select_plan_tasks",
]
