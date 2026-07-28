"""Strict Phase 5 cross-App collection, package binding, and intake facts."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Mapping, Optional

from .phase5_intake import (
    AUTHORIZED_DEVICE_TYPE,
    AUTHORIZED_KEY_ENV,
    AUTHORIZED_MODEL,
    AUTHORIZED_PROVIDER_BASE_URL,
    AUTHORIZED_PROVIDER_MODEL,
    AUTHORIZED_SERIAL,
    AUTHORIZED_TRANSPORT,
    CLAIM_BOUNDARY,
    RUN_ID,
    SHA256,
    TASK_ID,
    Phase5IntakeError,
    _canonical_string,
    _exact_keys,
    _positive_int,
    _safe_relative_ref,
    audit_trace,
    file_sha256,
    resolve_contained,
    semantic_sha256,
    source_file_manifest,
    strict_json_bytes,
    validate_intake_receipt as _unused_v1_receipt_validator,
)


del _unused_v1_receipt_validator

EXPERIMENT_SCHEMA_VERSION = "harmony-eval-phase5-cross-app-manifest-v2"
EXPERIMENT_ID = "phase5-cross-app-challenge-smoke-v2"
COLLECTION_SCHEMA_VERSION = "harmony-eval-phase5-cross-app-collection-run-v2"
COLLECTOR_VERSION = "harmony-eval-phase5-cross-app-collector-v2"
INTAKE_RECEIPT_SCHEMA_VERSION = "harmony-eval-phase5-cross-app-intake-receipt-v2"
INTAKE_VERSION = "harmony-eval-phase5-cross-app-intake-v2"
PENDING_GROUND_TRUTH = "PENDING_SINGLE_OPERATOR_REVIEW"
SOURCE_APP = "小红书"
SOURCE_PACKAGE = "com.xingin.xhs_hos"
TARGET_APP = "淘宝"
TARGET_PACKAGE = "com.taobao.taobao4hmos"
OS_VERSION = "OpenHarmony-6.1.1.120"
APP_BINDINGS = (
    (SOURCE_APP, SOURCE_PACKAGE, "9.38.0", 9380801),
    (TARGET_APP, TARGET_PACKAGE, "10.64.0", 770),
)
PACKAGE_PROBE_REPORT_SHA256 = (
    "e72686f980b996ef3ca9a1ac7e1866284e7cec812d42181b4144be907db93317"
)
PACKAGE_PROBE_SEMANTIC_SHA256 = (
    "d2ee3bad5b62ed4fff854885771ea41df13a80d1c85a36d65f84a941e27a8915"
)


def _non_empty_strings(value: Any, context: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise Phase5IntakeError(f"{context} must be a non-empty array")
    for index, item in enumerate(value):
        _canonical_string(item, f"{context}[{index}]")
    return value


def _validate_installed_apps(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, list) or len(value) != len(APP_BINDINGS):
        raise Phase5IntakeError("installed Apps must contain the frozen pair")
    for row, expected in zip(value, APP_BINDINGS):
        if not isinstance(row, Mapping):
            raise Phase5IntakeError("installed App row must be an object")
        _exact_keys(
            row,
            {"app", "package", "version_name", "version_code", "raw_dump_sha256"},
            "installed App",
        )
        if (
            tuple(
                row[key] for key in ("app", "package", "version_name", "version_code")
            )
            != expected
        ):
            raise Phase5IntakeError("installed App/package/version drift")
        if not isinstance(row["raw_dump_sha256"], str) or not SHA256.fullmatch(
            row["raw_dump_sha256"]
        ):
            raise Phase5IntakeError("installed App raw dump SHA is invalid")
    return value


def validate_experiment_manifest(value: Mapping[str, Any]) -> Mapping[str, Any]:
    _exact_keys(
        value,
        {
            "schema_version",
            "experiment_id",
            "protocol_status",
            "publication_eligible",
            "claim_boundary",
            "oracle_database_dependency",
            "phase4_status",
            "cohort",
            "package_probe",
            "agent",
            "collection_policy",
            "tasks",
        },
        "Phase 5 cross-App manifest",
    )
    expected_top = {
        "schema_version": EXPERIMENT_SCHEMA_VERSION,
        "experiment_id": EXPERIMENT_ID,
        "protocol_status": "DEVELOPMENT_CROSS_APP_SMOKE_FROZEN_BEFORE_COLLECTION",
        "publication_eligible": False,
        "claim_boundary": CLAIM_BOUNDARY,
        "oracle_database_dependency": False,
        "phase4_status": "FROZEN_MECHANISM_VALIDATION_ONLY",
    }
    for key, expected in expected_top.items():
        if value[key] != expected:
            raise Phase5IntakeError(f"cross-App manifest drift at {key}")

    cohort = value["cohort"]
    if not isinstance(cohort, Mapping):
        raise Phase5IntakeError("cross-App cohort must be an object")
    _exact_keys(
        cohort,
        {
            "cohort_id",
            "device_serial",
            "device_model",
            "device_type",
            "posture",
            "resolution",
            "os_version",
        },
        "cross-App cohort",
    )
    frozen_cohort = {
        "device_serial": AUTHORIZED_SERIAL,
        "device_model": AUTHORIZED_MODEL,
        "device_type": AUTHORIZED_DEVICE_TYPE,
        "posture": "FOLDED_OUTER_DISPLAY",
        "resolution": [1080, 2444],
        "os_version": OS_VERSION,
    }
    _canonical_string(cohort["cohort_id"], "cohort_id")
    for key, expected in frozen_cohort.items():
        if cohort[key] != expected:
            raise Phase5IntakeError(f"cross-App cohort drift at {key}")

    probe = value["package_probe"]
    if not isinstance(probe, Mapping):
        raise Phase5IntakeError("package_probe must be an object")
    _exact_keys(
        probe,
        {
            "schema_version",
            "status",
            "mode",
            "report_file_sha256",
            "report_semantic_sha256",
            "prior_same_path_attempt_preserved",
            "provenance_caveat",
            "installed_apps",
        },
        "package_probe",
    )
    if (
        probe["schema_version"] != "harmony-package-probe-v1"
        or probe["status"] != "PASS"
        or probe["mode"] != "READ_ONLY_PACKAGE_VERSION_PROBE"
        or probe["report_file_sha256"] != PACKAGE_PROBE_REPORT_SHA256
        or probe["report_semantic_sha256"] != PACKAGE_PROBE_SEMANTIC_SHA256
        or probe["prior_same_path_attempt_preserved"] is not False
    ):
        raise Phase5IntakeError("package-probe binding drift")
    _canonical_string(probe["provenance_caveat"], "package probe caveat")
    _validate_installed_apps(probe["installed_apps"])

    agent = value["agent"]
    if not isinstance(agent, Mapping):
        raise Phase5IntakeError("cross-App agent must be an object")
    _exact_keys(
        agent,
        {
            "provider_base_url",
            "model",
            "transport",
            "key_env",
            "device",
            "use_qwen3",
            "use_experience",
            "user_profile",
            "use_graphrag",
            "accept_planner_changes",
            "decider_protocol",
            "coord_mode",
            "e2e",
            "max_steps",
            "runner_module_sha256",
        },
        "cross-App agent",
    )
    frozen_agent = {
        "provider_base_url": AUTHORIZED_PROVIDER_BASE_URL,
        "model": AUTHORIZED_PROVIDER_MODEL,
        "transport": AUTHORIZED_TRANSPORT,
        "key_env": AUTHORIZED_KEY_ENV,
        "device": AUTHORIZED_DEVICE_TYPE,
        "use_qwen3": "on",
        "use_experience": "off",
        "user_profile": "off",
        "use_graphrag": "off",
        "accept_planner_changes": "off",
        "decider_protocol": "qwen_json",
        "coord_mode": "resized_pixel",
        "e2e": True,
        "max_steps": 15,
    }
    for key, expected in frozen_agent.items():
        if agent[key] != expected:
            raise Phase5IntakeError(f"cross-App agent drift at {key}")
    if not isinstance(agent["runner_module_sha256"], str) or not SHA256.fullmatch(
        agent["runner_module_sha256"]
    ):
        raise Phase5IntakeError("cross-App Runner SHA is invalid")

    policy = value["collection_policy"]
    if not isinstance(policy, Mapping):
        raise Phase5IntakeError("cross-App collection policy must be an object")
    _exact_keys(
        policy,
        {
            "risk_tier",
            "allowed_apps",
            "allowed_actions",
            "forbidden_actions",
            "abort_conditions",
            "collection_order",
            "ground_truth_status",
            "guardrail_callbacks_allowed",
            "overwrite_allowed",
            "final_performance_cohort_status",
        },
        "cross-App collection policy",
    )
    if (
        policy["risk_tier"] != "LOW_RISK_CROSS_APP_READ_ONLY_RESEARCH_AND_SEARCH"
        or policy["allowed_apps"] != [SOURCE_APP, TARGET_APP]
        or policy["ground_truth_status"] != PENDING_GROUND_TRUTH
        or policy["guardrail_callbacks_allowed"] is not False
        or policy["overwrite_allowed"] is not False
        or policy["final_performance_cohort_status"]
        != "NOT_FROZEN_UNTIL_ALL_THREE_SMOKES_ARE_REVIEWED"
    ):
        raise Phase5IntakeError("cross-App collection policy drift")
    for key in (
        "allowed_actions",
        "forbidden_actions",
        "abort_conditions",
        "collection_order",
    ):
        _non_empty_strings(policy[key], f"collection_policy.{key}")

    tasks = value["tasks"]
    if not isinstance(tasks, list) or len(tasks) != 3:
        raise Phase5IntakeError("cross-App smoke must freeze exactly three tasks")
    seen: set[str] = set()
    expected_order: list[str] = []
    for ordinal, task in enumerate(tasks, 1):
        if not isinstance(task, Mapping):
            raise Phase5IntakeError("cross-App task must be an object")
        _exact_keys(
            task,
            {
                "task_id",
                "collection_ordinal",
                "task_family",
                "initial_app",
                "initial_package",
                "source_query",
                "target_app",
                "target_package",
                "dynamic_slot_policy",
                "task_text",
                "expected_observable_criteria",
                "allowed_actions",
                "forbidden_actions",
                "contract_source_route",
                "smoke_priority",
            },
            "cross-App task",
        )
        task_id = task["task_id"]
        if (
            not isinstance(task_id, str)
            or not TASK_ID.fullmatch(task_id)
            or task_id in seen
        ):
            raise Phase5IntakeError("cross-App task_id is invalid or duplicated")
        seen.add(task_id)
        expected_order.append(task_id)
        if task["collection_ordinal"] != ordinal:
            raise Phase5IntakeError("cross-App task ordinals must be contiguous")
        expected_task = {
            "task_family": "cross_app_dynamic_transfer_read_only",
            "initial_app": SOURCE_APP,
            "initial_package": SOURCE_PACKAGE,
            "target_app": TARGET_APP,
            "target_package": TARGET_PACKAGE,
            "contract_source_route": "template",
            "smoke_priority": ("FIRST", "SECOND", "THIRD")[ordinal - 1],
        }
        for key, expected in expected_task.items():
            if task[key] != expected:
                raise Phase5IntakeError(f"cross-App task drift at {key}")
        text = _canonical_string(task["task_text"], "task_text")
        query = _canonical_string(task["source_query"], "source_query")
        if query not in text or "open_app" not in text:
            raise Phase5IntakeError("source query/open_app is not bound into task text")
        for key in (
            "expected_observable_criteria",
            "allowed_actions",
            "forbidden_actions",
        ):
            _non_empty_strings(task[key], f"task.{key}")
        slots = task["dynamic_slot_policy"]
        if not isinstance(slots, Mapping):
            raise Phase5IntakeError("dynamic_slot_policy must be an object")
        _exact_keys(
            slots,
            {
                "minimum_slots",
                "maximum_slots",
                "slot_description",
                "same_frame_required",
                "safety_constraint",
            },
            "dynamic_slot_policy",
        )
        minimum = _positive_int(slots["minimum_slots"], "minimum_slots")
        maximum = _positive_int(slots["maximum_slots"], "maximum_slots")
        if (
            minimum != maximum
            or maximum not in {1, 2}
            or slots["same_frame_required"] is not True
        ):
            raise Phase5IntakeError("dynamic slot count/frame policy drift")
        _canonical_string(slots["slot_description"], "slot_description")
        _canonical_string(slots["safety_constraint"], "safety_constraint")
    if policy["collection_order"] != expected_order:
        raise Phase5IntakeError("cross-App collection order drift")
    return value


def load_experiment_manifest(path: Path) -> Mapping[str, Any]:
    return validate_experiment_manifest(
        strict_json_bytes(path.read_bytes(), context="Phase 5 cross-App manifest")
    )


def find_task(manifest: Mapping[str, Any], task_id: str) -> Mapping[str, Any]:
    matches = [task for task in manifest["tasks"] if task["task_id"] == task_id]
    if len(matches) != 1:
        raise Phase5IntakeError(
            f"cross-App task_id must resolve exactly once: {task_id}"
        )
    return matches[0]


def validate_package_probe_report(
    *, manifest: Mapping[str, Any], report_path: Path
) -> Mapping[str, Any]:
    validate_experiment_manifest(manifest)
    report_path = report_path.resolve(strict=True)
    report = strict_json_bytes(report_path.read_bytes(), context="package probe report")
    probe = manifest["package_probe"]
    if file_sha256(report_path) != probe["report_file_sha256"]:
        raise Phase5IntakeError("package probe report file SHA drift")
    if semantic_sha256(report) != probe["report_semantic_sha256"]:
        raise Phase5IntakeError("package probe report semantic SHA drift")
    if (
        report.get("schema_version") != probe["schema_version"]
        or report.get("status") != "PASS"
        or report.get("mode") != "READ_ONLY_PACKAGE_VERSION_PROBE"
        or report.get("authorized_serial") != AUTHORIZED_SERIAL
        or report.get("observed_targets") != [AUTHORIZED_SERIAL]
        or report.get("device")
        != {"model": AUTHORIZED_MODEL, "openharmony_fullname": OS_VERSION}
    ):
        raise Phase5IntakeError("package probe report identity/status drift")
    packages = report.get("packages")
    if not isinstance(packages, list) or len(packages) != 2:
        raise Phase5IntakeError("package probe report must contain two packages")
    for actual, frozen in zip(packages, probe["installed_apps"]):
        if not isinstance(actual, Mapping):
            raise Phase5IntakeError("package probe package row must be an object")
        for key in ("package", "version_name", "version_code"):
            if actual.get(key) != frozen[key]:
                raise Phase5IntakeError(f"package probe field drift: {key}")
        if actual.get("raw_dump_sha256") != frozen["raw_dump_sha256"]:
            raise Phase5IntakeError("package probe raw dump SHA binding drift")
        raw_ref = _safe_relative_ref(actual.get("raw_dump_path"), "raw_dump_path")
        raw_path = resolve_contained(report_path.parent, raw_ref)
        if not raw_path.is_file() or file_sha256(raw_path) != frozen["raw_dump_sha256"]:
            raise Phase5IntakeError("package probe raw dump file hash drift")
    return report


def validate_collection_run_manifest(value: Mapping[str, Any]) -> Mapping[str, Any]:
    _exact_keys(
        value,
        {
            "schema_version",
            "collector_version",
            "experiment_id",
            "experiment_manifest_sha256",
            "package_probe_report_sha256",
            "run_id",
            "task_id",
            "task_text_sha256",
            "initial_app",
            "initial_package",
            "target_app",
            "target_package",
            "trace_relpath",
            "device",
            "installed_apps",
            "agent",
            "collection_status",
            "attempt_ordinal",
            "oracle_database_dependency",
            "ground_truth_status",
            "guardrail_callback_enabled",
            "start_state_guard_enabled",
            "runner_exit_code",
        },
        "Phase 5 cross-App collection run",
    )
    fixed = {
        "schema_version": COLLECTION_SCHEMA_VERSION,
        "collector_version": COLLECTOR_VERSION,
        "experiment_id": EXPERIMENT_ID,
        "package_probe_report_sha256": PACKAGE_PROBE_REPORT_SHA256,
        "initial_app": SOURCE_APP,
        "initial_package": SOURCE_PACKAGE,
        "target_app": TARGET_APP,
        "target_package": TARGET_PACKAGE,
        "oracle_database_dependency": False,
        "ground_truth_status": PENDING_GROUND_TRUTH,
        "guardrail_callback_enabled": False,
        "start_state_guard_enabled": False,
    }
    for key, expected in fixed.items():
        if value[key] != expected:
            raise Phase5IntakeError(f"cross-App collection run drift at {key}")
    if not isinstance(value["run_id"], str) or not RUN_ID.fullmatch(value["run_id"]):
        raise Phase5IntakeError("cross-App run_id is invalid")
    for key in ("experiment_manifest_sha256", "task_text_sha256"):
        if not isinstance(value[key], str) or not SHA256.fullmatch(value[key]):
            raise Phase5IntakeError(f"cross-App run SHA is invalid: {key}")
    _canonical_string(value["task_id"], "task_id")
    _safe_relative_ref(value["trace_relpath"], "trace_relpath")
    if value["collection_status"] not in {
        "RUN_COMPLETE",
        "RUN_ABORTED",
        "RUN_FAILED",
        "RUN_IN_PROGRESS",
    }:
        raise Phase5IntakeError("cross-App collection status is invalid")
    _positive_int(value["attempt_ordinal"], "attempt_ordinal")
    if value["runner_exit_code"] is not None and (
        not isinstance(value["runner_exit_code"], int)
        or isinstance(value["runner_exit_code"], bool)
    ):
        raise Phase5IntakeError("cross-App runner_exit_code is invalid")
    device = value["device"]
    if not isinstance(device, Mapping):
        raise Phase5IntakeError("cross-App device must be an object")
    _exact_keys(
        device,
        {"serial", "model", "device_type", "posture", "resolution", "os_version"},
        "cross-App device",
    )
    expected_device = {
        "serial": AUTHORIZED_SERIAL,
        "model": AUTHORIZED_MODEL,
        "device_type": AUTHORIZED_DEVICE_TYPE,
        "posture": "FOLDED_OUTER_DISPLAY",
        "resolution": [1080, 2444],
        "os_version": OS_VERSION,
    }
    if device != expected_device:
        raise Phase5IntakeError("cross-App device identity/version drift")
    _validate_installed_apps(value["installed_apps"])
    agent = value["agent"]
    if not isinstance(agent, Mapping):
        raise Phase5IntakeError("cross-App run agent must be an object")
    _exact_keys(
        agent,
        {
            "provider_base_url",
            "model",
            "transport",
            "runner_module_sha256",
            "collector_source_sha256",
            "evaluation_git_head",
            "runner_repository_git_head",
        },
        "cross-App run agent",
    )
    if (
        agent["provider_base_url"] != AUTHORIZED_PROVIDER_BASE_URL
        or agent["model"] != AUTHORIZED_PROVIDER_MODEL
        or agent["transport"] != AUTHORIZED_TRANSPORT
    ):
        raise Phase5IntakeError("cross-App provider/model/transport drift")
    for key in ("runner_module_sha256", "collector_source_sha256"):
        if not isinstance(agent[key], str) or not SHA256.fullmatch(agent[key]):
            raise Phase5IntakeError(f"cross-App agent SHA is invalid: {key}")
    _canonical_string(agent["evaluation_git_head"], "evaluation_git_head")
    _canonical_string(agent["runner_repository_git_head"], "runner_repository_git_head")
    return value


def load_collection_run_manifest(run_dir: Path) -> Mapping[str, Any]:
    path = resolve_contained(run_dir, "phase5_cross_app_collection_run_manifest.json")
    return validate_collection_run_manifest(
        strict_json_bytes(path.read_bytes(), context="Phase 5 cross-App collection run")
    )


def _cross_app_action_facts(trace_dir: Path) -> Mapping[str, Any]:
    actions = strict_json_bytes(
        (trace_dir / "actions.json").read_bytes(), context="cross-App actions.json"
    )
    rows = actions["actions"]
    open_targets = [
        row.get("app_name") for row in rows if row.get("type") == "open_app"
    ]
    input_hashes = [
        hashlib.sha256(row["text"].encode("utf-8")).hexdigest()
        for row in rows
        if row.get("type") in {"input", "click_input"}
        and isinstance(row.get("text"), str)
    ]
    return {
        "initial_app_declared_by_runner": actions["app_name"],
        "open_app_targets": open_targets,
        "target_app_open_requested": TARGET_APP in open_targets,
        "input_text_sha256_sequence": input_hashes,
        "foreground_package_per_frame_available": False,
        "app_switch_evidence_mode": "ACTION_LOG_PLUS_FRAME_EVIDENCE_NO_FRAME_FOREGROUND_PACKAGE_TELEMETRY",
        "success_inferred_by_intake": False,
    }


def build_intake_receipt(
    *, experiment_manifest: Mapping[str, Any], run_dir: Path
) -> Mapping[str, Any]:
    validate_experiment_manifest(experiment_manifest)
    run = load_collection_run_manifest(run_dir)
    manifest_sha = semantic_sha256(experiment_manifest)
    if (
        run["experiment_manifest_sha256"] != manifest_sha
        or run["experiment_id"] != experiment_manifest["experiment_id"]
    ):
        raise Phase5IntakeError("cross-App run/manifest identity drift")
    task = find_task(experiment_manifest, run["task_id"])
    if (
        run["task_text_sha256"]
        != hashlib.sha256(task["task_text"].encode("utf-8")).hexdigest()
    ):
        raise Phase5IntakeError("cross-App run/task text hash drift")
    for run_key, task_key in (
        ("initial_app", "initial_app"),
        ("initial_package", "initial_package"),
        ("target_app", "target_app"),
        ("target_package", "target_package"),
    ):
        if run[run_key] != task[task_key]:
            raise Phase5IntakeError("cross-App run/task App binding drift")
    if run["installed_apps"] != experiment_manifest["package_probe"]["installed_apps"]:
        raise Phase5IntakeError("cross-App run installed-App binding drift")
    agent = experiment_manifest["agent"]
    if (
        run["agent"]["runner_module_sha256"] != agent["runner_module_sha256"]
        or run["agent"]["provider_base_url"] != agent["provider_base_url"]
        or run["agent"]["model"] != agent["model"]
        or run["agent"]["transport"] != agent["transport"]
    ):
        raise Phase5IntakeError("cross-App run/agent provenance drift")
    trace_dir = resolve_contained(run_dir, run["trace_relpath"])
    if not trace_dir.is_dir():
        raise Phase5IntakeError("cross-App trace_relpath is not a directory")
    files = source_file_manifest(run_dir)
    eligible = run["collection_status"] == "RUN_COMPLETE"
    trace_audit = None
    action_facts = None
    errors: list[str] = []
    if eligible:
        trace_task = {**task, "app": task["initial_app"]}
        trace_audit = audit_trace(trace_dir, trace_task)
        action_facts = _cross_app_action_facts(trace_dir)
    else:
        errors.append("COLLECTION_NOT_COMPLETE")
    receipt = {
        "schema_version": INTAKE_RECEIPT_SCHEMA_VERSION,
        "intake_version": INTAKE_VERSION,
        "intake_source_sha256": file_sha256(Path(__file__)),
        "status": (
            "ACCEPTED_PENDING_SINGLE_OPERATOR_REVIEW"
            if eligible
            else "REJECTED_COLLECTION_INCOMPLETE"
        ),
        "publication_eligible": False,
        "experiment_id": experiment_manifest["experiment_id"],
        "experiment_manifest_sha256": manifest_sha,
        "package_probe_report_sha256": run["package_probe_report_sha256"],
        "run_id": run["run_id"],
        "task_id": task["task_id"],
        "collection_run_manifest_sha256": semantic_sha256(run),
        "collection_status": run["collection_status"],
        "ground_truth_status": PENDING_GROUND_TRUTH,
        "oracle_database_dependency": False,
        "claim_boundary": CLAIM_BOUNDARY,
        "source_files": list(files),
        "source_tree_sha256": semantic_sha256(list(files)),
        "evidence_capability_profile": (
            {
                "screenshot_frames": list(trace_audit.screenshot_frames),
                "hierarchy_xml_frames": list(trace_audit.hierarchy_xml_frames),
                "hierarchy_raw_json_frames": list(
                    trace_audit.hierarchy_raw_json_frames
                ),
                "action_count": trace_audit.action_count,
                "action_types": list(trace_audit.action_types),
                "timestamps": trace_audit.timestamp_capability,
                "integrity": "VALID_WITH_TIMESTAMP_DEGRADATION",
            }
            if trace_audit is not None
            else None
        ),
        "cross_app_observability": action_facts,
        "diagnostic_evidence": {
            "react_present_and_hashed": any(
                row["relative_ref"].endswith("/react.json") for row in files
            ),
            "reasoning_copied_to_receipt": False,
            "runner_self_report_copied_to_receipt": False,
            "old_verifier_verdict_copied_to_receipt": False,
        },
        "errors": errors,
    }
    return validate_intake_receipt(receipt)


def validate_intake_receipt(value: Mapping[str, Any]) -> Mapping[str, Any]:
    _exact_keys(
        value,
        {
            "schema_version",
            "intake_version",
            "intake_source_sha256",
            "status",
            "publication_eligible",
            "experiment_id",
            "experiment_manifest_sha256",
            "package_probe_report_sha256",
            "run_id",
            "task_id",
            "collection_run_manifest_sha256",
            "collection_status",
            "ground_truth_status",
            "oracle_database_dependency",
            "claim_boundary",
            "source_files",
            "source_tree_sha256",
            "evidence_capability_profile",
            "cross_app_observability",
            "diagnostic_evidence",
            "errors",
        },
        "Phase 5 cross-App intake receipt",
    )
    fixed = {
        "schema_version": INTAKE_RECEIPT_SCHEMA_VERSION,
        "intake_version": INTAKE_VERSION,
        "publication_eligible": False,
        "experiment_id": EXPERIMENT_ID,
        "package_probe_report_sha256": PACKAGE_PROBE_REPORT_SHA256,
        "ground_truth_status": PENDING_GROUND_TRUTH,
        "oracle_database_dependency": False,
        "claim_boundary": CLAIM_BOUNDARY,
    }
    for key, expected in fixed.items():
        if value[key] != expected:
            raise Phase5IntakeError(f"cross-App intake receipt drift at {key}")
    for key in (
        "intake_source_sha256",
        "experiment_manifest_sha256",
        "collection_run_manifest_sha256",
        "source_tree_sha256",
    ):
        if not isinstance(value[key], str) or not SHA256.fullmatch(value[key]):
            raise Phase5IntakeError(f"cross-App receipt SHA is invalid: {key}")
    if value["status"] not in {
        "ACCEPTED_PENDING_SINGLE_OPERATOR_REVIEW",
        "REJECTED_COLLECTION_INCOMPLETE",
    }:
        raise Phase5IntakeError("cross-App intake status is invalid")
    if not isinstance(value["run_id"], str) or not RUN_ID.fullmatch(value["run_id"]):
        raise Phase5IntakeError("cross-App receipt run_id is invalid")
    _canonical_string(value["task_id"], "task_id")
    if value["collection_status"] not in {
        "RUN_COMPLETE",
        "RUN_ABORTED",
        "RUN_FAILED",
        "RUN_IN_PROGRESS",
    }:
        raise Phase5IntakeError("cross-App receipt collection status is invalid")
    if not isinstance(value["source_files"], list) or not value["source_files"]:
        raise Phase5IntakeError("cross-App source_files must be non-empty")
    for index, row in enumerate(value["source_files"]):
        if not isinstance(row, Mapping):
            raise Phase5IntakeError("cross-App source file row must be an object")
        _exact_keys(row, {"relative_ref", "byte_size", "sha256"}, "source file")
        _safe_relative_ref(row["relative_ref"], f"source_files[{index}].relative_ref")
        if (
            not isinstance(row["byte_size"], int)
            or isinstance(row["byte_size"], bool)
            or row["byte_size"] < 0
        ):
            raise Phase5IntakeError("cross-App source file byte_size is invalid")
        if not isinstance(row["sha256"], str) or not SHA256.fullmatch(row["sha256"]):
            raise Phase5IntakeError("cross-App source file SHA is invalid")
    if not isinstance(value["errors"], list):
        raise Phase5IntakeError("cross-App receipt errors must be an array")
    diagnostics = value["diagnostic_evidence"]
    if not isinstance(diagnostics, Mapping):
        raise Phase5IntakeError("cross-App diagnostics must be an object")
    _exact_keys(
        diagnostics,
        {
            "react_present_and_hashed",
            "reasoning_copied_to_receipt",
            "runner_self_report_copied_to_receipt",
            "old_verifier_verdict_copied_to_receipt",
        },
        "cross-App diagnostics",
    )
    if any(
        diagnostics[key] is not False
        for key in (
            "reasoning_copied_to_receipt",
            "runner_self_report_copied_to_receipt",
            "old_verifier_verdict_copied_to_receipt",
        )
    ):
        raise Phase5IntakeError(
            "diagnostic/self-report content leaked into cross-App receipt"
        )
    observability = value["cross_app_observability"]
    if value["status"] == "ACCEPTED_PENDING_SINGLE_OPERATOR_REVIEW":
        if value["collection_status"] != "RUN_COMPLETE" or value["errors"] != []:
            raise Phase5IntakeError("accepted cross-App receipt status/error drift")
        capability = value["evidence_capability_profile"]
        if not isinstance(capability, Mapping):
            raise Phase5IntakeError("accepted cross-App receipt lacks capability facts")
        _exact_keys(
            capability,
            {
                "screenshot_frames",
                "hierarchy_xml_frames",
                "hierarchy_raw_json_frames",
                "action_count",
                "action_types",
                "timestamps",
                "integrity",
            },
            "cross-App evidence capability",
        )
        count = _positive_int(capability["action_count"], "action_count")
        expected_frames = list(range(1, count + 1))
        if (
            capability["screenshot_frames"] != expected_frames
            or capability["hierarchy_xml_frames"] != expected_frames
            or capability["hierarchy_raw_json_frames"] not in ([], expected_frames)
            or not isinstance(capability["action_types"], list)
            or len(capability["action_types"]) != count
            or capability["timestamps"]
            != "RUN_LEVEL_WALL_CLOCK_ONLY_NO_FRAME_TIMESTAMPS"
            or capability["integrity"] != "VALID_WITH_TIMESTAMP_DEGRADATION"
        ):
            raise Phase5IntakeError("cross-App evidence capability drift")
        for action_type in capability["action_types"]:
            _canonical_string(action_type, "action type")
        if not isinstance(observability, Mapping):
            raise Phase5IntakeError(
                "accepted cross-App receipt lacks observability facts"
            )
        _exact_keys(
            observability,
            {
                "initial_app_declared_by_runner",
                "open_app_targets",
                "target_app_open_requested",
                "input_text_sha256_sequence",
                "foreground_package_per_frame_available",
                "app_switch_evidence_mode",
                "success_inferred_by_intake",
            },
            "cross-App observability",
        )
        if (
            observability["initial_app_declared_by_runner"] != SOURCE_APP
            or observability["foreground_package_per_frame_available"] is not False
            or observability["success_inferred_by_intake"] is not False
            or observability["app_switch_evidence_mode"]
            != "ACTION_LOG_PLUS_FRAME_EVIDENCE_NO_FRAME_FOREGROUND_PACKAGE_TELEMETRY"
        ):
            raise Phase5IntakeError("cross-App observability boundary drift")
        if not isinstance(observability["open_app_targets"], list) or not isinstance(
            observability["input_text_sha256_sequence"], list
        ):
            raise Phase5IntakeError("cross-App observability sequences are invalid")
        for target in observability["open_app_targets"]:
            _canonical_string(target, "open_app target")
        if not isinstance(
            observability["target_app_open_requested"], bool
        ) or observability["target_app_open_requested"] != (
            TARGET_APP in observability["open_app_targets"]
        ):
            raise Phase5IntakeError("cross-App target open fact is inconsistent")
        for digest in observability["input_text_sha256_sequence"]:
            if not isinstance(digest, str) or not SHA256.fullmatch(digest):
                raise Phase5IntakeError("cross-App input action SHA is invalid")
    else:
        if value["collection_status"] == "RUN_COMPLETE":
            raise Phase5IntakeError(
                "complete collection cannot be rejected as incomplete"
            )
        if value["errors"] != ["COLLECTION_NOT_COMPLETE"]:
            raise Phase5IntakeError("incomplete collection error taxonomy drift")
        if (
            value["evidence_capability_profile"] is not None
            or observability is not None
        ):
            raise Phase5IntakeError(
                "incomplete collection cannot claim cross-App observability"
            )
    return value


def verify_intake_receipt(receipt: Mapping[str, Any], run_dir: Path) -> None:
    validate_intake_receipt(receipt)
    expected_files = source_file_manifest(run_dir)
    if receipt["source_files"] != list(expected_files):
        raise Phase5IntakeError("cross-App intake source file hash drift")
    if receipt["source_tree_sha256"] != semantic_sha256(list(expected_files)):
        raise Phase5IntakeError("cross-App intake source tree hash drift")


__all__ = [name for name in globals() if not name.startswith("_")]
