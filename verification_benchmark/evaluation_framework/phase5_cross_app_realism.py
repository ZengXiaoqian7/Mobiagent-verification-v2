"""Strict Phase 5 v3 cross-App realism-pilot collection and intake."""

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


EXPERIMENT_SCHEMA_VERSION = "harmony-eval-phase5-cross-app-realism-manifest-v3"
EXPERIMENT_ID = "phase5-cross-app-realism-pilot-v3"
COLLECTION_SCHEMA_VERSION = "harmony-eval-phase5-cross-app-realism-collection-run-v3"
COLLECTOR_VERSION = "harmony-eval-phase5-cross-app-realism-collector-v3"
INTAKE_RECEIPT_SCHEMA_VERSION = (
    "harmony-eval-phase5-cross-app-realism-intake-receipt-v3"
)
INTAKE_VERSION = "harmony-eval-phase5-cross-app-realism-intake-v3"
PENDING_GROUND_TRUTH = "PENDING_SINGLE_OPERATOR_REVIEW"
SOURCE_APP = "淘宝"
SOURCE_PACKAGE = "com.taobao.taobao4hmos"
TARGET_APP = "小红书"
TARGET_PACKAGE = "com.xingin.xhs_hos"
OS_VERSION = "OpenHarmony-6.1.1.120"
TASK_ID = "crossapp-ranked-bag-review-001"
TASK_FAMILY = "cross_app_ranked_product_research_read_only"
PREDECESSOR_RUN_ID = "p5r-2026071700000002"
PREDECESSOR_FILE_SHA256 = (
    "68278635361d67c81d1344e0055e2ab9f4a545f329e943f34ca428a12e996176"
)
PREDECESSOR_SEMANTIC_SHA256 = (
    "775ad102320206c3f7b5e8e43600e8e4500e3bbc89e1ca6a9d2c2932325a569a"
)


