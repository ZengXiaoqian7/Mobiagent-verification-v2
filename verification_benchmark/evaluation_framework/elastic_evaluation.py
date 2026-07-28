"""Elastic trace evaluation loop for user-reviewed verifier accuracy."""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from .contract_catalog import task_family_from_run_manifest
from .jit_contract_compiler import JitAppMetadata, JitCompileRequest
from .jit_model_proposer import OpenAICompatibleJitProposer
from .phase5_full_verifier_comparison import ProviderConfig, VisionCallRecorder
from .phase5_intake import Phase5IntakeError, canonical_bytes, write_new_json
from .phase5_trace_case import CasePaths, find_run_manifest
from .task_spec import TaskSpec
from .upgraded_verifier import (
    Phase5CheckerBackend,
    UPGRADED_VERIFIER_VERSION,
    UpgradedVerifierConfig,
    verify_trace_case,
)


ELASTIC_EVALUATION_VERSION = "mobiagent-elastic-evaluation-v1"
RESULTS_FILE = "results.jsonl"
SUMMARY_FILE = "summary.json"
REVIEW_FILE = "user_review.csv"
RUN_MANIFEST_FILE = "run_manifest.json"
REVIEW_FIELDS = (
    "run_id",
    "task_id",
    "verifier_verdict",
    "user_expected_verdict",
    "verifier_correct",
    "issue_type",
    "note",
)


class BaselineAdapter(Protocol):
    def verify(
        self, case: CasePaths, recorder: VisionCallRecorder
    ) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class ElasticEvaluationConfig:
    provider: ProviderConfig | None
    include_diagnostics: bool = False
    continue_on_error: bool = True
    selection_key: str | None = None
    checker_backend: Phase5CheckerBackend | None = None
    orchestration: Mapping[str, Any] | None = None
    baseline_adapter: BaselineAdapter | None = None
    cache_dir: Path | None = None
    enable_validated_jit: bool = False


def _jit_request_for_run(run: Mapping[str, Any]) -> tuple[TaskSpec, JitCompileRequest]:
    task_spec = TaskSpec.from_run_manifest(run)
    return task_spec, JitCompileRequest(
        task_description=task_spec.task_text,
        app_metadata=JitAppMetadata(
            app_id=task_spec.initial_app or "unknown-app",
            app_name=task_spec.initial_app or "unknown-app",
            platform="HarmonyOS",
            task_family=(
                None if task_spec.task_family == "unseen" else task_spec.task_family
            ),
            risk_tier={
                "read_only": "LOW",
                "low_risk_write": "MEDIUM",
                "high_risk": "HIGH",
            }[task_spec.risk_level],
        ),
    )


def _read_results(path: Path) -> list[Mapping[str, Any]]:
    if not path.is_file():
        return []
    rows = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), 1
    ):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, Mapping):
            raise Phase5IntakeError(
                f"{RESULTS_FILE}:{line_number} must contain an object"
            )
        rows.append(value)
    return rows


def _read_reviews(path: Path) -> dict[str, dict[str, str]]:
    if not path.is_file():
        return {}
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if tuple(reader.fieldnames or ()) != REVIEW_FIELDS:
            raise Phase5IntakeError("existing user_review.csv header is incompatible")
        return {str(row["run_id"]): dict(row) for row in reader}


def _write_results(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    rendered = b"".join(canonical_bytes(row) + b"\n" for row in rows)
    path.write_bytes(rendered)


def _write_reviews(
    path: Path,
    rows: Sequence[Mapping[str, Any]],
    existing: Mapping[str, Mapping[str, str]],
) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=REVIEW_FIELDS)
        writer.writeheader()
        for row in rows:
            run_id = str(row["run_id"])
            previous = existing.get(run_id, {})
            writer.writerow(
                {
                    "run_id": run_id,
                    "task_id": row["task_id"],
                    "verifier_verdict": row["verdict"],
                    "user_expected_verdict": previous.get("user_expected_verdict", ""),
                    "verifier_correct": previous.get("verifier_correct", ""),
                    "issue_type": previous.get("issue_type", ""),
                    "note": previous.get("note", ""),
                }
            )


