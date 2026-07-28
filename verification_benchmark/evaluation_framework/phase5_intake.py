"""Strict, hash-bound Phase 5 Harmony black-box collection and intake facts."""

from __future__ import annotations

import hashlib
import json
import math
import re
import stat
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Optional

from PIL import Image


EXPERIMENT_SCHEMA_VERSION = "harmony-eval-phase5-blackbox-pilot-v1"
COLLECTION_SCHEMA_VERSION = "harmony-eval-phase5-collection-run-v1"
INTAKE_RECEIPT_SCHEMA_VERSION = "harmony-eval-phase5-intake-receipt-v1"
COLLECTOR_VERSION = "harmony-eval-phase5-collector-v1"
INTAKE_VERSION = "harmony-eval-phase5-intake-v1"
PENDING_GROUND_TRUTH = "PENDING_BLIND_REVIEW"
CLAIM_BOUNDARY = "S0_S1_BLACK_BOX_ONLY_NO_BACKEND_SIDE_EFFECT_CLAIM"
AUTHORIZED_SERIAL = "5ZU0226122004500"
AUTHORIZED_MODEL = "DEL-AL10"
AUTHORIZED_DEVICE_TYPE = "Harmony"
AUTHORIZED_PROVIDER_BASE_URL = "https://api.horizon1123.top/v1"
AUTHORIZED_PROVIDER_MODEL = "gpt-5.4-mini"
AUTHORIZED_TRANSPORT = "raw_http"
AUTHORIZED_KEY_ENV = "MOBIAGENT_API_KEY"
RUN_ID = re.compile(r"^p5r-[0-9a-f]{16}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
TASK_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{2,79}$")
PHASE4_FORBIDDEN_TASK_FRAGMENTS = (
    "机械键盘",
    "保温杯",
    "人体工学垂直鼠标",
    "笔记本电脑支架",
    "无线降噪耳机",
)


class Phase5IntakeError(ValueError):
    pass


def strict_json_value_bytes(data: bytes, *, context: str) -> Any:
    if not isinstance(data, bytes):
        raise Phase5IntakeError(f"{context} must be bytes")

    def pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise Phase5IntakeError(f"{context} duplicate JSON key: {key}")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise Phase5IntakeError(f"{context} contains non-finite number: {value}")

    try:
        value = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=pairs_hook,
            parse_constant=reject_constant,
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise Phase5IntakeError(f"{context} is invalid UTF-8 JSON: {exc}") from exc
    return value


def strict_json_bytes(data: bytes, *, context: str) -> Mapping[str, Any]:
    value = strict_json_value_bytes(data, context=context)
    if not isinstance(value, Mapping):
        raise Phase5IntakeError(f"{context} root must be an object")
    return value


def canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise Phase5IntakeError(f"value is not canonical JSON: {exc}") from exc


def semantic_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _exact_keys(value: Mapping[str, Any], expected: set[str], context: str) -> None:
    actual = set(value)
    if actual != expected:
        raise Phase5IntakeError(
            f"{context} keys mismatch; missing={sorted(expected-actual)}, "
            f"unexpected={sorted(actual-expected)}"
        )


def _canonical_string(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise Phase5IntakeError(f"{context} must be a canonical non-empty string")
    return value


def _positive_int(value: Any, context: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise Phase5IntakeError(f"{context} must be a positive integer")
    return value


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError as exc:
        raise Phase5IntakeError(f"cannot stat source path {path}: {exc}") from exc
    return path.is_symlink() or bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _safe_relative_ref(value: Any, context: str) -> str:
    text = _canonical_string(value, context)
    if "\\" in text:
        raise Phase5IntakeError(f"{context} must use POSIX separators")
    ref = PurePosixPath(text)
    if ref.is_absolute() or ".." in ref.parts:
        raise Phase5IntakeError(f"{context} must not escape its root")
    return text


def resolve_contained(root: Path, relative_ref: str) -> Path:
    if _is_reparse_point(root):
        raise Phase5IntakeError(f"source root cannot be a reparse point/symlink: {root}")
    root_resolved = root.resolve(strict=True)
    candidate = root.joinpath(*PurePosixPath(relative_ref).parts)
    current = root_resolved
    for part in PurePosixPath(relative_ref).parts:
        current = current / part
        if current.exists() and _is_reparse_point(current):
            raise Phase5IntakeError(f"reparse point/symlink is forbidden: {relative_ref}")
    resolved = candidate.resolve(strict=True)
    if not resolved.is_relative_to(root_resolved):
        raise Phase5IntakeError(f"path escapes source root: {relative_ref}")
    return resolved


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
            "unseen_exclusions",
            "cohort",
            "agent",
            "collection_policy",
            "tasks",
        },
        "Phase 5 experiment manifest",
    )
    if value["schema_version"] != EXPERIMENT_SCHEMA_VERSION:
        raise Phase5IntakeError("unsupported Phase 5 experiment schema")
    if value["protocol_status"] != "DEVELOPMENT_PILOT_FROZEN_BEFORE_COLLECTION":
        raise Phase5IntakeError("Phase 5 protocol is not frozen before collection")
    if value["publication_eligible"] is not False:
        raise Phase5IntakeError("pilot manifest must not pre-claim publication eligibility")
    if value["claim_boundary"] != CLAIM_BOUNDARY:
        raise Phase5IntakeError("Phase 5 claim boundary drift")
    if value["oracle_database_dependency"] is not False:
        raise Phase5IntakeError("Phase 5 black-box manifest cannot depend on an Oracle database")
    if value["phase4_status"] != "FROZEN_MECHANISM_VALIDATION_ONLY":
        raise Phase5IntakeError("Phase 4 status is not frozen")
    exclusions = value["unseen_exclusions"]
    if not isinstance(exclusions, list) or sorted(exclusions) != sorted(PHASE4_FORBIDDEN_TASK_FRAGMENTS):
        raise Phase5IntakeError("Phase 5 unseen exclusion list drift")

    cohort = value["cohort"]
    if not isinstance(cohort, Mapping):
        raise Phase5IntakeError("cohort must be an object")
    _exact_keys(
        cohort,
        {"cohort_id", "device_serial", "device_model", "device_type", "posture", "resolution"},
        "cohort",
    )
    if (
        cohort["device_serial"] != AUTHORIZED_SERIAL
        or cohort["device_model"] != AUTHORIZED_MODEL
        or cohort["device_type"] != AUTHORIZED_DEVICE_TYPE
    ):
        raise Phase5IntakeError("authorized device identity drift")
    if cohort["posture"] != "FOLDED_OUTER_DISPLAY" or cohort["resolution"] != [1080, 2444]:
        raise Phase5IntakeError("pilot cohort posture/resolution drift")

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
        "max_steps": 15,
    }
    for key, expected in expected_agent.items():
        if agent[key] != expected:
            raise Phase5IntakeError(f"agent configuration drift at {key}")
    if not isinstance(agent["runner_module_sha256"], str) or not SHA256.fullmatch(agent["runner_module_sha256"]):
        raise Phase5IntakeError("runner_module_sha256 is invalid")

    policy = value["collection_policy"]
    if not isinstance(policy, Mapping):
        raise Phase5IntakeError("collection_policy must be an object")
    _exact_keys(
        policy,
        {
            "risk_tier",
            "allowed_actions",
            "forbidden_actions",
            "abort_conditions",
            "collection_order",
            "ground_truth_status",
            "guardrail_callbacks_allowed",
            "overwrite_allowed",
        },
        "collection_policy",
    )
    if policy["risk_tier"] != "LOW_RISK_READ_ONLY_SEARCH":
        raise Phase5IntakeError("collection risk tier drift")
    if policy["ground_truth_status"] != PENDING_GROUND_TRUTH:
        raise Phase5IntakeError("Ground Truth must remain pending during collection")
    if policy["guardrail_callbacks_allowed"] is not False or policy["overwrite_allowed"] is not False:
        raise Phase5IntakeError("Phase 5 collection must disable callbacks and overwrite")
    for key in ("allowed_actions", "forbidden_actions", "abort_conditions", "collection_order"):
        if not isinstance(policy[key], list) or not policy[key]:
            raise Phase5IntakeError(f"collection_policy {key} must be a non-empty array")
        for index, item in enumerate(policy[key]):
            _canonical_string(item, f"collection_policy.{key}[{index}]")

    tasks = value["tasks"]
    if not isinstance(tasks, list) or not 1 <= len(tasks) <= 20:
        raise Phase5IntakeError("Phase 5 pilot requires 1..20 frozen tasks")
    seen: set[str] = set()
    expected_order = []
    for ordinal, task in enumerate(tasks, 1):
        if not isinstance(task, Mapping):
            raise Phase5IntakeError("task must be an object")
        _exact_keys(
            task,
            {
                "task_id",
                "collection_ordinal",
                "app",
                "package",
                "task_family",
                "task_text",
                "requested_entity",
                "expected_observable_criteria",
                "allowed_actions",
                "forbidden_actions",
                "contract_source_route",
                "app_version_policy",
                "smoke_priority",
            },
            "task",
        )
        task_id = task["task_id"]
        if not isinstance(task_id, str) or not TASK_ID.fullmatch(task_id) or task_id in seen:
            raise Phase5IntakeError("task_id is invalid or duplicated")
        seen.add(task_id)
        if task["collection_ordinal"] != ordinal:
            raise Phase5IntakeError("task collection ordinals must be contiguous")
        expected_order.append(task_id)
        if task["app"] != "淘宝" or task["package"] != "com.taobao.taobao4hmos":
            raise Phase5IntakeError("pilot task App/package drift")
        if task["task_family"] != "search_results_read_only":
            raise Phase5IntakeError("pilot task family drift")
        text = _canonical_string(task["task_text"], "task_text")
        entity = _canonical_string(task["requested_entity"], "requested_entity")
        if entity not in text:
            raise Phase5IntakeError("task text/requested entity binding mismatch")
        if any(fragment in text for fragment in PHASE4_FORBIDDEN_TASK_FRAGMENTS):
            raise Phase5IntakeError("Phase 4 observed task leaked into Phase 5 unseen set")
        if not isinstance(task["expected_observable_criteria"], list) or not task["expected_observable_criteria"]:
            raise Phase5IntakeError("task expected criteria must be non-empty")
        for key in ("expected_observable_criteria", "allowed_actions", "forbidden_actions"):
            if not isinstance(task[key], list) or not task[key]:
                raise Phase5IntakeError(f"task {key} must be a non-empty array")
            for index, item in enumerate(task[key]):
                _canonical_string(item, f"task.{key}[{index}]")
        if task["contract_source_route"] not in {"registry", "template", "validated-jit"}:
            raise Phase5IntakeError("task contract source route is invalid")
        _canonical_string(task["app_version_policy"], "task.app_version_policy")
        if task["smoke_priority"] not in {"FIRST", "SECOND", "THIRD", "PILOT_LATER"}:
            raise Phase5IntakeError("task smoke priority is invalid")
    if policy["collection_order"] != expected_order:
        raise Phase5IntakeError("collection order does not match frozen task ordinals")
    return value