def _strings(value: Any, context: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise Phase5IntakeError(f"{context} must be a non-empty array")
    for index, item in enumerate(value):
        _canonical_string(item, f"{context}[{index}]")
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
            "predecessor_disposition",
            "cohort",
            "package_probe",
            "agent",
            "collection_policy",
            "tasks",
        },
        "Phase 5 realism manifest",
    )
    fixed = {
        "schema_version": EXPERIMENT_SCHEMA_VERSION,
        "experiment_id": EXPERIMENT_ID,
        "protocol_status": "DEVELOPMENT_REALISM_PILOT_FROZEN_BEFORE_COLLECTION",
        "publication_eligible": False,
        "claim_boundary": CLAIM_BOUNDARY,
        "oracle_database_dependency": False,
        "phase4_status": "FROZEN_MECHANISM_VALIDATION_ONLY",
    }
    for key, expected in fixed.items():
        if value[key] != expected:
            raise Phase5IntakeError(f"realism manifest drift at {key}")

    predecessor = value["predecessor_disposition"]
    if not isinstance(predecessor, Mapping):
        raise Phase5IntakeError("predecessor disposition must be an object")
    _exact_keys(
        predecessor,
        {"run_id", "status", "file_sha256", "semantic_sha256"},
        "predecessor disposition",
    )
    if predecessor != {
        "run_id": PREDECESSOR_RUN_ID,
        "status": "RETIRED_FROM_PERFORMANCE_COHORT_CONSTRUCT_INVALID",
        "file_sha256": PREDECESSOR_FILE_SHA256,
        "semantic_sha256": PREDECESSOR_SEMANTIC_SHA256,
    }:
        raise Phase5IntakeError("predecessor disposition binding drift")

    cohort = value["cohort"]
    if not isinstance(cohort, Mapping):
        raise Phase5IntakeError("realism cohort must be an object")
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
        "realism cohort",
    )
    _canonical_string(cohort["cohort_id"], "cohort_id")
    expected_cohort = {
        "device_serial": AUTHORIZED_SERIAL,
        "device_model": AUTHORIZED_MODEL,
        "device_type": AUTHORIZED_DEVICE_TYPE,
        "posture": "FOLDED_OUTER_DISPLAY",
        "resolution": [1080, 2444],
        "os_version": OS_VERSION,
    }
    if any(cohort[key] != expected for key, expected in expected_cohort.items()):
        raise Phase5IntakeError("realism cohort identity/version drift")

    probe = value["package_probe"]
    if not isinstance(probe, Mapping):
        raise Phase5IntakeError("realism package probe must be an object")
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
        "realism package probe",
    )
    if (
        probe["schema_version"] != "harmony-package-probe-v1"
        or probe["status"] != "PASS"
        or probe["mode"] != "READ_ONLY_PACKAGE_VERSION_PROBE"
        or probe["report_file_sha256"] != PACKAGE_PROBE_REPORT_SHA256
        or probe["report_semantic_sha256"] != PACKAGE_PROBE_SEMANTIC_SHA256
        or probe["prior_same_path_attempt_preserved"] is not False
    ):
        raise Phase5IntakeError("realism package probe binding drift")
    _canonical_string(probe["provenance_caveat"], "package probe caveat")
    _validate_installed_apps(probe["installed_apps"])

    agent = value["agent"]
    if not isinstance(agent, Mapping):
        raise Phase5IntakeError("realism agent must be an object")
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
        "realism agent",
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
        "max_steps": 15,
    }
    for key, expected in expected_agent.items():
        if agent[key] != expected:
            raise Phase5IntakeError(f"realism agent drift at {key}")
    if not isinstance(agent["runner_module_sha256"], str) or not SHA256.fullmatch(
        agent["runner_module_sha256"]
    ):
        raise Phase5IntakeError("realism Runner SHA is invalid")

    policy = value["collection_policy"]
    if not isinstance(policy, Mapping):
        raise Phase5IntakeError("realism collection policy must be an object")
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
        "realism collection policy",
    )
    if (
        policy["risk_tier"] != "LOW_RISK_CROSS_APP_READ_ONLY_PRODUCT_RESEARCH"
        or policy["allowed_apps"] != [SOURCE_APP, TARGET_APP]
        or policy["collection_order"] != [TASK_ID]
        or policy["ground_truth_status"] != PENDING_GROUND_TRUTH
        or policy["guardrail_callbacks_allowed"] is not False
        or policy["overwrite_allowed"] is not False
        or policy["final_performance_cohort_status"]
        != "NOT_FROZEN_UNTIL_REALISM_PILOT_IS_REVIEWED"
    ):
        raise Phase5IntakeError("realism collection policy drift")
    for key in ("allowed_actions", "forbidden_actions", "abort_conditions"):
        _strings(policy[key], f"collection_policy.{key}")

    tasks = value["tasks"]
    if (
        not isinstance(tasks, list)
        or len(tasks) != 1
        or not isinstance(tasks[0], Mapping)
    ):
        raise Phase5IntakeError("realism pilot must freeze exactly one task")
    task = tasks[0]
    _exact_keys(
        task,
        {
            "task_id",
            "collection_ordinal",
            "task_family",
            "initial_app",
            "initial_package",
            "source_query",
            "source_sort",
            "selection_policy",
            "target_app",
            "target_package",
            "transfer_policy",
            "task_text",
            "expected_observable_criteria",
            "allowed_actions",
            "forbidden_actions",
            "contract_source_route",
            "smoke_priority",
        },
        "realism task",
    )
    task_fixed = {
        "task_id": TASK_ID,
        "collection_ordinal": 1,
        "task_family": TASK_FAMILY,
        "initial_app": SOURCE_APP,
        "initial_package": SOURCE_PACKAGE,
        "source_query": "通勤双肩包",
        "source_sort": "销量",
        "target_app": TARGET_APP,
        "target_package": TARGET_PACKAGE,
        "contract_source_route": "template",
        "smoke_priority": "REALISM_GATE",
    }
    for key, expected in task_fixed.items():
        if task[key] != expected:
            raise Phase5IntakeError(f"realism task drift at {key}")
    selection = task["selection_policy"]
    if not isinstance(selection, Mapping):
        raise Phase5IntakeError("selection policy must be an object")
    _exact_keys(
        selection,
        {
            "scope",
            "reading_order",
            "excluded_visible_markers",
            "rule",
            "global_ranking_claim_allowed",
        },
        "selection policy",
    )
    if selection != {
        "scope": "FIRST_LOADED_VIEWPORT_AFTER_VISIBLE_SALES_SORT",
        "reading_order": "TOP_TO_BOTTOM_LEFT_TO_RIGHT",
        "excluded_visible_markers": ["广告", "直播"],
        "rule": "FIRST_NON_EXCLUDED_CARD_WITH_VISIBLE_IDENTIFIABLE_BRAND_OR_DISTINCTIVE_PRODUCT_PHRASE",
        "global_ranking_claim_allowed": False,
    }:
        raise Phase5IntakeError("realism selection policy drift")
    transfer = task["transfer_policy"]
    if not isinstance(transfer, Mapping):
        raise Phase5IntakeError("transfer policy must be an object")
    _exact_keys(
        transfer,
        {
            "minimum_slots",
            "maximum_slots",
            "slot_description",
            "exact_visible_identity_required",
            "generic_category_or_style_only_forbidden",
        },
        "transfer policy",
    )
    if (
        transfer["minimum_slots"] != 1
        or transfer["maximum_slots"] != 1
        or transfer["exact_visible_identity_required"] is not True
        or transfer["generic_category_or_style_only_forbidden"] is not True
    ):
        raise Phase5IntakeError("realism transfer policy drift")
    _canonical_string(transfer["slot_description"], "slot description")
    text = _canonical_string(task["task_text"], "task text")
    for required in ("通勤双肩包", "销量", "广告", "直播", "open_app", "小红书"):
        if required not in text:
            raise Phase5IntakeError(f"realism task text omits frozen term: {required}")
    for key in ("expected_observable_criteria", "allowed_actions", "forbidden_actions"):
        _strings(task[key], f"task.{key}")
    return value