def _summary(
    rows: Sequence[Mapping[str, Any]], reviews: Mapping[str, Mapping[str, str]]
) -> Mapping[str, Any]:
    verdicts = ("PASS", "FAIL", "ABSTAIN", "INVALID_TRACE", "UNSUPPORTED")
    reviewed = [
        review
        for review in reviews.values()
        if str(review.get("verifier_correct", "")).strip()
    ]
    correct_values = {
        "1": True,
        "true": True,
        "yes": True,
        "y": True,
        "是": True,
        "0": False,
        "false": False,
        "no": False,
        "n": False,
        "否": False,
    }
    parsed = [
        correct_values.get(str(row.get("verifier_correct", "")).strip().lower())
        for row in reviewed
    ]
    incorrect_run_ids = {
        str(row.get("run_id"))
        for row, parsed_value in zip(reviewed, parsed)
        if parsed_value is False
    }
    result_by_run = {str(row.get("run_id")): row for row in rows}

    def distribution(field: str) -> Mapping[str, int]:
        values: dict[str, int] = {}
        for run_id in incorrect_run_ids:
            value = str(result_by_run.get(run_id, {}).get(field) or "UNKNOWN")
            values[value] = values.get(value, 0) + 1
        return dict(sorted(values.items()))

    issue_types: dict[str, int] = {}
    for review, parsed_value in zip(reviewed, parsed):
        if parsed_value is not False:
            continue
        issue = str(review.get("issue_type") or "UNSPECIFIED").strip() or "UNSPECIFIED"
        issue_types[issue] = issue_types.get(issue, 0) + 1
    comparison_rows = [
        row for row in rows if isinstance(row.get("mobiflow_baseline"), Mapping)
    ]
    comparison = {
        "enabled": bool(comparison_rows),
        "comparable": sum(
            row["mobiflow_baseline"].get("verdict") in {"PASS", "FAIL"}
            for row in comparison_rows
        ),
        "agreement": sum(
            row.get("verdict") == row["mobiflow_baseline"].get("verdict")
            for row in comparison_rows
            if row["mobiflow_baseline"].get("verdict") in {"PASS", "FAIL"}
        ),
        "upgraded_pass_mobiflow_fail": sum(
            row.get("verdict") == "PASS"
            and row["mobiflow_baseline"].get("verdict") == "FAIL"
            for row in comparison_rows
        ),
        "upgraded_fail_mobiflow_pass": sum(
            row.get("verdict") == "FAIL"
            and row["mobiflow_baseline"].get("verdict") == "PASS"
            for row in comparison_rows
        ),
        "mobiflow_abstain_or_error": sum(
            row["mobiflow_baseline"].get("verdict") not in {"PASS", "FAIL"}
            for row in comparison_rows
        ),
    }
    return {
        "total": len(rows),
        "verdict_counts": {
            verdict: sum(row.get("verdict") == verdict for row in rows)
            for verdict in verdicts
        },
        "review": {
            "reviewed": len(reviewed),
            "correct": sum(value is True for value in parsed),
            "incorrect": sum(value is False for value in parsed),
            "unparsed": sum(value is None for value in parsed),
            "false_pass": sum(
                review.get("verifier_verdict") == "PASS"
                and review.get("user_expected_verdict") == "FAIL"
                for review in reviewed
            ),
            "false_fail": sum(
                review.get("verifier_verdict") == "FAIL"
                and review.get("user_expected_verdict") == "PASS"
                for review in reviewed
            ),
            "expected_abstain": sum(
                review.get("user_expected_verdict") == "ABSTAIN" for review in reviewed
            ),
            "incorrect_distribution": {
                "initial_app": distribution("initial_app"),
                "task_family": distribution("task_family"),
                "issue_type": dict(sorted(issue_types.items())),
            },
        },
        "mobiflow_comparison": comparison,
    }


