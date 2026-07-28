"""Turn elastic user-review mistakes into an automatically rerunnable plan."""

from __future__ import annotations

import csv
import hashlib
from pathlib import Path
from typing import Any, Mapping

from .automated_evaluation_plan import (
    AUTOMATED_EVALUATION_PLAN_VERSION,
    EXISTING_TRACE,
)
from .elastic_evaluation import REVIEW_FIELDS, REVIEW_FILE, RUN_MANIFEST_FILE
from .phase5_intake import Phase5IntakeError, canonical_bytes, strict_json_bytes


_FALSE_VALUES = {"0", "false", "no", "n", "否"}


def _reviews(path: Path) -> list[Mapping[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if tuple(reader.fieldnames or ()) != REVIEW_FIELDS:
            raise Phase5IntakeError("user_review.csv header is incompatible")
        return [dict(row) for row in reader]


def build_review_regression_plan(
    evaluation_dir: Path, output_path: Path, *, merge: bool = False
) -> Mapping[str, Any]:
    root = evaluation_dir.resolve(strict=True)
    review_path = root / REVIEW_FILE
    manifest_path = root / RUN_MANIFEST_FILE
    if not review_path.is_file() or not manifest_path.is_file():
        raise Phase5IntakeError("evaluation directory lacks review or run manifest")
    manifest = strict_json_bytes(
        manifest_path.read_bytes(), context="elastic evaluation run manifest"
    )
    cases = manifest.get("cases")
    if not isinstance(cases, list):
        raise Phase5IntakeError("evaluation run manifest cases are invalid")
    case_by_run = {
        str(case.get("run_id")): case
        for case in cases
        if isinstance(case, Mapping) and case.get("run_id")
    }
    mistakes = [
        row
        for row in _reviews(review_path)
        if str(row.get("verifier_correct", "")).strip().lower() in _FALSE_VALUES
    ]
    if not mistakes:
        raise Phase5IntakeError("no verifier_correct=false rows to import")
    task_by_run: dict[str, Mapping[str, Any]] = {}
    review_by_run: dict[str, Mapping[str, Any]] = {}
    target = output_path.resolve()
    if target.exists():
        if not merge:
            raise Phase5IntakeError(f"refusing to overwrite regression plan: {target}")
        previous = strict_json_bytes(
            target.read_bytes(), context="existing review regression plan"
        )
        if previous.get("schema_version") != AUTOMATED_EVALUATION_PLAN_VERSION:
            raise Phase5IntakeError("existing regression plan schema is incompatible")
        for task in previous.get("tasks", []):
            if not isinstance(task, Mapping) or task.get("kind") != EXISTING_TRACE:
                raise Phase5IntakeError(
                    "regression plan may contain only existing traces"
                )
            run_dir = Path(str(task["run_dir"]))
            task_by_run[run_dir.name] = dict(task)
        previous_metadata = previous.get("metadata", {})
        if isinstance(previous_metadata, Mapping):
            for item in previous_metadata.get("review_cases", []):
                if isinstance(item, Mapping) and item.get("run_id"):
                    review_by_run[str(item["run_id"])] = dict(item)
    for review in mistakes:
        run_id = str(review["run_id"])
        case = case_by_run.get(run_id)
        if case is None:
            raise Phase5IntakeError(
                f"reviewed run is absent from run manifest: {run_id}"
            )
        task: dict[str, Any] = {
            "kind": EXISTING_TRACE,
            "run_dir": str(Path(str(case["run_dir"])).resolve(strict=True)),
            "intake_receipt": str(
                Path(str(case["intake_receipt"])).resolve(strict=True)
            ),
        }
        if case.get("task_contract"):
            task["task_contract"] = str(
                Path(str(case["task_contract"])).resolve(strict=True)
            )
        if case.get("contract_freeze"):
            task["contract_freeze"] = str(
                Path(str(case["contract_freeze"])).resolve(strict=True)
            )
        task_by_run[run_id] = task
        review_by_run[run_id] = {
            "run_id": run_id,
            "task_id": review.get("task_id", ""),
            "verifier_verdict": review.get("verifier_verdict", ""),
            "user_expected_verdict": review.get("user_expected_verdict", ""),
            "issue_type": review.get("issue_type", ""),
            "note": review.get("note", ""),
            "source_evaluation_dir": str(root),
        }
    payload = {
        "schema_version": AUTOMATED_EVALUATION_PLAN_VERSION,
        "metadata": {
            "purpose": "Automatically rerun verifier mistakes recorded by the user",
            "source_review_sha256": hashlib.sha256(
                review_path.read_bytes()
            ).hexdigest(),
            "review_cases": [review_by_run[key] for key in sorted(review_by_run)],
        },
        "tasks": [task_by_run[key] for key in sorted(task_by_run)],
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(canonical_bytes(payload))
    return payload


__all__ = ["build_review_regression_plan"]