def load_experiment_manifest(path: Path) -> Mapping[str, Any]:
    return validate_experiment_manifest(
        strict_json_bytes(path.read_bytes(), context="Phase 5 realism manifest")
    )


def find_task(manifest: Mapping[str, Any], task_id: str) -> Mapping[str, Any]:
    validate_experiment_manifest(manifest)
    if task_id != TASK_ID:
        raise Phase5IntakeError(f"realism task_id must be {TASK_ID}")
    return manifest["tasks"][0]


def validate_predecessor_disposition(
    *, manifest: Mapping[str, Any], disposition_path: Path
) -> Mapping[str, Any]:
    validate_experiment_manifest(manifest)
    path = disposition_path.resolve(strict=True)
    value = strict_json_bytes(path.read_bytes(), context="predecessor disposition")
    frozen = manifest["predecessor_disposition"]
    if file_sha256(path) != frozen["file_sha256"]:
        raise Phase5IntakeError("predecessor disposition file SHA drift")
    if semantic_sha256(value) != frozen["semantic_sha256"]:
        raise Phase5IntakeError("predecessor disposition semantic SHA drift")
    if (
        value.get("run_id") != PREDECESSOR_RUN_ID
        or value.get("status") != frozen["status"]
        or value.get("performance_cohort_eligible") is not False
        or value.get("is_ground_truth") is not False
        or value.get("is_verifier_output") is not False
    ):
        raise Phase5IntakeError("predecessor disposition semantic boundary drift")
    return value


