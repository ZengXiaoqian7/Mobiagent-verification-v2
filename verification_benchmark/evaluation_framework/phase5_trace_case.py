"""Public, reasoning-free loader for packaged Runner trace cases.

This module is intentionally limited to acquisition facts.  It does not read
agent reasoning, ground truth, or verifier decisions while preparing a case.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from .phase5_intake import Phase5IntakeError, resolve_contained, strict_json_bytes


SUPPORTED_RUN_MANIFESTS = (
    "run_manifest.json",
    "phase5_realism_collection_run_manifest.json",
    "phase5_realism_cohort_collection_run_manifest.json",
    "phase5_collection_run_manifest.json",
)


@dataclass(frozen=True)
class CasePaths:
    """Filesystem inputs for verification or evaluation.

    ``ground_truth`` is deliberately optional so production verification never
    needs an answer key. ``task_contract`` is optional provenance input; the
    verifier can load it as a frozen Contract registry artifact.
    """

    run_dir: Path
    intake_receipt: Path
    ground_truth: Optional[Path] = None
    task_contract: Optional[Path] = None
    contract_freeze: Optional[Path] = None


def load_json(path: Path, context: str) -> Mapping[str, Any]:
    return strict_json_bytes(path.read_bytes(), context=context)


def find_run_manifest(run_dir: Path) -> Mapping[str, Any]:
    for name in SUPPORTED_RUN_MANIFESTS:
        path = run_dir / name
        if path.is_file():
            return load_json(path, name)
    raise Phase5IntakeError(f"no supported run manifest found in {run_dir}")


def trace_dir(run_dir: Path, run_manifest: Mapping[str, Any]) -> Path:
    ref = run_manifest.get("trace_relpath")
    if not isinstance(ref, str) or not ref:
        raise Phase5IntakeError("run manifest missing trace_relpath")
    trace = resolve_contained(run_dir, ref)
    if not trace.is_dir():
        raise Phase5IntakeError("trace_relpath is not a directory")
    return trace


def load_actions(
    trace_directory: Path,
) -> tuple[Mapping[str, Any], list[Mapping[str, Any]]]:
    payload = load_json(trace_directory / "actions.json", "actions.json")
    rows = payload.get("actions")
    if not isinstance(rows, list) or not rows:
        raise Phase5IntakeError("actions.json must contain a non-empty actions array")
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise Phase5IntakeError(f"actions[{index}] must be an object")
    return payload, rows


def input_texts(actions: Sequence[Mapping[str, Any]]) -> list[str]:
    return [
        row["text"]
        for row in actions
        if row.get("type") in {"input", "click_input"}
        and isinstance(row.get("text"), str)
        and row["text"].strip()
    ]


def open_app_targets(actions: Sequence[Mapping[str, Any]]) -> list[str]:
    return [
        row["app_name"]
        for row in actions
        if row.get("type") == "open_app"
        and isinstance(row.get("app_name"), str)
        and row["app_name"].strip()
    ]


def first_source_sort_frame(
    actions: Sequence[Mapping[str, Any]], target_app: str
) -> int | None:
    """Return the first frame captured *after* the candidate source click.

    Runner frame ``N`` is captured before action ``N``.  Therefore a click at
    action ``N`` can only be verified from the next captured action frame, not
    from frame ``N`` itself.
    """

    seen_source_input = False
    for offset, row in enumerate(actions):
        action_type = row.get("type")
        if action_type in {"input", "click_input"} and isinstance(row.get("text"), str):
            seen_source_input = True
            continue
        if action_type == "open_app" and row.get("app_name") == target_app:
            return None
        if seen_source_input and action_type == "click":
            for later in actions[offset + 1 :]:
                evidence_index = later.get("action_index")
                if isinstance(evidence_index, int) and evidence_index > 0:
                    return evidence_index
            return None
    return None


# Private compatibility aliases for the existing Phase 5 experimental modules.
_load_json = load_json
_find_run_manifest = find_run_manifest
_trace_dir = trace_dir
_load_actions = load_actions
_input_texts = input_texts
_open_app_targets = open_app_targets
_first_source_sort_frame = first_source_sort_frame


__all__ = [
    "CasePaths",
    "SUPPORTED_RUN_MANIFESTS",
    "find_run_manifest",
    "first_source_sort_frame",
    "input_texts",
    "load_actions",
    "load_json",
    "open_app_targets",
    "trace_dir",
]
