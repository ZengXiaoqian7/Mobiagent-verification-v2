"""Phase 5 final cross-App realism cohort manifest, collection, and intake."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Mapping

from .phase5_cross_app import (
    PACKAGE_PROBE_REPORT_SHA256,
    PACKAGE_PROBE_SEMANTIC_SHA256,
    _validate_installed_apps,
)
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
    TASK_ID as TASK_ID_RE,
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
)


EXPERIMENT_SCHEMA_VERSION = "harmony-eval-phase5-cross-app-realism-cohort-manifest-v1"
EXPERIMENT_ID = "phase5-cross-app-realism-cohort-v1"
COLLECTION_SCHEMA_VERSION = "harmony-eval-phase5-cross-app-realism-cohort-run-v1"
COLLECTOR_VERSION = "harmony-eval-phase5-cross-app-realism-cohort-collector-v1"
INTAKE_RECEIPT_SCHEMA_VERSION = (
    "harmony-eval-phase5-cross-app-realism-cohort-intake-receipt-v1"
)
INTAKE_VERSION = "harmony-eval-phase5-cross-app-realism-cohort-intake-v1"
PENDING_GROUND_TRUTH = "PENDING_SINGLE_OPERATOR_REVIEW"
OS_VERSION = "OpenHarmony-6.1.1.120"
XHS_APP = "小红书"
XHS_PACKAGE = "com.xingin.xhs_hos"
TAOBAO_APP = "淘宝"
TAOBAO_PACKAGE = "com.taobao.taobao4hmos"
TASK_FAMILY = "cross_app_realism_final_read_only"
PILOT_GT_FILE_SHA256 = (
    "8529796ccfde1f62822680aaaf230877e8501d7a9087764dae89674f0e903341"
)
PILOT_GT_SEMANTIC_SHA256 = (
    "41b04d1421402850d4f5033fe2a8f5d23ebddc07ea3f3fa65197c5c0e3c60961"
)


def _strings(value: Any, context: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise Phase5IntakeError(f"{context} must be a non-empty array")
    for index, item in enumerate(value):
        _canonical_string(item, f"{context}[{index}]")
    return value


def _app_package(app: str) -> str:
    if app == TAOBAO_APP:
        return TAOBAO_PACKAGE
    if app == XHS_APP:
        return XHS_PACKAGE
    raise Phase5IntakeError(f"unsupported cohort App: {app}")


def _validate_common_identity(value: Mapping[str, Any]) -> None:
    cohort = value["cohort"]
    if not isinstance(cohort, Mapping):
        raise Phase5IntakeError("cohort must be an object")
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
        "cohort",
    )
    if cohort != {
        "cohort_id": "mate-x7-folded-outer-1080x2444-cross-app-final-v1",
        "device_serial": AUTHORIZED_SERIAL,
        "device_model": AUTHORIZED_MODEL,
        "device_type": AUTHORIZED_DEVICE_TYPE,
        "posture": "FOLDED_OUTER_DISPLAY",
        "resolution": [1080, 2444],
        "os_version": OS_VERSION,
    }:
        raise Phase5IntakeError("cohort identity/version drift")

    probe = value["package_probe"]
    if not isinstance(probe, Mapping):
        raise Phase5IntakeError("package probe must be an object")
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
        "package probe",
    )
    if (
        probe["schema_version"] != "harmony-package-probe-v1"
        or probe["status"] != "PASS"
        or probe["mode"] != "READ_ONLY_PACKAGE_VERSION_PROBE"
        or probe["report_file_sha256"] != PACKAGE_PROBE_REPORT_SHA256
        or probe["report_semantic_sha256"] != PACKAGE_PROBE_SEMANTIC_SHA256
        or probe["prior_same_path_attempt_preserved"] is not False
    ):
        raise Phase5IntakeError("package probe binding drift")
    _canonical_string(probe["provenance_caveat"], "package probe caveat")
    _validate_installed_apps(probe["installed_apps"])

    agent = value["agent"]
    if not isinstance(agent, Mapping):
        raise Phase5IntakeError("agent must be an object")
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
        "agent",
    )
    expected_agent = {
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
        "max_steps": 18,
    }
    for key, expected in expected_agent.items():
        if agent[key] != expected:
            raise Phase5IntakeError(f"agent drift at {key}")
    if not isinstance(agent["runner_module_sha256"], str) or not SHA256.fullmatch(
        agent["runner_module_sha256"]
    ):
        raise Phase5IntakeError("runner_module_sha256 is invalid")


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
            "pilot_ground_truth",
            "cohort",
            "package_probe",
            "agent",
            "collection_policy",
            "tasks",
        },
        "Phase 5 final realism cohort manifest",
    )
    fixed = {
        "schema_version": EXPERIMENT_SCHEMA_VERSION,
        "experiment_id": EXPERIMENT_ID,
        "protocol_status": "FINAL_CROSS_APP_REALISM_COHORT_FROZEN_BEFORE_COLLECTION",
        "publication_eligible": False,
        "claim_boundary": CLAIM_BOUNDARY,
        "oracle_database_dependency": False,
        "phase4_status": "FROZEN_MECHANISM_VALIDATION_ONLY",
    }
    for key, expected in fixed.items():
        if value[key] != expected:
            raise Phase5IntakeError(f"cohort manifest drift at {key}")

    pilot = value["pilot_ground_truth"]
    if not isinstance(pilot, Mapping):
        raise Phase5IntakeError("pilot_ground_truth must be an object")
    _exact_keys(
        pilot,
        {"run_id", "verdict", "file_sha256", "semantic_sha256", "used_for_design"},
        "pilot_ground_truth",
    )
    if pilot != {
        "run_id": "p5r-2026071700000003",
        "verdict": "FAIL",
        "file_sha256": PILOT_GT_FILE_SHA256,
        "semantic_sha256": PILOT_GT_SEMANTIC_SHA256,
        "used_for_design": True,
    }:
        raise Phase5IntakeError("pilot GT binding drift")

    _validate_common_identity(value)

    policy = value["collection_policy"]
    if not isinstance(policy, Mapping):
        raise Phase5IntakeError("collection_policy must be an object")
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
            "verifier_allowed_before_gt",
            "performance_cohort_status",
        },
        "collection_policy",
    )
    if (
        policy["risk_tier"] != "LOW_RISK_CROSS_APP_READ_ONLY_REALISM_FINAL"
        or set(policy["allowed_apps"]) != {TAOBAO_APP, XHS_APP}
        or policy["ground_truth_status"] != PENDING_GROUND_TRUTH
        or policy["guardrail_callbacks_allowed"] is not False
        or policy["overwrite_allowed"] is not False
        or policy["verifier_allowed_before_gt"] is not False
        or policy["performance_cohort_status"] != "FROZEN_FOR_SINGLE_OPERATOR_GT"
    ):
        raise Phase5IntakeError("collection policy drift")
    for key in ("allowed_actions", "forbidden_actions", "abort_conditions"):
        _strings(policy[key], f"collection_policy.{key}")

    tasks = value["tasks"]
    if not isinstance(tasks, list) or len(tasks) != 12:
        raise Phase5IntakeError("final cohort must freeze exactly 12 tasks")
    seen: set[str] = set()
    flows: set[str] = set()
    task_ids: list[str] = []
    for ordinal, task in enumerate(tasks, 1):
        if not isinstance(task, Mapping):
            raise Phase5IntakeError("task must be an object")
        _validate_task(task, ordinal)
        task_id = task["task_id"]
        if task_id in seen:
            raise Phase5IntakeError("duplicate task_id")
        seen.add(task_id)
        task_ids.append(task_id)
        flows.add(task["flow_type"])
    if policy["collection_order"] != task_ids:
        raise Phase5IntakeError("collection order must equal task order")
    if flows != {"taobao_to_xhs", "xhs_to_taobao"}:
        raise Phase5IntakeError("cohort must cover both cross-App directions")
    return value


def _validate_task(task: Mapping[str, Any], ordinal: int) -> None:
    _exact_keys(
        task,
        {
            "task_id",
            "collection_ordinal",
            "task_family",
            "flow_type",
            "initial_app",
            "initial_package",
            "target_app",
            "target_package",
            "source_query",
            "selection_policy",
            "transfer_policy",
            "task_text",
            "expected_observable_criteria",
            "allowed_actions",
            "forbidden_actions",
            "difficulty_tags",
        },
        "cohort task",
    )
    task_id = task["task_id"]
    if not isinstance(task_id, str) or not TASK_ID_RE.fullmatch(task_id):
        raise Phase5IntakeError("invalid task_id")
    if task["collection_ordinal"] != ordinal:
        raise Phase5IntakeError("collection ordinal drift")
    if task["task_family"] != TASK_FAMILY:
        raise Phase5IntakeError("task family drift")
    if task["flow_type"] not in {"taobao_to_xhs", "xhs_to_taobao"}:
        raise Phase5IntakeError("flow_type drift")
    if task["initial_app"] == task["target_app"]:
        raise Phase5IntakeError("task must cross Apps")
    if task["initial_package"] != _app_package(task["initial_app"]):
        raise Phase5IntakeError("initial package drift")
    if task["target_package"] != _app_package(task["target_app"]):
        raise Phase5IntakeError("target package drift")
    _canonical_string(task["source_query"], "source_query")
    text = _canonical_string(task["task_text"], "task_text")
    for required in (task["source_query"], task["target_app"], "open_app"):
        if required not in text:
            raise Phase5IntakeError(f"task text omits frozen term: {required}")
    for key in ("expected_observable_criteria", "allowed_actions", "forbidden_actions"):
        _strings(task[key], f"task.{key}")
    _strings(task["difficulty_tags"], "task.difficulty_tags")
    selection = task["selection_policy"]
    if not isinstance(selection, Mapping):
        raise Phase5IntakeError("selection_policy must be an object")
    _exact_keys(
        selection,
        {"source_surface", "rule", "excluded_visible_markers", "global_claim_allowed"},
        "selection_policy",
    )
    if selection["global_claim_allowed"] is not False:
        raise Phase5IntakeError("global claim is not allowed")
    _strings(selection["excluded_visible_markers"], "selection excluded markers")
    _canonical_string(selection["source_surface"], "selection source surface")
    _canonical_string(selection["rule"], "selection rule")
    transfer = task["transfer_policy"]
    if not isinstance(transfer, Mapping):
        raise Phase5IntakeError("transfer_policy must be an object")
    _exact_keys(
        transfer,
        {
            "minimum_slots",
            "maximum_slots",
            "slot_description",
            "exact_visible_support_required",
            "generic_only_forbidden",
        },
        "transfer_policy",
    )
    min_slots = _positive_int(transfer["minimum_slots"], "minimum_slots")
    max_slots = _positive_int(transfer["maximum_slots"], "maximum_slots")
    if min_slots > max_slots or max_slots > 2:
        raise Phase5IntakeError("transfer slot range drift")
    if (
        transfer["exact_visible_support_required"] is not True
        or transfer["generic_only_forbidden"] is not True
    ):
        raise Phase5IntakeError("transfer policy boundary drift")
    _canonical_string(transfer["slot_description"], "slot_description")


def load_experiment_manifest(path: Path) -> Mapping[str, Any]:
    return validate_experiment_manifest(
        strict_json_bytes(path.read_bytes(), context="Phase 5 final cohort manifest")
    )


def find_task(manifest: Mapping[str, Any], task_id: str) -> Mapping[str, Any]:
    validate_experiment_manifest(manifest)
    for task in manifest["tasks"]:
        if task["task_id"] == task_id:
            return task
    raise Phase5IntakeError(f"unknown final cohort task_id: {task_id}")


def validate_package_probe_report(
    *, manifest: Mapping[str, Any], report_path: Path
) -> Mapping[str, Any]:
    validate_experiment_manifest(manifest)
    path = report_path.resolve(strict=True)
    report = strict_json_bytes(path.read_bytes(), context="package probe report")
    probe = manifest["package_probe"]
    if file_sha256(path) != probe["report_file_sha256"]:
        raise Phase5IntakeError("package probe report file SHA drift")
    if semantic_sha256(report) != probe["report_semantic_sha256"]:
        raise Phase5IntakeError("package probe report semantic SHA drift")
    if (
        report.get("status") != "PASS"
        or report.get("authorized_serial") != AUTHORIZED_SERIAL
        or report.get("observed_targets") != [AUTHORIZED_SERIAL]
        or report.get("device")
        != {"model": AUTHORIZED_MODEL, "openharmony_fullname": OS_VERSION}
    ):
        raise Phase5IntakeError("package probe identity/status drift")
    packages = report.get("packages")
    if not isinstance(packages, list) or len(packages) != 2:
        raise Phase5IntakeError("package probe package count drift")
    for actual, frozen in zip(packages, probe["installed_apps"]):
        if not isinstance(actual, Mapping):
            raise Phase5IntakeError("package row must be an object")
        for key in ("package", "version_name", "version_code", "raw_dump_sha256"):
            if actual.get(key) != frozen[key]:
                raise Phase5IntakeError(f"package probe field drift: {key}")
        raw_path = resolve_contained(
            path.parent,
            _safe_relative_ref(actual.get("raw_dump_path"), "raw_dump_path"),
        )
        if file_sha256(raw_path) != frozen["raw_dump_sha256"]:
            raise Phase5IntakeError("package raw dump hash drift")
    return report


def validate_pilot_ground_truth(
    *, manifest: Mapping[str, Any], ground_truth_path: Path
) -> Mapping[str, Any]:
    validate_experiment_manifest(manifest)
    path = ground_truth_path.resolve(strict=True)
    value = strict_json_bytes(path.read_bytes(), context="pilot Ground Truth")
    if file_sha256(path) != PILOT_GT_FILE_SHA256:
        raise Phase5IntakeError("pilot GT file SHA drift")
    if semantic_sha256(value) != PILOT_GT_SEMANTIC_SHA256:
        raise Phase5IntakeError("pilot GT semantic SHA drift")
    if (
        value.get("run_id") != "p5r-2026071700000003"
        or value.get("verdict") != "FAIL"
        or value.get("automated_verifier_run_before_review") is not False
        or value.get("publication_eligible") is not False
    ):
        raise Phase5IntakeError("pilot GT semantic boundary drift")
    return value


def validate_collection_run_manifest(value: Mapping[str, Any]) -> Mapping[str, Any]:
    _exact_keys(
        value,
        {
            "schema_version",
            "collector_version",
            "experiment_id",
            "experiment_manifest_sha256",
            "package_probe_report_sha256",
            "pilot_ground_truth_sha256",
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
        "Phase 5 final cohort collection run",
    )
    fixed = {
        "schema_version": COLLECTION_SCHEMA_VERSION,
        "collector_version": COLLECTOR_VERSION,
        "experiment_id": EXPERIMENT_ID,
        "package_probe_report_sha256": PACKAGE_PROBE_REPORT_SHA256,
        "pilot_ground_truth_sha256": PILOT_GT_FILE_SHA256,
        "oracle_database_dependency": False,
        "ground_truth_status": PENDING_GROUND_TRUTH,
        "guardrail_callback_enabled": False,
        "start_state_guard_enabled": False,
    }
    for key, expected in fixed.items():
        if value[key] != expected:
            raise Phase5IntakeError(f"collection run drift at {key}")
    if not isinstance(value["run_id"], str) or not RUN_ID.fullmatch(value["run_id"]):
        raise Phase5IntakeError("run_id is invalid")
    if not isinstance(value["task_id"], str) or not TASK_ID_RE.fullmatch(
        value["task_id"]
    ):
        raise Phase5IntakeError("task_id is invalid")
    for key in ("experiment_manifest_sha256", "task_text_sha256"):
        if not isinstance(value[key], str) or not SHA256.fullmatch(value[key]):
            raise Phase5IntakeError(f"collection SHA invalid: {key}")
    if value["initial_package"] != _app_package(value["initial_app"]):
        raise Phase5IntakeError("collection initial App/package drift")
    if value["target_package"] != _app_package(value["target_app"]):
        raise Phase5IntakeError("collection target App/package drift")
    _safe_relative_ref(value["trace_relpath"], "trace_relpath")
    if value["collection_status"] not in {
        "RUN_COMPLETE",
        "RUN_ABORTED",
        "RUN_FAILED",
        "RUN_IN_PROGRESS",
    }:
        raise Phase5IntakeError("collection status is invalid")
    _positive_int(value["attempt_ordinal"], "attempt_ordinal")
    if value["runner_exit_code"] is not None and (
        not isinstance(value["runner_exit_code"], int)
        or isinstance(value["runner_exit_code"], bool)
    ):
        raise Phase5IntakeError("runner_exit_code is invalid")
    if value["device"] != {
        "serial": AUTHORIZED_SERIAL,
        "model": AUTHORIZED_MODEL,
        "device_type": AUTHORIZED_DEVICE_TYPE,
        "posture": "FOLDED_OUTER_DISPLAY",
        "resolution": [1080, 2444],
        "os_version": OS_VERSION,
    }:
        raise Phase5IntakeError("device binding drift")
    _validate_installed_apps(value["installed_apps"])
    agent = value["agent"]
    if not isinstance(agent, Mapping):
        raise Phase5IntakeError("run agent must be an object")
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
        "run agent",
    )
    if (
        agent["provider_base_url"] != AUTHORIZED_PROVIDER_BASE_URL
        or agent["model"] != AUTHORIZED_PROVIDER_MODEL
        or agent["transport"] != AUTHORIZED_TRANSPORT
    ):
        raise Phase5IntakeError("provider/model/transport drift")
    for key in ("runner_module_sha256", "collector_source_sha256"):
        if not isinstance(agent[key], str) or not SHA256.fullmatch(agent[key]):
            raise Phase5IntakeError(f"run agent SHA invalid: {key}")
    _canonical_string(agent["evaluation_git_head"], "evaluation_git_head")
    _canonical_string(agent["runner_repository_git_head"], "runner_repository_git_head")
    return value


def load_collection_run_manifest(run_dir: Path) -> Mapping[str, Any]:
    path = resolve_contained(
        run_dir, "phase5_realism_cohort_collection_run_manifest.json"
    )
    return validate_collection_run_manifest(
        strict_json_bytes(path.read_bytes(), context="Phase 5 final cohort run")
    )


def _action_facts(trace_dir: Path, *, target_app: str) -> Mapping[str, Any]:
    actions = strict_json_bytes(
        (trace_dir / "actions.json").read_bytes(), context="cohort actions.json"
    )
    rows = actions["actions"]
    targets = [row.get("app_name") for row in rows if row.get("type") == "open_app"]
    input_hashes = [
        hashlib.sha256(row["text"].encode("utf-8")).hexdigest()
        for row in rows
        if row.get("type") in {"input", "click_input"}
        and isinstance(row.get("text"), str)
    ]
    return {
        "initial_app_declared_by_runner": actions["app_name"],
        "open_app_targets": targets,
        "target_app_open_requested": target_app in targets,
        "input_text_sha256_sequence": input_hashes,
        "foreground_package_per_frame_available": False,
        "selection_rule_satisfaction_inferred_by_intake": False,
        "success_inferred_by_intake": False,
    }


def build_intake_receipt(
    *, experiment_manifest: Mapping[str, Any], run_dir: Path
) -> Mapping[str, Any]:
    validate_experiment_manifest(experiment_manifest)
    run = load_collection_run_manifest(run_dir)
    manifest_sha = semantic_sha256(experiment_manifest)
    if run["experiment_manifest_sha256"] != manifest_sha:
        raise Phase5IntakeError("run/manifest hash drift")
    task = find_task(experiment_manifest, run["task_id"])
    if (
        run["task_text_sha256"]
        != hashlib.sha256(task["task_text"].encode("utf-8")).hexdigest()
    ):
        raise Phase5IntakeError("run/task text hash drift")
    for key in ("initial_app", "initial_package", "target_app", "target_package"):
        if run[key] != task[key]:
            raise Phase5IntakeError(f"run/task drift at {key}")
    if run["installed_apps"] != experiment_manifest["package_probe"]["installed_apps"]:
        raise Phase5IntakeError("installed-App binding drift")
    if (
        run["agent"]["runner_module_sha256"]
        != experiment_manifest["agent"]["runner_module_sha256"]
    ):
        raise Phase5IntakeError("Runner binding drift")
    trace_dir = resolve_contained(run_dir, run["trace_relpath"])
    if not trace_dir.is_dir():
        raise Phase5IntakeError("trace_relpath is not a directory")
    files = source_file_manifest(run_dir)
    eligible = run["collection_status"] == "RUN_COMPLETE"
    trace_audit = None
    action_facts = None
    errors: list[str] = []
    if eligible:
        trace_audit = audit_trace(trace_dir, {**task, "app": task["initial_app"]})
        action_facts = _action_facts(trace_dir, target_app=task["target_app"])
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
        "experiment_id": EXPERIMENT_ID,
        "experiment_manifest_sha256": manifest_sha,
        "package_probe_report_sha256": PACKAGE_PROBE_REPORT_SHA256,
        "pilot_ground_truth_sha256": PILOT_GT_FILE_SHA256,
        "run_id": run["run_id"],
        "task_id": task["task_id"],
        "initial_app": task["initial_app"],
        "target_app": task["target_app"],
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
            "pilot_ground_truth_sha256",
            "run_id",
            "task_id",
            "initial_app",
            "target_app",
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
        "Phase 5 final cohort intake receipt",
    )
    fixed = {
        "schema_version": INTAKE_RECEIPT_SCHEMA_VERSION,
        "intake_version": INTAKE_VERSION,
        "publication_eligible": False,
        "experiment_id": EXPERIMENT_ID,
        "package_probe_report_sha256": PACKAGE_PROBE_REPORT_SHA256,
        "pilot_ground_truth_sha256": PILOT_GT_FILE_SHA256,
        "ground_truth_status": PENDING_GROUND_TRUTH,
        "oracle_database_dependency": False,
        "claim_boundary": CLAIM_BOUNDARY,
    }
    for key, expected in fixed.items():
        if value[key] != expected:
            raise Phase5IntakeError(f"cohort receipt drift at {key}")
    for key in (
        "intake_source_sha256",
        "experiment_manifest_sha256",
        "collection_run_manifest_sha256",
        "source_tree_sha256",
    ):
        if not isinstance(value[key], str) or not SHA256.fullmatch(value[key]):
            raise Phase5IntakeError(f"receipt SHA invalid: {key}")
    if not isinstance(value["run_id"], str) or not RUN_ID.fullmatch(value["run_id"]):
        raise Phase5IntakeError("receipt run_id is invalid")
    if not isinstance(value["task_id"], str) or not TASK_ID_RE.fullmatch(
        value["task_id"]
    ):
        raise Phase5IntakeError("receipt task_id is invalid")
    for key in ("initial_app", "target_app"):
        if value[key] not in {TAOBAO_APP, XHS_APP}:
            raise Phase5IntakeError(f"receipt App is invalid: {key}")
    if value["initial_app"] == value["target_app"]:
        raise Phase5IntakeError("receipt must bind a cross-App task")
    if not isinstance(value["source_files"], list) or not value["source_files"]:
        raise Phase5IntakeError("receipt source_files must be non-empty")
    diagnostics = value["diagnostic_evidence"]
    if not isinstance(diagnostics, Mapping):
        raise Phase5IntakeError("diagnostics must be an object")
    _exact_keys(
        diagnostics,
        {
            "react_present_and_hashed",
            "reasoning_copied_to_receipt",
            "runner_self_report_copied_to_receipt",
            "old_verifier_verdict_copied_to_receipt",
        },
        "diagnostics",
    )
    if any(
        diagnostics[key] is not False
        for key in (
            "reasoning_copied_to_receipt",
            "runner_self_report_copied_to_receipt",
            "old_verifier_verdict_copied_to_receipt",
        )
    ):
        raise Phase5IntakeError("diagnostic/self-report leaked into receipt")
    accepted = value["status"] == "ACCEPTED_PENDING_SINGLE_OPERATOR_REVIEW"
    if accepted:
        if value["collection_status"] != "RUN_COMPLETE" or value["errors"] != []:
            raise Phase5IntakeError("accepted receipt status drift")
        capability = value["evidence_capability_profile"]
        observability = value["cross_app_observability"]
        if not isinstance(capability, Mapping) or not isinstance(
            observability, Mapping
        ):
            raise Phase5IntakeError("accepted receipt lacks evidence facts")
        count = _positive_int(capability.get("action_count"), "action_count")
        expected_frames = list(range(1, count + 1))
        if (
            capability.get("screenshot_frames") != expected_frames
            or capability.get("hierarchy_xml_frames") != expected_frames
            or capability.get("hierarchy_raw_json_frames") not in ([], expected_frames)
            or len(capability.get("action_types", [])) != count
            or capability.get("timestamps")
            != "RUN_LEVEL_WALL_CLOCK_ONLY_NO_FRAME_TIMESTAMPS"
            or capability.get("integrity") != "VALID_WITH_TIMESTAMP_DEGRADATION"
        ):
            raise Phase5IntakeError("evidence capability drift")
        _exact_keys(
            observability,
            {
                "initial_app_declared_by_runner",
                "open_app_targets",
                "target_app_open_requested",
                "input_text_sha256_sequence",
                "foreground_package_per_frame_available",
                "selection_rule_satisfaction_inferred_by_intake",
                "success_inferred_by_intake",
            },
            "observability",
        )
        if (
            observability["initial_app_declared_by_runner"] != value["initial_app"]
            or observability["target_app_open_requested"]
            != (value["target_app"] in observability["open_app_targets"])
            or observability["foreground_package_per_frame_available"] is not False
            or observability["selection_rule_satisfaction_inferred_by_intake"]
            is not False
            or observability["success_inferred_by_intake"] is not False
        ):
            raise Phase5IntakeError("observability boundary drift")
        for digest in observability["input_text_sha256_sequence"]:
            if not isinstance(digest, str) or not SHA256.fullmatch(digest):
                raise Phase5IntakeError("input action SHA invalid")
    else:
        if value["status"] != "REJECTED_COLLECTION_INCOMPLETE":
            raise Phase5IntakeError("receipt status is invalid")
        if value["collection_status"] == "RUN_COMPLETE":
            raise Phase5IntakeError("complete run cannot be rejected incomplete")
        if value["errors"] != ["COLLECTION_NOT_COMPLETE"]:
            raise Phase5IntakeError("incomplete error taxonomy drift")
        if (
            value["evidence_capability_profile"] is not None
            or value["cross_app_observability"] is not None
        ):
            raise Phase5IntakeError("incomplete run cannot claim evidence facts")
    return value


def verify_intake_receipt(receipt: Mapping[str, Any], run_dir: Path) -> None:
    validate_intake_receipt(receipt)
    expected = source_file_manifest(run_dir)
    if receipt["source_files"] != list(expected):
        raise Phase5IntakeError("intake source file hash drift")
    if receipt["source_tree_sha256"] != semantic_sha256(list(expected)):
        raise Phase5IntakeError("intake source tree hash drift")


__all__ = [name for name in globals() if not name.startswith("_")]