def run_elastic_evaluation(
    cases: Sequence[CasePaths],
    config: ElasticEvaluationConfig,
    output_dir: Path,
    *,
    resume: bool = False,
) -> Mapping[str, Any]:
    if not cases and config.orchestration is None:
        raise ValueError("at least one trace case is required")
    if config.enable_validated_jit and config.provider is None:
        raise Phase5IntakeError(
            "Validated JIT requires a configured model provider"
        )
    root = output_dir.resolve()
    if root.exists() and not resume:
        raise Phase5IntakeError(f"refusing to overwrite evaluation output: {root}")
    root.mkdir(parents=True, exist_ok=resume)

    results_path = root / RESULTS_FILE
    review_path = root / REVIEW_FILE
    existing_rows = _read_results(results_path) if resume else []
    reviews = _read_reviews(review_path) if resume else {}
    completed = {str(row["run_id"]) for row in existing_rows}
    rows = list(existing_rows)
    recorder = (
        None
        if config.provider is None
        else (
            VisionCallRecorder(config.provider)
            if config.cache_dir is None
            else VisionCallRecorder(config.provider, config.cache_dir)
        )
    )
    baseline_recorder = (
        (
            VisionCallRecorder(config.provider)
            if config.cache_dir is None
            else VisionCallRecorder(config.provider, config.cache_dir)
        )
        if config.provider is not None and config.baseline_adapter is not None
        else None
    )
    requested_cases = []
    for case in cases:
        run = find_run_manifest(case.run_dir.resolve(strict=True))
        run_id = str(run["run_id"])
        task_id = str(run["task_id"])
        requested_cases.append(
            {
                "run_id": run_id,
                "task_id": task_id,
                "run_dir": str(case.run_dir.resolve(strict=True)),
                "intake_receipt": str(case.intake_receipt.resolve(strict=True)),
                "task_contract": (
                    str(case.task_contract.resolve(strict=True))
                    if case.task_contract is not None
                    else None
                ),
                "contract_freeze": (
                    str(case.contract_freeze.resolve(strict=True))
                    if case.contract_freeze is not None
                    else None
                ),
            }
        )
        if run_id in completed:
            continue
        task_spec = None
        jit_request = None
        jit_proposer = None
        selection_key = config.selection_key
        if config.enable_validated_jit:
            assert config.provider is not None
            task_spec, jit_request = _jit_request_for_run(run)
            if selection_key is None:
                selection_key = jit_request.selection_key
            jit_proposer = OpenAICompatibleJitProposer(
                base_url=config.provider.base_url,
                model=config.provider.model,
                api_key=config.provider.api_key,
                timeout=config.provider.timeout,
                max_retries=config.provider.max_retries,
            )
        verification = verify_trace_case(
            case,
            UpgradedVerifierConfig(
                provider=config.provider,
                selection_key=selection_key,
                enable_validated_jit=config.enable_validated_jit,
                jit_request=jit_request,
                jit_proposer=jit_proposer,
                task_spec=task_spec,
                include_diagnostics=config.include_diagnostics,
                continue_on_error=config.continue_on_error,
                checker_backend=config.checker_backend,
                cache_dir=config.cache_dir,
            ),
            recorder=recorder,
        )
        row = {
            "run_id": run_id,
            "task_id": task_id,
            "experiment_id": str(run.get("experiment_id") or ""),
            "task_family": task_family_from_run_manifest(run),
            "initial_app": str(run.get("initial_app") or ""),
            "target_app": str(run.get("target_app") or ""),
            "trace_dir": str(case.run_dir.resolve(strict=True)),
            "intake_receipt": str(case.intake_receipt.resolve(strict=True)),
            "task_contract": (
                str(case.task_contract.resolve(strict=True))
                if case.task_contract is not None
                else None
            ),
            "contract_freeze": (
                str(case.contract_freeze.resolve(strict=True))
                if case.contract_freeze is not None
                else None
            ),
            **verification.result.as_dict(),
        }
        if config.baseline_adapter is not None:
            if baseline_recorder is None:
                raise Phase5IntakeError(
                    "MobiFlow comparison requires a configured model provider"
                )
            try:
                baseline = config.baseline_adapter.verify(case, baseline_recorder)
                row["mobiflow_baseline"] = {
                    "ok": bool(baseline.get("ok")),
                    "verdict": str(baseline.get("verdict") or "ABSTAIN"),
                    "reason": str(baseline.get("reason") or ""),
                }
            except Exception as exc:  # noqa: BLE001 - comparison is never primary.
                row["mobiflow_baseline"] = {
                    "ok": False,
                    "verdict": "ABSTAIN",
                    "reason": f"baseline error: {type(exc).__name__}: {exc}",
                }
        rows.append(row)
        if config.include_diagnostics:
            write_new_json(
                root / "diagnostics" / f"{run_id}.json",
                verification.diagnostics,
            )
        _write_results(results_path, rows)

    rows = sorted(rows, key=lambda row: (str(row["run_id"]), str(row["task_id"])))
    _write_results(results_path, rows)
    _write_reviews(review_path, rows, reviews)
    current_reviews = _read_reviews(review_path)
    summary = dict(_summary(rows, current_reviews))
    task_records = (
        config.orchestration.get("tasks", ())
        if isinstance(config.orchestration, Mapping)
        else ()
    )
    if isinstance(task_records, Sequence) and not isinstance(
        task_records, (str, bytes)
    ):
        runner_statuses: dict[str, int] = {}
        for record in task_records:
            if not isinstance(record, Mapping):
                continue
            status = str(record.get("status") or "UNKNOWN")
            runner_statuses[status] = runner_statuses.get(status, 0) + 1
        summary["runner_execution"] = {
            "status_counts": dict(sorted(runner_statuses.items())),
            "error_count": sum(
                count
                for status, count in runner_statuses.items()
                if status
                in {
                    "RUNNER_ERROR",
                    "RUNNER_FAILED",
                    "INTAKE_ERROR",
                    "INTAKE_FAILED",
                    "INTAKE_RECEIPT_MISSING",
                }
            ),
        }
    (root / SUMMARY_FILE).write_bytes(canonical_bytes(summary))
    manifest = {
        "evaluation_version": ELASTIC_EVALUATION_VERSION,
        "verifier_version": UPGRADED_VERIFIER_VERSION,
        "provider": (
            None
            if config.provider is None
            else {
                "base_url": config.provider.base_url,
                "model": config.provider.model,
                "api_key_env": config.provider.api_key_env,
                "transport": config.provider.transport,
            }
        ),
        "deterministic_only": config.provider is None,
        "resume": resume,
        "include_diagnostics": config.include_diagnostics,
        "cache_dir": str(config.cache_dir.resolve()) if config.cache_dir else None,
        "cases": requested_cases,
        "orchestration": (
            dict(config.orchestration) if config.orchestration is not None else None
        ),
        "mobiflow_comparison": config.baseline_adapter is not None,
        "outputs": {
            "results": RESULTS_FILE,
            "summary": SUMMARY_FILE,
            "user_review": REVIEW_FILE,
            "diagnostics": "diagnostics/" if config.include_diagnostics else None,
        },
    }
    (root / RUN_MANIFEST_FILE).write_bytes(canonical_bytes(manifest))
    return {"output_dir": str(root), "summary": summary, "manifest": manifest}


__all__ = [
    "ELASTIC_EVALUATION_VERSION",
    "ElasticEvaluationConfig",
    "RESULTS_FILE",
    "REVIEW_FIELDS",
    "REVIEW_FILE",
    "RUN_MANIFEST_FILE",
    "SUMMARY_FILE",
    "run_elastic_evaluation",
]