def validate_package_probe_report(
    *, manifest: Mapping[str, Any], report_path: Path
) -> Mapping[str, Any]:
    validate_experiment_manifest(manifest)
    path = report_path.resolve(strict=True)
    report = strict_json_bytes(path.read_bytes(), context="package probe report")
    probe = manifest["package_probe"]
    if file_sha256(path) != probe["report_file_sha256"]:
        raise Phase5IntakeError("realism package probe report file SHA drift")
    if semantic_sha256(report) != probe["report_semantic_sha256"]:
        raise Phase5IntakeError("realism package probe report semantic SHA drift")
    if (
        report.get("status") != "PASS"
        or report.get("authorized_serial") != AUTHORIZED_SERIAL
        or report.get("observed_targets") != [AUTHORIZED_SERIAL]
        or report.get("device")
        != {"model": AUTHORIZED_MODEL, "openharmony_fullname": OS_VERSION}
    ):
        raise Phase5IntakeError("realism package probe identity/status drift")
    packages = report.get("packages")
    if not isinstance(packages, list) or len(packages) != 2:
        raise Phase5IntakeError("realism package probe package count drift")
    for actual, frozen in zip(packages, probe["installed_apps"]):
        if not isinstance(actual, Mapping):
            raise Phase5IntakeError("realism package row must be an object")
        for key in ("package", "version_name", "version_code", "raw_dump_sha256"):
            if actual.get(key) != frozen[key]:
                raise Phase5IntakeError(f"realism package probe field drift: {key}")
        raw_ref = _safe_relative_ref(actual.get("raw_dump_path"), "raw_dump_path")
        raw_path = resolve_contained(path.parent, raw_ref)
        if file_sha256(raw_path) != frozen["raw_dump_sha256"]:
            raise Phase5IntakeError("realism package raw dump hash drift")
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
            "predecessor_disposition_sha256",
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
        "Phase 5 realism collection run",
    )
    fixed = {
        "schema_version": COLLECTION_SCHEMA_VERSION,
        "collector_version": COLLECTOR_VERSION,
        "experiment_id": EXPERIMENT_ID,
        "package_probe_report_sha256": PACKAGE_PROBE_REPORT_SHA256,
        "predecessor_disposition_sha256": PREDECESSOR_FILE_SHA256,
        "task_id": TASK_ID,
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
            raise Phase5IntakeError(f"realism collection run drift at {key}")
    if not isinstance(value["run_id"], str) or not RUN_ID.fullmatch(value["run_id"]):
        raise Phase5IntakeError("realism run_id is invalid")
    for key in ("experiment_manifest_sha256", "task_text_sha256"):
        if not isinstance(value[key], str) or not SHA256.fullmatch(value[key]):
            raise Phase5IntakeError(f"realism collection SHA invalid: {key}")
    _safe_relative_ref(value["trace_relpath"], "trace_relpath")
    if value["collection_status"] not in {
        "RUN_COMPLETE",
        "RUN_ABORTED",
        "RUN_FAILED",
        "RUN_IN_PROGRESS",
    }:
        raise Phase5IntakeError("realism collection status is invalid")
    _positive_int(value["attempt_ordinal"], "attempt_ordinal")
    if value["runner_exit_code"] is not None and (
        not isinstance(value["runner_exit_code"], int)
        or isinstance(value["runner_exit_code"], bool)
    ):
        raise Phase5IntakeError("realism runner_exit_code is invalid")
    device = value["device"]
    if device != {
        "serial": AUTHORIZED_SERIAL,
        "model": AUTHORIZED_MODEL,
        "device_type": AUTHORIZED_DEVICE_TYPE,
        "posture": "FOLDED_OUTER_DISPLAY",
        "resolution": [1080, 2444],
        "os_version": OS_VERSION,
    }:
        raise Phase5IntakeError("realism device binding drift")
    _validate_installed_apps(value["installed_apps"])
    agent = value["agent"]
    if not isinstance(agent, Mapping):
        raise Phase5IntakeError("realism run agent must be an object")
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
        "realism run agent",
    )
    if (
        agent["provider_base_url"] != AUTHORIZED_PROVIDER_BASE_URL
        or agent["model"] != AUTHORIZED_PROVIDER_MODEL
        or agent["transport"] != AUTHORIZED_TRANSPORT
    ):
        raise Phase5IntakeError("realism provider/model/transport drift")
    for key in ("runner_module_sha256", "collector_source_sha256"):
        if not isinstance(agent[key], str) or not SHA256.fullmatch(agent[key]):
            raise Phase5IntakeError(f"realism run agent SHA invalid: {key}")
    _canonical_string(agent["evaluation_git_head"], "evaluation_git_head")
    _canonical_string(agent["runner_repository_git_head"], "runner_repository_git_head")
    return value