def load_experiment_manifest(path: Path) -> Mapping[str, Any]:
    return validate_experiment_manifest(
        strict_json_bytes(path.read_bytes(), context="Phase 5 experiment manifest")
    )


def find_task(manifest: Mapping[str, Any], task_id: str) -> Mapping[str, Any]:
    matches = [task for task in manifest["tasks"] if task["task_id"] == task_id]
    if len(matches) != 1:
        raise Phase5IntakeError(f"task_id must resolve exactly once: {task_id}")
    return matches[0]


def validate_collection_run_manifest(value: Mapping[str, Any]) -> Mapping[str, Any]:
    _exact_keys(
        value,
        {
            "schema_version",
            "collector_version",
            "experiment_id",
            "experiment_manifest_sha256",
            "run_id",
            "task_id",
            "task_text_sha256",
            "app",
            "package",
            "trace_relpath",
            "device",
            "agent",
            "collection_status",
            "attempt_ordinal",
            "oracle_database_dependency",
            "ground_truth_status",
            "guardrail_callback_enabled",
            "start_state_guard_enabled",
            "runner_exit_code",
        },
        "Phase 5 collection run manifest",
    )
    if value["schema_version"] != COLLECTION_SCHEMA_VERSION or value["collector_version"] != COLLECTOR_VERSION:
        raise Phase5IntakeError("collection run schema/version drift")
    if not isinstance(value["run_id"], str) or not RUN_ID.fullmatch(value["run_id"]):
        raise Phase5IntakeError("run_id must be an opaque p5r- identifier")
    if not isinstance(value["experiment_manifest_sha256"], str) or not SHA256.fullmatch(value["experiment_manifest_sha256"]):
        raise Phase5IntakeError("experiment manifest SHA is invalid")
    if not isinstance(value["task_text_sha256"], str) or not SHA256.fullmatch(value["task_text_sha256"]):
        raise Phase5IntakeError("task text SHA is invalid")
    _safe_relative_ref(value["trace_relpath"], "trace_relpath")
    if value["collection_status"] not in {"RUN_COMPLETE", "RUN_ABORTED", "RUN_FAILED", "RUN_IN_PROGRESS"}:
        raise Phase5IntakeError("collection_status is invalid")
    _positive_int(value["attempt_ordinal"], "attempt_ordinal")
    if value["oracle_database_dependency"] is not False or value["ground_truth_status"] != PENDING_GROUND_TRUTH:
        raise Phase5IntakeError("collection run injected Oracle/Ground Truth")
    if value["guardrail_callback_enabled"] is not False or value["start_state_guard_enabled"] is not False:
        raise Phase5IntakeError("Phase 4 callback accidentally enabled")
    if value["runner_exit_code"] is not None and (
        not isinstance(value["runner_exit_code"], int) or isinstance(value["runner_exit_code"], bool)
    ):
        raise Phase5IntakeError("runner_exit_code must be integer or null")
    for name, expected in (
        ("app", "淘宝"),
        ("package", "com.taobao.taobao4hmos"),
    ):
        if value[name] != expected:
            raise Phase5IntakeError(f"collection run {name} drift")
    device = value["device"]
    agent = value["agent"]
    if not isinstance(device, Mapping) or not isinstance(agent, Mapping):
        raise Phase5IntakeError("collection device/agent must be objects")
    _exact_keys(device, {"serial", "model", "device_type", "posture", "resolution", "os_version", "app_version"}, "collection device")
    _exact_keys(agent, {"provider_base_url", "model", "transport", "runner_module_sha256", "collector_source_sha256", "evaluation_git_head", "runner_repository_git_head"}, "collection agent")
    if device["serial"] != AUTHORIZED_SERIAL or device["model"] != AUTHORIZED_MODEL or device["device_type"] != AUTHORIZED_DEVICE_TYPE:
        raise Phase5IntakeError("collection device identity drift")
    if device["posture"] != "FOLDED_OUTER_DISPLAY" or device["resolution"] != [1080, 2444]:
        raise Phase5IntakeError("collection posture/resolution drift")
    _canonical_string(device["os_version"], "os_version")
    _canonical_string(device["app_version"], "app_version")
    if agent["provider_base_url"] != AUTHORIZED_PROVIDER_BASE_URL or agent["model"] != AUTHORIZED_PROVIDER_MODEL or agent["transport"] != AUTHORIZED_TRANSPORT:
        raise Phase5IntakeError("collection provider/model/transport drift")
    for key in ("runner_module_sha256", "collector_source_sha256"):
        if not isinstance(agent[key], str) or not SHA256.fullmatch(agent[key]):
            raise Phase5IntakeError(f"collection agent {key} is invalid")
    _canonical_string(agent["evaluation_git_head"], "collection evaluation_git_head")
    _canonical_string(agent["runner_repository_git_head"], "collection runner_repository_git_head")
    return value


