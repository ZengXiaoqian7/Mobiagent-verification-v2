"""Phase 5 ClawMate runner trace-capability probe."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping, Optional

from .phase5_intake import (
    AUTHORIZED_DEVICE_TYPE,
    AUTHORIZED_MODEL,
    AUTHORIZED_SERIAL,
    CLAIM_BOUNDARY,
    RUN_ID,
    SHA256,
    TASK_ID,
    Phase5IntakeError,
    _canonical_string,
    _exact_keys,
    file_sha256,
    semantic_sha256,
    source_file_manifest,
    strict_json_bytes,
)


PROBE_RUN_SCHEMA_VERSION = "harmony-eval-phase5-clawmate-trace-probe-run-v1"
PROBE_RECEIPT_SCHEMA_VERSION = "harmony-eval-phase5-clawmate-trace-probe-receipt-v1"
PROBE_ID = "phase5-clawmate-trace-probe-v1"
PROBE_INTAKE_VERSION = "harmony-eval-phase5-clawmate-trace-probe-intake-v1"
RUNNER_PROFILE = "CLAWMATE_CANDIDATE"
PENDING_GROUND_TRUTH = "PENDING_SINGLE_OPERATOR_REVIEW"
MANIFEST_FILE = "phase5_clawmate_trace_probe_manifest.json"
GIT_SHA1 = re.compile(r"^[0-9a-f]{40}$")


def _validate_url(value: Any, context: str) -> str:
    text = _canonical_string(value, context)
    if not (text.startswith("https://github.com/") or text.startswith("https://www.modelscope.cn/")):
        raise Phase5IntakeError(f"{context} must be a checked public source URL")
    return text


def _optional_git_commit(value: Any, context: str) -> str:
    text = _canonical_string(value, context)
    if text != "UNKNOWN_PENDING_OPERATOR_CAPTURE" and not GIT_SHA1.fullmatch(text):
        raise Phase5IntakeError(f"{context} must be a 40-char Git commit or UNKNOWN_PENDING_OPERATOR_CAPTURE")
    return text


def validate_probe_manifest(value: Mapping[str, Any]) -> Mapping[str, Any]:
    _exact_keys(
        value,
        {
            "schema_version",
            "probe_id",
            "run_id",
            "runner_profile",
            "claim_boundary",
            "publication_eligible",
            "verifier_allowed_before_gt",
            "ground_truth_status",
            "collection_status",
            "device",
            "clawmate",
            "task",
            "privacy",
            "raw_export",
        },
        "ClawMate trace probe manifest",
    )
    if value["schema_version"] != PROBE_RUN_SCHEMA_VERSION:
        raise Phase5IntakeError("unsupported ClawMate probe manifest schema")
    if value["probe_id"] != PROBE_ID or value["runner_profile"] != RUNNER_PROFILE:
        raise Phase5IntakeError("ClawMate probe identity drift")
    if not isinstance(value["run_id"], str) or not RUN_ID.fullmatch(value["run_id"]):
        raise Phase5IntakeError("run_id must match p5r-[0-9a-f]{16}")
    if value["claim_boundary"] != CLAIM_BOUNDARY:
        raise Phase5IntakeError("claim boundary drift")
    if value["publication_eligible"] is not False or value["verifier_allowed_before_gt"] is not False:
        raise Phase5IntakeError("probe cannot claim publication eligibility or allow verifier before GT")
    if value["ground_truth_status"] != PENDING_GROUND_TRUTH:
        raise Phase5IntakeError("probe must remain pending single-operator GT")
    if value["collection_status"] not in {"RUN_COMPLETE", "RUN_FAILED", "RUN_ABORTED", "RUN_IN_PROGRESS"}:
        raise Phase5IntakeError("unsupported ClawMate collection_status")

    device = value["device"]
    if not isinstance(device, Mapping):
        raise Phase5IntakeError("device must be an object")
    _exact_keys(device, {"serial", "model", "device_type", "os_version"}, "device")
    if (
        device["serial"] != AUTHORIZED_SERIAL
        or device["model"] != AUTHORIZED_MODEL
        or device["device_type"] != AUTHORIZED_DEVICE_TYPE
    ):
        raise Phase5IntakeError("authorized device identity drift")
    _canonical_string(device["os_version"], "device.os_version")

    clawmate = value["clawmate"]
    if not isinstance(clawmate, Mapping):
        raise Phase5IntakeError("clawmate must be an object")
    _exact_keys(
        clawmate,
        {
            "repo_url",
            "commit",
            "desktop_version",
            "harmony_app_repo_url",
            "harmony_app_submodule_commit",
            "mobiinfer_repo_url",
            "mobiinfer_commit",
            "model_source_url",
            "model_id",
            "inference_backend",
        },
        "clawmate",
    )
    _validate_url(clawmate["repo_url"], "clawmate.repo_url")
    _optional_git_commit(clawmate["commit"], "clawmate.commit")
    _canonical_string(clawmate["desktop_version"], "clawmate.desktop_version")
    _validate_url(clawmate["harmony_app_repo_url"], "clawmate.harmony_app_repo_url")
    _optional_git_commit(clawmate["harmony_app_submodule_commit"], "clawmate.harmony_app_submodule_commit")
    _validate_url(clawmate["mobiinfer_repo_url"], "clawmate.mobiinfer_repo_url")
    _optional_git_commit(clawmate["mobiinfer_commit"], "clawmate.mobiinfer_commit")
    _validate_url(clawmate["model_source_url"], "clawmate.model_source_url")
    _canonical_string(clawmate["model_id"], "clawmate.model_id")
    if clawmate["inference_backend"] not in {"MOBIINFER_ON_DEVICE", "CLAWMATE_DESKTOP_FALLBACK", "UNKNOWN_PENDING_OPERATOR_CAPTURE"}:
        raise Phase5IntakeError("unsupported ClawMate inference backend")

    task = value["task"]
    if not isinstance(task, Mapping):
        raise Phase5IntakeError("task must be an object")
    _exact_keys(task, {"task_id", "task_text", "initial_app", "target_app"}, "task")
    if not isinstance(task["task_id"], str) or not TASK_ID.fullmatch(task["task_id"]):
        raise Phase5IntakeError("invalid task_id")
    _canonical_string(task["task_text"], "task.task_text")
    _canonical_string(task["initial_app"], "task.initial_app")
    _canonical_string(task["target_app"], "task.target_app")
    if task["initial_app"] == task["target_app"]:
        raise Phase5IntakeError("ClawMate probe task must cross Apps")

    privacy = value["privacy"]
    if not isinstance(privacy, Mapping):
        raise Phase5IntakeError("privacy must be an object")
    _exact_keys(
        privacy,
        {
            "fresh_profile_required",
            "personal_data_export_forbidden",
            "logs_may_contain_private_data",
            "operator_redaction_required_before_sharing",
        },
        "privacy",
    )
    if (
        privacy["fresh_profile_required"] is not True
        or privacy["personal_data_export_forbidden"] is not True
        or privacy["logs_may_contain_private_data"] is not True
        or privacy["operator_redaction_required_before_sharing"] is not True
    ):
        raise Phase5IntakeError("privacy boundary drift")

    raw_export = value["raw_export"]
    if not isinstance(raw_export, Mapping):
        raise Phase5IntakeError("raw_export must be an object")
    _exact_keys(raw_export, {"source_kind", "operator_notes"}, "raw_export")
    if raw_export["source_kind"] not in {"CLAWMATE_DESKTOP_OR_APP_EXPORT", "CLAWMATE_LOGS_PLUS_OPERATOR_SCREEN_CAPTURE"}:
        raise Phase5IntakeError("unsupported raw_export source kind")
    _canonical_string(raw_export["operator_notes"], "raw_export.operator_notes")
    return value


def load_probe_manifest(export_dir: Path) -> Mapping[str, Any]:
    path = export_dir / MANIFEST_FILE
    return validate_probe_manifest(
        strict_json_bytes(path.read_bytes(), context="ClawMate trace probe manifest")
    )


def _json_action_count(path: Path) -> Optional[tuple[int, list[str]]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    actions = value.get("actions") if isinstance(value, Mapping) else value
    if not isinstance(actions, list):
        return None
    types: list[str] = []
    for item in actions:
        if isinstance(item, Mapping):
            action = item.get("type") or item.get("action") or item.get("name")
            if isinstance(action, str) and action.strip():
                types.append(action.strip())
    return len(actions), types


def _capability(files: list[dict[str, Any]], export_dir: Path) -> Mapping[str, Any]:
    screenshot_refs: list[str] = []
    hierarchy_refs: list[str] = []
    action_refs: list[str] = []
    log_refs: list[str] = []
    action_count = 0
    action_types: list[str] = []
    for row in files:
        ref = row["relative_ref"]
        lower = ref.lower()
        name = Path(ref).name.lower()
        if lower.endswith((".jpg", ".jpeg", ".png")):
            screenshot_refs.append(ref)
        if lower.endswith(".xml") or (
            lower.endswith(".json")
            and any(token in name for token in ("hierarchy", "layout", "uitree", "ui_tree", "dump"))
        ):
            hierarchy_refs.append(ref)
        if lower.endswith((".log", ".txt")):
            log_refs.append(ref)
        if name in {"actions.json", "action_trace.json", "trajectory.json", "trace_actions.json"}:
            action_refs.append(ref)
            parsed = _json_action_count(export_dir / ref)
            if parsed is not None:
                count, types = parsed
                action_count = max(action_count, count)
                action_types.extend(types)
    unique_action_types = sorted(set(action_types))
    return {
        "action_files": action_refs,
        "action_count": action_count,
        "action_types": unique_action_types,
        "screenshot_files": screenshot_refs,
        "hierarchy_files": hierarchy_refs,
        "log_files": log_refs,
        "has_actions": bool(action_refs) and action_count > 0,
        "has_screenshots": bool(screenshot_refs),
        "has_ui_hierarchy": bool(hierarchy_refs),
        "has_logs": bool(log_refs),
        "canonical_adapter_ready": bool(action_refs) and action_count > 0 and bool(screenshot_refs) and bool(hierarchy_refs),
    }


def build_probe_receipt(*, export_dir: Path) -> Mapping[str, Any]:
    root = export_dir.resolve(strict=True)
    manifest = load_probe_manifest(root)
    files = list(source_file_manifest(root))
    capability = _capability(files, root)
    errors: list[str] = []
    if manifest["collection_status"] != "RUN_COMPLETE":
        errors.append("COLLECTION_NOT_COMPLETE")
    if not capability["canonical_adapter_ready"]:
        errors.append("CANONICAL_TRACE_EVIDENCE_INSUFFICIENT")
    status = (
        "REJECTED_COLLECTION_INCOMPLETE"
        if manifest["collection_status"] != "RUN_COMPLETE"
        else "ACCEPTED_FOR_TRACE_CAPABILITY_REVIEW"
    )
    return {
        "schema_version": PROBE_RECEIPT_SCHEMA_VERSION,
        "intake_version": PROBE_INTAKE_VERSION,
        "intake_source_sha256": file_sha256(Path(__file__)),
        "status": status,
        "publication_eligible": False,
        "probe_id": PROBE_ID,
        "runner_profile": RUNNER_PROFILE,
        "run_id": manifest["run_id"],
        "task_id": manifest["task"]["task_id"],
        "probe_manifest_sha256": semantic_sha256(manifest),
        "collection_status": manifest["collection_status"],
        "ground_truth_status": PENDING_GROUND_TRUTH,
        "claim_boundary": CLAIM_BOUNDARY,
        "source_files": files,
        "source_tree_sha256": semantic_sha256(files),
        "capability_profile": capability,
        "verifier_allowed_before_gt": False,
        "canonical_adapter_ready": capability["canonical_adapter_ready"],
        "diagnostic_boundary": {
            "reasoning_copied_to_receipt": False,
            "runner_self_report_copied_to_receipt": False,
            "logs_used_as_success_authority": False,
        },
        "errors": errors,
    }


def validate_probe_receipt(value: Mapping[str, Any]) -> Mapping[str, Any]:
    _exact_keys(
        value,
        {
            "schema_version",
            "intake_version",
            "intake_source_sha256",
            "status",
            "publication_eligible",
            "probe_id",
            "runner_profile",
            "run_id",
            "task_id",
            "probe_manifest_sha256",
            "collection_status",
            "ground_truth_status",
            "claim_boundary",
            "source_files",
            "source_tree_sha256",
            "capability_profile",
            "verifier_allowed_before_gt",
            "canonical_adapter_ready",
            "diagnostic_boundary",
            "errors",
        },
        "ClawMate probe receipt",
    )
    if value["schema_version"] != PROBE_RECEIPT_SCHEMA_VERSION or value["intake_version"] != PROBE_INTAKE_VERSION:
        raise Phase5IntakeError("ClawMate probe receipt schema/version drift")
    if value["status"] not in {"ACCEPTED_FOR_TRACE_CAPABILITY_REVIEW", "REJECTED_COLLECTION_INCOMPLETE"}:
        raise Phase5IntakeError("unsupported ClawMate probe receipt status")
    if value["publication_eligible"] is not False or value["verifier_allowed_before_gt"] is not False:
        raise Phase5IntakeError("ClawMate probe receipt cannot allow publication/verifier")
    if value["probe_id"] != PROBE_ID or value["runner_profile"] != RUNNER_PROFILE:
        raise Phase5IntakeError("ClawMate probe receipt identity drift")
    if not isinstance(value["run_id"], str) or not RUN_ID.fullmatch(value["run_id"]):
        raise Phase5IntakeError("invalid run_id")
    if not isinstance(value["task_id"], str) or not TASK_ID.fullmatch(value["task_id"]):
        raise Phase5IntakeError("invalid task_id")
    for key in ("intake_source_sha256", "probe_manifest_sha256", "source_tree_sha256"):
        if not isinstance(value[key], str) or not SHA256.fullmatch(value[key]):
            raise Phase5IntakeError(f"invalid {key}")
    if value["ground_truth_status"] != PENDING_GROUND_TRUTH or value["claim_boundary"] != CLAIM_BOUNDARY:
        raise Phase5IntakeError("ClawMate receipt GT/claim boundary drift")
    if not isinstance(value["source_files"], list) or not value["source_files"]:
        raise Phase5IntakeError("source_files must be non-empty")
    capability = value["capability_profile"]
    if not isinstance(capability, Mapping):
        raise Phase5IntakeError("capability_profile must be an object")
    _exact_keys(
        capability,
        {
            "action_files",
            "action_count",
            "action_types",
            "screenshot_files",
            "hierarchy_files",
            "log_files",
            "has_actions",
            "has_screenshots",
            "has_ui_hierarchy",
            "has_logs",
            "canonical_adapter_ready",
        },
        "capability_profile",
    )
    if value["canonical_adapter_ready"] is not capability["canonical_adapter_ready"]:
        raise Phase5IntakeError("canonical adapter readiness mismatch")
    diagnostics = value["diagnostic_boundary"]
    if not isinstance(diagnostics, Mapping):
        raise Phase5IntakeError("diagnostic_boundary must be an object")
    _exact_keys(
        diagnostics,
        {
            "reasoning_copied_to_receipt",
            "runner_self_report_copied_to_receipt",
            "logs_used_as_success_authority",
        },
        "diagnostic_boundary",
    )
    if any(diagnostics.values()):
        raise Phase5IntakeError("diagnostic content leaked into authority")
    return value


def verify_probe_receipt(receipt: Mapping[str, Any], export_dir: Path) -> None:
    validate_probe_receipt(receipt)
    root = export_dir.resolve(strict=True)
    manifest = load_probe_manifest(root)
    files = list(source_file_manifest(root))
    if receipt["probe_manifest_sha256"] != semantic_sha256(manifest):
        raise Phase5IntakeError("probe manifest hash drift")
    if receipt["source_files"] != files:
        raise Phase5IntakeError("source file hash drift")
    if receipt["source_tree_sha256"] != semantic_sha256(files):
        raise Phase5IntakeError("source tree hash drift")


def build_probe_manifest_template(
    *,
    run_id: str,
    task_id: str,
    task_text: str,
    initial_app: str,
    target_app: str,
    os_version: str,
    clawmate_commit: str,
    harmony_app_submodule_commit: str,
    mobiinfer_commit: str,
    desktop_version: str,
    inference_backend: str,
) -> Mapping[str, Any]:
    return validate_probe_manifest(
        {
            "schema_version": PROBE_RUN_SCHEMA_VERSION,
            "probe_id": PROBE_ID,
            "run_id": run_id,
            "runner_profile": RUNNER_PROFILE,
            "claim_boundary": CLAIM_BOUNDARY,
            "publication_eligible": False,
            "verifier_allowed_before_gt": False,
            "ground_truth_status": PENDING_GROUND_TRUTH,
            "collection_status": "RUN_IN_PROGRESS",
            "device": {
                "serial": AUTHORIZED_SERIAL,
                "model": AUTHORIZED_MODEL,
                "device_type": AUTHORIZED_DEVICE_TYPE,
                "os_version": os_version,
            },
            "clawmate": {
                "repo_url": "https://github.com/IPADS-SAI/ClawMate",
                "commit": clawmate_commit,
                "desktop_version": desktop_version,
                "harmony_app_repo_url": "https://github.com/doulujiyao12/mobiinfra-oh",
                "harmony_app_submodule_commit": harmony_app_submodule_commit,
                "mobiinfer_repo_url": "https://github.com/doulujiyao12/mobiinfer",
                "mobiinfer_commit": mobiinfer_commit,
                "model_source_url": "https://www.modelscope.cn/models/fengerhu1/MobiMind-1.5-2B-W8A8-0717",
                "model_id": "MobiMind-1.5-2B-W8A8-0717",
                "inference_backend": inference_backend,
            },
            "task": {
                "task_id": task_id,
                "task_text": task_text,
                "initial_app": initial_app,
                "target_app": target_app,
            },
            "privacy": {
                "fresh_profile_required": True,
                "personal_data_export_forbidden": True,
                "logs_may_contain_private_data": True,
                "operator_redaction_required_before_sharing": True,
            },
            "raw_export": {
                "source_kind": "CLAWMATE_DESKTOP_OR_APP_EXPORT",
                "operator_notes": "Replace collection_status with RUN_COMPLETE only after copying raw ClawMate screenshots/actions/UI hierarchy/logs into this directory.",
            },
        }
    )


__all__ = [name for name in globals() if not name.startswith("_")]