def load_collection_run_manifest(run_dir: Path) -> Mapping[str, Any]:
    path = resolve_contained(run_dir, "phase5_realism_collection_run_manifest.json")
    return validate_collection_run_manifest(
        strict_json_bytes(path.read_bytes(), context="Phase 5 realism collection run")
    )


def _action_facts(trace_dir: Path) -> Mapping[str, Any]:
    actions = strict_json_bytes(
        (trace_dir / "actions.json").read_bytes(), context="realism actions.json"
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
        "target_app_open_requested": TARGET_APP in targets,
        "input_text_sha256_sequence": input_hashes,
        "foreground_package_per_frame_available": False,
        "sales_sort_activation_inferred_by_intake": False,
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
        raise Phase5IntakeError("realism run/manifest hash drift")
    task = find_task(experiment_manifest, run["task_id"])
    if (
        run["task_text_sha256"]
        != hashlib.sha256(task["task_text"].encode("utf-8")).hexdigest()
    ):
        raise Phase5IntakeError("realism run/task text hash drift")
    if run["installed_apps"] != experiment_manifest["package_probe"]["installed_apps"]:
        raise Phase5IntakeError("realism installed-App binding drift")
    if (
        run["agent"]["runner_module_sha256"]
        != experiment_manifest["agent"]["runner_module_sha256"]
    ):
        raise Phase5IntakeError("realism Runner binding drift")
    trace_dir = resolve_contained(run_dir, run["trace_relpath"])
    if not trace_dir.is_dir():
        raise Phase5IntakeError("realism trace_relpath is not a directory")
    files = source_file_manifest(run_dir)
    eligible = run["collection_status"] == "RUN_COMPLETE"
    trace_audit = None
    action_facts = None
    errors: list[str] = []
    if eligible:
        trace_task = {**task, "app": task["initial_app"]}
        trace_audit = audit_trace(trace_dir, trace_task)
        action_facts = _action_facts(trace_dir)
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
        "predecessor_disposition_sha256": PREDECESSOR_FILE_SHA256,
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
            "predecessor_disposition_sha256",
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
        "Phase 5 realism intake receipt",
    )
    fixed = {
        "schema_version": INTAKE_RECEIPT_SCHEMA_VERSION,
        "intake_version": INTAKE_VERSION,
        "publication_eligible": False,
        "experiment_id": EXPERIMENT_ID,
        "package_probe_report_sha256": PACKAGE_PROBE_REPORT_SHA256,
        "predecessor_disposition_sha256": PREDECESSOR_FILE_SHA256,
        "task_id": TASK_ID,
        "ground_truth_status": PENDING_GROUND_TRUTH,
        "oracle_database_dependency": False,
        "claim_boundary": CLAIM_BOUNDARY,
    }
    for key, expected in fixed.items():
        if value[key] != expected:
            raise Phase5IntakeError(f"realism receipt drift at {key}")
    for key in (
        "intake_source_sha256",
        "experiment_manifest_sha256",
        "collection_run_manifest_sha256",
        "source_tree_sha256",
    ):
        if not isinstance(value[key], str) or not SHA256.fullmatch(value[key]):
            raise Phase5IntakeError(f"realism receipt SHA invalid: {key}")
    if not isinstance(value["run_id"], str) or not RUN_ID.fullmatch(value["run_id"]):
        raise Phase5IntakeError("realism receipt run_id is invalid")
    if not isinstance(value["source_files"], list) or not value["source_files"]:
        raise Phase5IntakeError("realism receipt source_files must be non-empty")
    diagnostics = value["diagnostic_evidence"]
    if not isinstance(diagnostics, Mapping):
        raise Phase5IntakeError("realism diagnostics must be an object")
    _exact_keys(
        diagnostics,
        {
            "react_present_and_hashed",
            "reasoning_copied_to_receipt",
            "runner_self_report_copied_to_receipt",
            "old_verifier_verdict_copied_to_receipt",
        },
        "realism diagnostics",
    )
    if any(
        diagnostics[key] is not False
        for key in (
            "reasoning_copied_to_receipt",
            "runner_self_report_copied_to_receipt",
            "old_verifier_verdict_copied_to_receipt",
        )
    ):
        raise Phase5IntakeError("diagnostic/self-report leaked into realism receipt")
    accepted = value["status"] == "ACCEPTED_PENDING_SINGLE_OPERATOR_REVIEW"
    if accepted:
        if value["collection_status"] != "RUN_COMPLETE" or value["errors"] != []:
            raise Phase5IntakeError("accepted realism receipt status drift")
        capability = value["evidence_capability_profile"]
        observability = value["cross_app_observability"]
        if not isinstance(capability, Mapping) or not isinstance(
            observability, Mapping
        ):
            raise Phase5IntakeError("accepted realism receipt lacks evidence facts")
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
            raise Phase5IntakeError("realism evidence capability drift")
        _exact_keys(
            observability,
            {
                "initial_app_declared_by_runner",
                "open_app_targets",
                "target_app_open_requested",
                "input_text_sha256_sequence",
                "foreground_package_per_frame_available",
                "sales_sort_activation_inferred_by_intake",
                "selection_rule_satisfaction_inferred_by_intake",
                "success_inferred_by_intake",
            },
            "realism observability",
        )
        if (
            observability["initial_app_declared_by_runner"] != SOURCE_APP
            or observability["target_app_open_requested"]
            != (TARGET_APP in observability["open_app_targets"])
            or observability["foreground_package_per_frame_available"] is not False
            or observability["sales_sort_activation_inferred_by_intake"] is not False
            or observability["selection_rule_satisfaction_inferred_by_intake"]
            is not False
            or observability["success_inferred_by_intake"] is not False
        ):
            raise Phase5IntakeError("realism observability boundary drift")
        for digest in observability["input_text_sha256_sequence"]:
            if not isinstance(digest, str) or not SHA256.fullmatch(digest):
                raise Phase5IntakeError("realism input action SHA invalid")
    else:
        if value["status"] != "REJECTED_COLLECTION_INCOMPLETE":
            raise Phase5IntakeError("realism receipt status is invalid")
        if value["collection_status"] == "RUN_COMPLETE":
            raise Phase5IntakeError(
                "complete realism run cannot be rejected incomplete"
            )
        if value["errors"] != ["COLLECTION_NOT_COMPLETE"]:
            raise Phase5IntakeError("realism incomplete error taxonomy drift")
        if (
            value["evidence_capability_profile"] is not None
            or value["cross_app_observability"] is not None
        ):
            raise Phase5IntakeError(
                "incomplete realism run cannot claim evidence facts"
            )
    return value


def verify_intake_receipt(receipt: Mapping[str, Any], run_dir: Path) -> None:
    validate_intake_receipt(receipt)
    expected = source_file_manifest(run_dir)
    if receipt["source_files"] != list(expected):
        raise Phase5IntakeError("realism intake source file hash drift")
    if receipt["source_tree_sha256"] != semantic_sha256(list(expected)):
        raise Phase5IntakeError("realism intake source tree hash drift")


__all__ = [name for name in globals() if not name.startswith("_")]