def load_collection_run_manifest(run_dir: Path) -> Mapping[str, Any]:
    path = resolve_contained(run_dir, "phase5_collection_run_manifest.json")
    return validate_collection_run_manifest(
        strict_json_bytes(path.read_bytes(), context="Phase 5 collection run manifest")
    )


_ACTION_TOP_LEVEL_KEYS = {
    "app_name",
    "task_type",
    "old_task_description",
    "task_description",
    "execution_timestamp",
    "decider_protocol",
    "stop_reason",
    "action_count",
    "actions",
}
_ACTION_ROW_KEYS = {
    "type",
    "status",
    "message",
    "text",
    "app_name",
    "direction",
    "position_x",
    "position_y",
    "release_position_x",
    "release_position_y",
    "bounds",
    "raw_model_bbox",
    "converted_bounds",
    "click_point_before_xml_alignment",
    "click_point",
    "screenshot_size",
    "click_coordinate_size",
    "xml_hit_test_result",
    "target_element",
    "focus_wait_seconds",
    "seconds",
    "press_position_x",
    "press_position_y",
    "question",
    "response",
    "tag",
    "handled",
    "reason",
    "action_index",
}


@dataclass(frozen=True)
class TraceAudit:
    action_count: int
    action_types: tuple[str, ...]
    screenshot_frames: tuple[int, ...]
    hierarchy_xml_frames: tuple[int, ...]
    hierarchy_raw_json_frames: tuple[int, ...]
    timestamp_capability: str


