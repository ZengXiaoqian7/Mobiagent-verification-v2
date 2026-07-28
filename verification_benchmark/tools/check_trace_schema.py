#!/usr/bin/env python3
"""Check Runner/MobiFlow trace directory schema.

Schema errors mean MobiFlow cannot reliably read the trace. Warnings mark data
hygiene issues that should be visible in reports but do not always block
evaluation.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List, Set


def _read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _numeric_indices(path: Path, suffix: str) -> Set[int]:
    indices: Set[int] = set()
    if not path.exists() or not path.is_dir():
        return indices
    for item in path.iterdir():
        if not item.is_file() or item.suffix.lower() != suffix:
            continue
        try:
            indices.add(int(item.stem))
        except ValueError:
            continue
    return indices


def inspect_trace(path: Path | str) -> Dict[str, Any]:
    """Return schema and hygiene information for a trace directory."""

    trace_dir = Path(path)
    errors: List[str] = []
    warnings: List[str] = []

    if not trace_dir.exists():
        return {
            "ok": False,
            "path": str(trace_dir),
            "errors": [f"trace directory does not exist: {trace_dir}"],
            "warnings": [],
        }
    if not trace_dir.is_dir():
        return {
            "ok": False,
            "path": str(trace_dir),
            "errors": [f"trace path is not a directory: {trace_dir}"],
            "warnings": [],
        }

    actions_path = trace_dir / "actions.json"
    react_path = trace_dir / "react.json"
    actions_meta: Dict[str, Any] = {}
    actions: List[Any] = []
    reacts: List[Any] = []

    if not actions_path.exists():
        errors.append("missing actions.json")
    else:
        try:
            loaded = _read_json(actions_path)
            if not isinstance(loaded, dict):
                errors.append("actions.json must contain an object")
            else:
                actions_meta = loaded
                raw_actions = loaded.get("actions")
                if not isinstance(raw_actions, list):
                    errors.append("actions.json.actions must be a list")
                else:
                    actions = raw_actions
        except Exception as exc:  # noqa: BLE001
            errors.append(f"actions.json parse failed: {type(exc).__name__}: {exc}")

    if not react_path.exists():
        errors.append("missing react.json")
    else:
        try:
            loaded = _read_json(react_path)
            if not isinstance(loaded, list):
                errors.append("react.json must contain a list")
            else:
                reacts = loaded
        except Exception as exc:  # noqa: BLE001
            errors.append(f"react.json parse failed: {type(exc).__name__}: {exc}")

    jpg_indices = _numeric_indices(trace_dir, ".jpg")
    xml_indices = _numeric_indices(trace_dir, ".xml")
    artifact_indices = sorted(jpg_indices | xml_indices)
    action_count = len(actions)
    declared_action_count = actions_meta.get("action_count")

    if declared_action_count is not None and declared_action_count != action_count:
        warnings.append(
            f"actions.json action_count={declared_action_count} but actions list has {action_count}"
        )

    if reacts and action_count and len(reacts) != action_count:
        warnings.append(f"react.json has {len(reacts)} entries but actions list has {action_count}")

    if not artifact_indices:
        errors.append("missing numeric frame artifacts such as 1.jpg or 1.xml")

    expected = set(range(1, action_count + 1)) if action_count else set()
    missing_jpg = sorted(expected - jpg_indices)
    missing_xml = sorted(expected - xml_indices)
    extra_artifacts = sorted(idx for idx in artifact_indices if action_count and idx > action_count)

    if missing_jpg and jpg_indices:
        warnings.append(f"missing screenshots for action indices: {missing_jpg}")
    if missing_xml:
        warnings.append(f"missing XML files for action indices: {missing_xml}")
    if extra_artifacts:
        warnings.append(
            f"extra numeric frame artifacts beyond actions list: {extra_artifacts}; "
            "MobiFlow may load them with empty action/reasoning fields"
        )

    action_indices = [
        item.get("action_index")
        for item in actions
        if isinstance(item, dict) and item.get("action_index") is not None
    ]
    if action_indices and action_indices != list(range(1, len(action_indices) + 1)):
        warnings.append(f"non-contiguous action_index sequence: {action_indices}")

    return {
        "ok": not errors,
        "path": str(trace_dir),
        "errors": errors,
        "warnings": warnings,
        "action_count": action_count,
        "declared_action_count": declared_action_count,
        "react_count": len(reacts),
        "jpg_indices": sorted(jpg_indices),
        "xml_indices": sorted(xml_indices),
        "artifact_indices": artifact_indices,
        "missing_jpg": missing_jpg,
        "missing_xml": missing_xml,
        "extra_artifacts": extra_artifacts,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Check a Runner/MobiFlow trace directory schema.")
    parser.add_argument("trace_dir")
    parser.add_argument("--json", action="store_true", help="Print full JSON report.")
    args = parser.parse_args()

    report = inspect_trace(Path(args.trace_dir))
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print("Trace schema:", "OK" if report["ok"] else "ERROR")
        for error in report.get("errors") or []:
            print(f"error: {error}")
        for warning in report.get("warnings") or []:
            print(f"warning: {warning}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
