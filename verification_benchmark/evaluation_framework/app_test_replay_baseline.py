"""Ground-truth replay evaluation for the PC App-test agent."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from app_test_agent.mock_executor import ScriptedStepExecutor
from app_test_agent.orchestrator import run_app_test
from app_test_agent.schema import load_test_case

from .app_test_manifest_intake import load_app_test_manifest_evidence


REPLAY_BASELINE_SCHEMA_VERSION = "app-test-replay-baseline-v1"
REPLAY_BASELINE_REPORT_SCHEMA_VERSION = "app-test-replay-baseline-report-v1"
RESULT_LABELS = (
    "APP_PASS",
    "APP_FAIL",
    "TEST_EXECUTION_FAIL",
    "ENV_BLOCKED",
    "INCONCLUSIVE",
    "UNSUPPORTED",
)


class ReplayBaselineError(ValueError):
    pass


@dataclass(frozen=True)
class ReplayBaselineCase:
    case_id: str
    test_case_path: Path
    manifest_path: Path
    ground_truth: str
    key_frames: tuple[int, ...]
    truth_reason: str
    allowed_verification_policy: tuple[str, ...]


def load_replay_baseline_cases(
    config_path: Path,
    *,
    asset_root: Path | None = None,
) -> tuple[ReplayBaselineCase, ...]:
    source = config_path.resolve(strict=True)
    try:
        payload = json.loads(source.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReplayBaselineError(f"cannot load replay baseline config: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise ReplayBaselineError("replay baseline config must be an object")
    if payload.get("schema_version") != REPLAY_BASELINE_SCHEMA_VERSION:
        raise ReplayBaselineError("unsupported replay baseline schema_version")
    raw_cases = payload.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise ReplayBaselineError("replay baseline config requires a non-empty cases list")
    root = asset_root.resolve() if asset_root is not None else source.parent
    cases: list[ReplayBaselineCase] = []
    seen_ids: set[str] = set()
    for index, raw in enumerate(raw_cases):
        if not isinstance(raw, Mapping):
            raise ReplayBaselineError(f"cases[{index}] must be an object")
        case_id = _required_text(raw.get("case_id"), f"cases[{index}].case_id")
        if case_id in seen_ids:
            raise ReplayBaselineError(f"duplicate replay case_id: {case_id}")
        seen_ids.add(case_id)
        ground_truth = _required_text(
            raw.get("ground_truth"), f"cases[{index}].ground_truth"
        ).upper()
        if ground_truth not in RESULT_LABELS:
            raise ReplayBaselineError(
                f"cases[{index}].ground_truth unsupported: {ground_truth}"
            )
        raw_frames = raw.get("key_frames", [])
        if not isinstance(raw_frames, list) or any(
            not isinstance(item, int) or isinstance(item, bool) or item < 0
            for item in raw_frames
        ):
            raise ReplayBaselineError(f"cases[{index}].key_frames must be non-negative integers")
        raw_policy = raw.get("allowed_verification_policy", ["NOT_RUN"])
        if not isinstance(raw_policy, list) or not raw_policy:
            raise ReplayBaselineError(
                f"cases[{index}].allowed_verification_policy must be non-empty"
            )
        cases.append(
            ReplayBaselineCase(
                case_id=case_id,
                test_case_path=_resolve_asset_path(
                    root,
                    _required_text(
                        raw.get("test_case_path"), f"cases[{index}].test_case_path"
                    ),
                ),
                manifest_path=_resolve_asset_path(
                    root,
                    _required_text(
                        raw.get("manifest_path"), f"cases[{index}].manifest_path"
                    ),
                ),
                ground_truth=ground_truth,
                key_frames=tuple(raw_frames),
                truth_reason=_required_text(
                    raw.get("truth_reason"), f"cases[{index}].truth_reason"
                ),
                allowed_verification_policy=tuple(
                    _required_text(item, f"cases[{index}].allowed_verification_policy[]")
                    for item in raw_policy
                ),
            )
        )
    return tuple(cases)


def evaluate_replay_baseline(
    cases: Sequence[ReplayBaselineCase],
    *,
    recompute_step_gates: bool = True,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for case in cases:
        missing = [
            str(path)
            for path in (case.test_case_path, case.manifest_path)
            if not path.is_file()
        ]
        if missing:
            rows.append(_unavailable_row(case, f"missing required asset(s): {', '.join(missing)}"))
            continue
        try:
            test_case = load_test_case(case.test_case_path)
            intake = load_app_test_manifest_evidence(
                test_case=test_case,
                test_case_path=case.test_case_path,
                manifest_path=case.manifest_path,
                recompute_step_gates=recompute_step_gates,
            )
            report = run_app_test(
                test_case,
                ScriptedStepExecutor(
                    intake.execution_record,
                    name="app_test_ground_truth_replay",
                ),
                run_id=f"baseline-{case.case_id}",
            )
        except Exception as exc:  # noqa: BLE001 - one unavailable case must not hide the cohort.
            rows.append(_unavailable_row(case, f"replay error: {type(exc).__name__}: {exc}"))
            continue
        predicted = str(report.get("overall_result") or "")
        if predicted not in RESULT_LABELS:
            rows.append(_unavailable_row(case, f"unsupported replay result: {predicted!r}"))
            continue
        verification = report.get("verification_runner_result")
        verification_status = (
            str(verification.get("status"))
            if isinstance(verification, Mapping) and verification.get("status") is not None
            else "UNKNOWN"
        )
        rows.append(
            {
                "case_id": case.case_id,
                "availability": "EVALUATED",
                "ground_truth": case.ground_truth,
                "predicted": predicted,
                "correct": predicted == case.ground_truth,
                "truth_reason": case.truth_reason,
                "key_frames": list(case.key_frames),
                "test_case_path": str(case.test_case_path),
                "manifest_path": str(case.manifest_path),
                "allowed_verification_policy": list(case.allowed_verification_policy),
                "verification_status": verification_status,
                "verification_policy_conformant": (
                    verification_status in case.allowed_verification_policy
                ),
                "attribution": report.get("attribution"),
                "failed_step": _failed_step(report),
                "intake": intake.as_intake_summary(),
            }
        )
    return summarize_replay_rows(rows, recompute_step_gates=recompute_step_gates)


def summarize_replay_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    recompute_step_gates: bool,
) -> dict[str, Any]:
    evaluated = [row for row in rows if row.get("availability") == "EVALUATED"]
    matrix = {
        truth: {prediction: 0 for prediction in RESULT_LABELS}
        for truth in RESULT_LABELS
    }
    for row in evaluated:
        truth = str(row.get("ground_truth"))
        prediction = str(row.get("predicted"))
        if truth not in matrix or prediction not in matrix[truth]:
            raise ReplayBaselineError(
                f"row has unsupported truth/prediction pair: {truth}/{prediction}"
            )
        matrix[truth][prediction] += 1
    total = len(evaluated)
    correct = sum(1 for row in evaluated if row.get("correct") is True)
    false_pass = sum(
        1
        for row in evaluated
        if row.get("predicted") == "APP_PASS" and row.get("ground_truth") != "APP_PASS"
    )
    false_fail = sum(
        1
        for row in evaluated
        if row.get("predicted") == "APP_FAIL" and row.get("ground_truth") != "APP_FAIL"
    )
    execution_misattribution = sum(
        1
        for row in evaluated
        if row.get("predicted") == "TEST_EXECUTION_FAIL"
        and row.get("ground_truth") != "TEST_EXECUTION_FAIL"
    )
    environment_misattribution = sum(
        1
        for row in evaluated
        if row.get("predicted") == "ENV_BLOCKED"
        and row.get("ground_truth") != "ENV_BLOCKED"
    )
    attribution_labels = {
        "APP_PASS",
        "APP_FAIL",
        "TEST_EXECUTION_FAIL",
        "ENV_BLOCKED",
        "UNSUPPORTED",
    }
    attribution_errors = sum(
        1
        for row in evaluated
        if row.get("ground_truth") in attribution_labels
        and row.get("predicted") in attribution_labels
        and row.get("ground_truth") != row.get("predicted")
    )
    inconclusive = sum(1 for row in evaluated if row.get("predicted") == "INCONCLUSIVE")
    unavailable = len(rows) - total
    return {
        "schema_version": REPLAY_BASELINE_REPORT_SCHEMA_VERSION,
        "recompute_step_gates": recompute_step_gates,
        "labels": list(RESULT_LABELS),
        "summary": {
            "configured_cases": len(rows),
            "evaluated_cases": total,
            "unavailable_cases": unavailable,
            "correct_cases": correct,
            "exact_accuracy": _rate(correct, total),
            "false_pass_count": false_pass,
            "false_pass_rate": _rate(false_pass, total),
            "false_fail_count": false_fail,
            "false_fail_rate": _rate(false_fail, total),
            "attribution_error_count": attribution_errors,
            "attribution_error_rate": _rate(attribution_errors, total),
            "execution_misattribution_count": execution_misattribution,
            "environment_misattribution_count": environment_misattribution,
            "inconclusive_count": inconclusive,
            "inconclusive_rate": _rate(inconclusive, total),
        },
        "confusion_matrix": matrix,
        "rows": [dict(row) for row in rows],
    }


def render_replay_baseline_markdown(report: Mapping[str, Any]) -> str:
    summary = report["summary"]
    labels = tuple(str(item) for item in report["labels"])
    matrix = report["confusion_matrix"]
    lines = [
        "# App-test PC Replay Baseline",
        "",
        f"- Evaluated: {summary['evaluated_cases']} / {summary['configured_cases']}",
        f"- Exact accuracy: {_format_rate(summary['exact_accuracy'])}",
        f"- False pass rate: {_format_rate(summary['false_pass_rate'])}",
        f"- False fail rate: {_format_rate(summary['false_fail_rate'])}",
        f"- Attribution error rate: {_format_rate(summary['attribution_error_rate'])}",
        f"- Inconclusive rate: {_format_rate(summary['inconclusive_rate'])}",
        "",
        "## Confusion matrix",
        "",
        "Truth \\ Predicted | " + " | ".join(labels),
        "--- | " + " | ".join("---:" for _ in labels),
    ]
    for truth in labels:
        lines.append(
            f"{truth} | " + " | ".join(str(matrix[truth][prediction]) for prediction in labels)
        )
    lines.extend(["", "## Cases", ""])
    for row in report["rows"]:
        prediction = row.get("predicted", "UNAVAILABLE")
        lines.append(
            f"- `{row['case_id']}`: truth `{row['ground_truth']}`, predicted `{prediction}`, "
            f"availability `{row['availability']}` — {row.get('truth_reason', row.get('error', ''))}"
        )
    return "\n".join(lines) + "\n"


def _unavailable_row(case: ReplayBaselineCase, error: str) -> dict[str, Any]:
    return {
        "case_id": case.case_id,
        "availability": "UNAVAILABLE",
        "ground_truth": case.ground_truth,
        "predicted": None,
        "correct": None,
        "truth_reason": case.truth_reason,
        "key_frames": list(case.key_frames),
        "test_case_path": str(case.test_case_path),
        "manifest_path": str(case.manifest_path),
        "allowed_verification_policy": list(case.allowed_verification_policy),
        "error": error,
    }


def _failed_step(report: Mapping[str, Any]) -> str | None:
    execution = report.get("execution_result")
    if isinstance(execution, Mapping) and isinstance(execution.get("failed_step"), str):
        return str(execution["failed_step"])
    return None


def _required_text(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReplayBaselineError(f"{context} must be a non-empty string")
    return value.strip()


def _resolve_asset_path(root: Path, value: str) -> Path:
    candidate = Path(value)
    return candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()


def _rate(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _format_rate(value: Any) -> str:
    return "n/a" if value is None else f"{float(value):.2%}"


__all__ = [
    "REPLAY_BASELINE_REPORT_SCHEMA_VERSION",
    "REPLAY_BASELINE_SCHEMA_VERSION",
    "RESULT_LABELS",
    "ReplayBaselineCase",
    "ReplayBaselineError",
    "evaluate_replay_baseline",
    "load_replay_baseline_cases",
    "render_replay_baseline_markdown",
    "summarize_replay_rows",
]