def _numeric_files(trace_dir: Path, suffix: str) -> dict[int, Path]:
    result: dict[int, Path] = {}
    for path in trace_dir.iterdir():
        if _is_reparse_point(path):
            raise Phase5IntakeError(f"reparse point/symlink is forbidden: {path.name}")
        if not path.is_file() or path.suffix.lower() != suffix:
            continue
        try:
            index = int(path.stem)
        except ValueError:
            continue
        if index in result:
            raise Phase5IntakeError(f"duplicate numeric artifact index: {index}{suffix}")
        result[index] = path
    return result


def audit_trace(trace_dir: Path, task: Mapping[str, Any]) -> TraceAudit:
    actions_path = trace_dir / "actions.json"
    react_path = trace_dir / "react.json"
    if not actions_path.is_file() or not react_path.is_file():
        raise Phase5IntakeError("trace must contain actions.json and react.json")
    actions = strict_json_bytes(actions_path.read_bytes(), context="actions.json")
    _exact_keys(actions, _ACTION_TOP_LEVEL_KEYS, "actions.json")
    if actions["task_description"] != task["task_text"]:
        raise Phase5IntakeError("actions task description drift")
    if actions["app_name"] != task["app"] or actions["decider_protocol"] != "qwen_json":
        raise Phase5IntakeError("actions app/decider protocol drift")
    rows = actions["actions"]
    if not isinstance(rows, list):
        raise Phase5IntakeError("actions.json actions must be an array")
    count = actions["action_count"]
    if not isinstance(count, int) or isinstance(count, bool) or count != len(rows) or count <= 0:
        raise Phase5IntakeError("actions action_count mismatch or empty trace")
    types: list[str] = []
    for expected, row in enumerate(rows, 1):
        if not isinstance(row, Mapping):
            raise Phase5IntakeError("action row must be an object")
        unexpected = set(row) - _ACTION_ROW_KEYS
        if unexpected:
            raise Phase5IntakeError(f"action row unknown fields: {sorted(unexpected)}")
        if any(str(key).startswith("guardrail") for key in row):
            raise Phase5IntakeError("Guardrail field leaked into Phase 5 action trace")
        if row.get("action_index") != expected:
            raise Phase5IntakeError("action_index sequence must be contiguous from one")
        action_type = _canonical_string(row.get("type"), "action type")
        if action_type == "done_candidate_intercepted":
            raise Phase5IntakeError("Phase 4 Guardrail callback action leaked into Phase 5")
        types.append(action_type)
    react = strict_json_value_bytes(react_path.read_bytes(), context="react.json")
    if not isinstance(react, list) or len(react) != count:
        raise Phase5IntakeError("react.json must be an array aligned to actions")
    jpg = _numeric_files(trace_dir, ".jpg")
    xml = _numeric_files(trace_dir, ".xml")
    raw_json = _numeric_files(trace_dir, ".json")
    expected_frames = set(range(1, count + 1))
    if set(jpg) != expected_frames or set(xml) != expected_frames:
        raise Phase5IntakeError("screenshot/XML frame sequence must exactly match actions")
    if raw_json and set(raw_json) != expected_frames:
        raise Phase5IntakeError("raw hierarchy JSON frames must be absent or complete")
    for index in sorted(expected_frames):
        try:
            with Image.open(jpg[index]) as image:
                image.verify()
        except (OSError, ValueError) as exc:
            raise Phase5IntakeError(f"unreadable screenshot frame {index}: {exc}") from exc
        try:
            if not xml[index].read_bytes().strip():
                raise Phase5IntakeError(f"empty hierarchy XML frame {index}")
            ET.parse(xml[index])
        except (OSError, ET.ParseError) as exc:
            raise Phase5IntakeError(f"unreadable hierarchy XML frame {index}: {exc}") from exc
        if index in raw_json:
            strict_json_value_bytes(
                raw_json[index].read_bytes(), context=f"raw hierarchy {index}.json"
            )
    timestamp = actions["execution_timestamp"]
    if not isinstance(timestamp, Mapping):
        raise Phase5IntakeError("execution_timestamp must be an object")
    _exact_keys(timestamp, {"date", "weekday", "time"}, "execution_timestamp")
    for key in ("date", "weekday", "time"):
        _canonical_string(timestamp[key], f"execution_timestamp.{key}")
    return TraceAudit(
        count,
        tuple(types),
        tuple(sorted(jpg)),
        tuple(sorted(xml)),
        tuple(sorted(raw_json)),
        "RUN_LEVEL_WALL_CLOCK_ONLY_NO_FRAME_TIMESTAMPS",
    )


def source_file_manifest(run_dir: Path) -> tuple[dict[str, Any], ...]:
    if _is_reparse_point(run_dir):
        raise Phase5IntakeError(f"source root cannot be a reparse point/symlink: {run_dir}")
    root = run_dir.resolve(strict=True)
    rows = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        if _is_reparse_point(path):
            raise Phase5IntakeError(f"reparse point/symlink is forbidden: {path}")
        if not path.is_file():
            continue
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(root):
            raise Phase5IntakeError("source file escaped run root")
        rows.append(
            {
                "relative_ref": path.relative_to(root).as_posix(),
                "byte_size": path.stat().st_size,
                "sha256": file_sha256(path),
            }
        )
    if not rows:
        raise Phase5IntakeError("raw run contains no files")
    return tuple(rows)


def build_intake_receipt(
    *,
    experiment_manifest: Mapping[str, Any],
    run_dir: Path,
) -> Mapping[str, Any]:
    validate_experiment_manifest(experiment_manifest)
    run = load_collection_run_manifest(run_dir)
    if run["experiment_id"] != experiment_manifest["experiment_id"]:
        raise Phase5IntakeError("run/experiment identity drift")
    manifest_sha = semantic_sha256(experiment_manifest)
    if run["experiment_manifest_sha256"] != manifest_sha:
        raise Phase5IntakeError("run/experiment manifest hash drift")
    task = find_task(experiment_manifest, run["task_id"])
    if run["task_text_sha256"] != hashlib.sha256(task["task_text"].encode("utf-8")).hexdigest():
        raise Phase5IntakeError("run/task text hash drift")
    if run["app"] != task["app"] or run["package"] != task["package"]:
        raise Phase5IntakeError("run/task App binding drift")
    cohort = experiment_manifest["cohort"]
    agent = experiment_manifest["agent"]
    if (
        run["device"]["serial"] != cohort["device_serial"]
        or run["device"]["model"] != cohort["device_model"]
        or run["device"]["posture"] != cohort["posture"]
        or run["device"]["resolution"] != cohort["resolution"]
    ):
        raise Phase5IntakeError("run/cohort device drift")
    if (
        run["agent"]["provider_base_url"] != agent["provider_base_url"]
        or run["agent"]["model"] != agent["model"]
        or run["agent"]["transport"] != agent["transport"]
        or run["agent"]["runner_module_sha256"] != agent["runner_module_sha256"]
    ):
        raise Phase5IntakeError("run/agent provenance drift")
    trace_dir = resolve_contained(run_dir, run["trace_relpath"])
    if not trace_dir.is_dir():
        raise Phase5IntakeError("trace_relpath is not a directory")
    files = source_file_manifest(run_dir)
    trace_audit: Optional[TraceAudit] = None
    eligible = run["collection_status"] == "RUN_COMPLETE"
    errors: list[str] = []
    if eligible:
        trace_audit = audit_trace(trace_dir, task)
    else:
        errors.append("COLLECTION_NOT_COMPLETE")
    receipt = {
        "schema_version": INTAKE_RECEIPT_SCHEMA_VERSION,
        "intake_version": INTAKE_VERSION,
        "intake_source_sha256": file_sha256(Path(__file__)),
        "status": "ACCEPTED_PENDING_BLIND_REVIEW" if eligible else "REJECTED_COLLECTION_INCOMPLETE",
        "publication_eligible": False,
        "experiment_id": experiment_manifest["experiment_id"],
        "experiment_manifest_sha256": manifest_sha,
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
                "hierarchy_raw_json_frames": list(trace_audit.hierarchy_raw_json_frames),
                "action_count": trace_audit.action_count,
                "action_types": list(trace_audit.action_types),
                "timestamps": trace_audit.timestamp_capability,
                "integrity": "VALID_WITH_TIMESTAMP_DEGRADATION",
            }
            if trace_audit is not None
            else None
        ),
        "diagnostic_evidence": {
            "react_present_and_hashed": any(row["relative_ref"].endswith("/react.json") for row in files),
            "reasoning_copied_to_receipt": False,
            "runner_self_report_copied_to_receipt": False,
            "old_verifier_verdict_copied_to_receipt": False,
        },
        "errors": errors,
    }
    return receipt


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
            "diagnostic_evidence",
            "errors",
        },
        "Phase 5 intake receipt",
    )
    if value["schema_version"] != INTAKE_RECEIPT_SCHEMA_VERSION or value["intake_version"] != INTAKE_VERSION:
        raise Phase5IntakeError("intake receipt schema/version drift")
    if not isinstance(value["intake_source_sha256"], str) or not SHA256.fullmatch(value["intake_source_sha256"]):
        raise Phase5IntakeError("intake source hash is invalid")
    if value["publication_eligible"] is not False:
        raise Phase5IntakeError("intake receipt cannot claim publication eligibility")
    if value["ground_truth_status"] != PENDING_GROUND_TRUTH or value["oracle_database_dependency"] is not False:
        raise Phase5IntakeError("intake receipt injected Oracle/Ground Truth")
    if value["claim_boundary"] != CLAIM_BOUNDARY:
        raise Phase5IntakeError("intake receipt claim boundary drift")
    if not isinstance(value["source_files"], list) or not value["source_files"]:
        raise Phase5IntakeError("intake receipt source_files must be non-empty")
    if not isinstance(value["source_tree_sha256"], str) or not SHA256.fullmatch(value["source_tree_sha256"]):
        raise Phase5IntakeError("intake receipt source tree hash is invalid")
    diagnostics = value["diagnostic_evidence"]
    if not isinstance(diagnostics, Mapping):
        raise Phase5IntakeError("diagnostic_evidence must be an object")
    _exact_keys(
        diagnostics,
        {
            "react_present_and_hashed",
            "reasoning_copied_to_receipt",
            "runner_self_report_copied_to_receipt",
            "old_verifier_verdict_copied_to_receipt",
        },
        "diagnostic_evidence",
    )
    if any(
        diagnostics[key] is not False
        for key in (
            "reasoning_copied_to_receipt",
            "runner_self_report_copied_to_receipt",
            "old_verifier_verdict_copied_to_receipt",
        )
    ):
        raise Phase5IntakeError("diagnostic or self-report content leaked into receipt")
    return value


def verify_intake_receipt(receipt: Mapping[str, Any], run_dir: Path) -> None:
    validate_intake_receipt(receipt)
    expected_files = source_file_manifest(run_dir)
    if receipt.get("source_files") != list(expected_files):
        raise Phase5IntakeError("intake receipt source file hash drift")
    if receipt.get("source_tree_sha256") != semantic_sha256(list(expected_files)):
        raise Phase5IntakeError("intake receipt source tree hash drift")


def write_new_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(canonical_bytes(value) + b"\n")


__all__ = [name for name in globals() if not name.startswith("_")]
