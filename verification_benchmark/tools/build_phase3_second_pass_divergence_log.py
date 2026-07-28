#!/usr/bin/env python3
"""Build a post-hoc, label-bound divergence log from the Phase 3 second pass."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import sys
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


DIVERGENCE_LOG_SCHEMA_VERSION = "harmony-eval-development-divergence-log-v1"
DIVERGENCE_AUDITOR_VERSION = "harmony-eval-phase3-divergence-auditor-v1"
RESULT_PATH = (
    ROOT
    / "verification_benchmark/reports/audit_batch/development/phase3_taobao_search_replay_alignment_second_pass_v1/batch_result.json"
)
LABELS_PATH = ROOT / "verification_benchmark/labels_taobao_batch_false_done.jsonl"
CACHE_PATH = (
    ROOT
    / "verification_benchmark/reports/recording_capability_expansion/development/phase3_taobao_search_full_vcr_v1/cache.jsonl"
)
RECEIPT_PATH = CACHE_PATH.with_name("receipt.json")
OUTPUT_PATH = RESULT_PATH.with_name("divergence_log.json")
VISUAL_RESULT_PATH = (
    ROOT
    / "verification_benchmark/reports/audit_batch/development/phase3_taobao_search_replay_alignment_visual_state_v1/batch_result.json"
)
VISUAL_CACHE_PATH = (
    ROOT
    / "verification_benchmark/reports/visual_state_capability_expansion/development/phase3_taobao_search_visual_state_v1/cache.jsonl"
)
VISUAL_RECEIPT_PATH = VISUAL_CACHE_PATH.with_name("receipt.json")
ENHANCED_SYSTEM_ID = "enhanced-v2-development-real8-visual-gate"
ENHANCED_REPORT_PATH = (
    ROOT
    / "verification_benchmark/reports/real8_visual_gate_deterministic/benchmark_eval_deterministic.json"
)
ENHANCED_REPORT_SHA256 = (
    "b6010be627bcebabf31a23680e20535befbd9259bdcef03ad0a36ab12623fb7b"
)
EXPECTED_BATCH_ID = "phase3-taobao-search-development-replay-alignment-second-pass-v1"
EXPECTED_VISUAL_BATCH_ID = (
    "phase3-taobao-search-development-replay-alignment-visual-state-v1"
)
EXPECTED_RECORDING_MANIFEST_SHA256 = (
    "45975d2f73ea4847847a4010ae847d76299fd2f6df9013221040896a34ece1c7"
)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key is forbidden: {key}")
        result[key] = value
    return result


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        + "\n"
    ).encode("utf-8")


def _load_json(path: Path) -> Mapping[str, Any]:
    value = json.loads(
        path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_keys
    )
    if not isinstance(value, Mapping):
        raise ValueError(f"JSON source must be an object: {path}")
    return value


def _load_ground_truth(path: Path) -> dict[str, str]:
    result = {}
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), 1
    ):
        if not line.strip():
            raise ValueError(f"blank label line is forbidden: {line_number}")
        row = json.loads(line, object_pairs_hook=_reject_duplicate_keys)
        if not isinstance(row, Mapping):
            raise ValueError("label row must be an object")
        trace_id = row.get("trace_id")
        ground_truth = row.get("ground_truth")
        if not isinstance(trace_id, str) or not trace_id:
            raise ValueError("label trace_id must be non-empty")
        if ground_truth not in {"success", "fail"}:
            raise ValueError("label ground_truth must be success or fail")
        if trace_id in result:
            raise ValueError("duplicate label trace_id is forbidden")
        result[trace_id] = "PASS" if ground_truth == "success" else "FAIL"
    return result


def build_divergence_log(
    root: Path = ROOT, *, visual_state_pass: bool = False
) -> dict[str, Any]:
    root = root.resolve()
    result_path = VISUAL_RESULT_PATH if visual_state_pass else RESULT_PATH
    result = _load_json(result_path)
    semantic = dict(result)
    claimed_result_sha = semantic.pop("result_sha256", None)
    if claimed_result_sha != _digest(semantic):
        raise ValueError("second-pass result semantic SHA-256 mismatch")
    expected_schema = (
        "harmony-eval-development-batch-result-v2"
        if visual_state_pass
        else "harmony-eval-development-batch-result-v1"
    )
    if result.get("schema_version") != expected_schema:
        raise ValueError("unsupported replay result schema")
    if result.get("data_role") != "development":
        raise ValueError("divergence log is development-only")
    expected_batch_id = (
        EXPECTED_VISUAL_BATCH_ID if visual_state_pass else EXPECTED_BATCH_ID
    )
    if result.get("batch_id") != expected_batch_id:
        raise ValueError("divergence log received the wrong batch result")
    labels = _load_ground_truth(LABELS_PATH)
    receipt = _load_json(RECEIPT_PATH)
    if receipt.get("status") != "COMPLETE":
        raise ValueError("recording receipt is not COMPLETE")
    if receipt.get("manifest_sha256") != EXPECTED_RECORDING_MANIFEST_SHA256:
        raise ValueError("recording receipt Manifest SHA-256 mismatch")
    if receipt.get("api_key_recorded") is not False:
        raise ValueError(
            "recording receipt must prove that the API key was not recorded"
        )
    receipt_summary = receipt.get("summary")
    if not isinstance(receipt_summary, Mapping) or {
        name: receipt_summary.get(name)
        for name in ("recorded", "cached", "errors", "requests")
    } != {"recorded": 52, "cached": 2, "errors": 0, "requests": 52}:
        raise ValueError("recording receipt summary differs from the authorized run")
    sessions = receipt.get("sessions")
    if not isinstance(sessions, list) or len(sessions) != 1:
        raise ValueError("recording receipt must contain exactly one session")
    session = sessions[0]
    if not isinstance(session, Mapping) or {
        "request_budget": session.get("request_budget"),
        "worst_case_requests": session.get("worst_case_requests"),
        "item_count": len(session.get("items", [])),
    } != {"request_budget": 52, "worst_case_requests": 52, "item_count": 54}:
        raise ValueError("recording receipt session differs from the authorized budget")
    evidence_sources = result.get("evidence_sources")
    recorded_storage_sha256 = (
        evidence_sources.get("recorded_ocr_llm", {}).get("storage_sha256")
        if visual_state_pass and isinstance(evidence_sources, Mapping)
        else result.get("evidence_storage_sha256")
    )
    if receipt.get("cache_storage_sha256") != recorded_storage_sha256:
        raise ValueError("receipt cache storage SHA differs from second-pass result")

    system_summaries = result.get("historical_alignment", {}).get("systems")
    alignment_rows = result.get("historical_alignment", {}).get("rows")
    if not isinstance(system_summaries, list) or not isinstance(alignment_rows, list):
        raise ValueError("second-pass alignment payload is malformed")
    systems = {
        item["system_id"]: item
        for item in system_summaries
        if isinstance(item, Mapping) and isinstance(item.get("system_id"), str)
    }
    if ENHANCED_SYSTEM_ID not in systems:
        raise ValueError("Enhanced v2 alignment summary is absent")
    if _file_sha256(ENHANCED_REPORT_PATH) != ENHANCED_REPORT_SHA256:
        raise ValueError("Enhanced v2 source report SHA-256 drifted")

    divergences = []
    unresolved = []
    for row in alignment_rows:
        if not isinstance(row, Mapping):
            raise ValueError("alignment row must be an object")
        status = row.get("status")
        if status == "DISAGREES":
            trace_id = row["trace_id"]
            ground_truth = labels.get(trace_id)
            if ground_truth is None:
                classification = "UNRESOLVED_NO_EXTERNAL_LABEL"
            elif (
                row["new_kernel_verdict"] == ground_truth
                and row["historical_verdict"] != ground_truth
            ):
                classification = "NEW_CORRECTS_HISTORICAL_ERROR"
            elif (
                row["historical_verdict"] == ground_truth
                and row["new_kernel_verdict"] != ground_truth
            ):
                classification = "NEW_REGRESSION_AGAINST_HISTORICAL"
            else:
                classification = "UNRESOLVED_WITH_EXTERNAL_LABEL"
            divergences.append(
                {
                    "system_id": row["system_id"],
                    "trace_id": trace_id,
                    "ground_truth": ground_truth,
                    "historical_verdict": row["historical_verdict"],
                    "new_kernel_verdict": row["new_kernel_verdict"],
                    "classification": classification,
                }
            )
        elif status in {"NEW_UNSUPPORTED", "NEW_ABSTAIN", "NEW_INVALID_TRACE"}:
            unresolved.append(
                {
                    "system_id": row["system_id"],
                    "trace_id": row["trace_id"],
                    "historical_verdict": row["historical_verdict"],
                    "new_kernel_verdict": row["new_kernel_verdict"],
                    "status": status,
                }
            )
    divergences.sort(key=lambda item: (item["system_id"], item["trace_id"]))
    unresolved.sort(key=lambda item: (item["system_id"], item["trace_id"]))
    enhanced_divergences = sum(
        item["system_id"] == ENHANCED_SYSTEM_ID for item in divergences
    )
    corrections = sum(
        item["classification"] == "NEW_CORRECTS_HISTORICAL_ERROR"
        for item in divergences
    )
    regressions = sum(
        item["classification"] == "NEW_REGRESSION_AGAINST_HISTORICAL"
        for item in divergences
    )
    result_input_name = (
        "visual_state_pass_result" if visual_state_pass else "second_pass_result"
    )
    inputs: dict[str, Any] = {
        result_input_name: {
            "source_ref": result_path.relative_to(root).as_posix(),
            "file_sha256": _file_sha256(result_path),
            "result_sha256": claimed_result_sha,
        },
        "ground_truth_labels": {
            "source_ref": LABELS_PATH.relative_to(root).as_posix(),
            "file_sha256": _file_sha256(LABELS_PATH),
            "trace_count": len(labels),
        },
        "enhanced_v2_baseline": {
            "source_ref": ENHANCED_REPORT_PATH.relative_to(root).as_posix(),
            "file_sha256": ENHANCED_REPORT_SHA256,
            "frozen_tag": "enhanced-v2-20260713",
            "frozen_commit": "652ec29aa0708feb5d56364b9cdf0f4d45bc233b",
            "git_blob_sha1": "d26996f196d31ba14c497ea989c51e0c8439f3bf",
        },
        "recorded_cache": {
            "source_ref": CACHE_PATH.relative_to(root).as_posix(),
            "file_sha256": _file_sha256(CACHE_PATH),
            "storage_sha256": recorded_storage_sha256,
        },
        "recording_receipt": {
            "source_ref": RECEIPT_PATH.relative_to(root).as_posix(),
            "file_sha256": _file_sha256(RECEIPT_PATH),
            "manifest_sha256": receipt["manifest_sha256"],
            "request_count": receipt["summary"]["requests"],
        },
    }
    if visual_state_pass:
        visual_receipt = _load_json(VISUAL_RECEIPT_PATH)
        visual_source = evidence_sources.get("visual_state", {})
        if not isinstance(visual_source, Mapping):
            raise ValueError("visual-state evidence source identity is absent")
        if visual_receipt.get("safety_boundary") != {
            "api_key_read": False,
            "main_replay_cache_lookup_only": True,
            "main_replay_pixel_decoding_allowed": False,
            "network_used": False,
        }:
            raise ValueError("visual-state safety boundary is not closed")
        if visual_receipt.get("cache", {}).get("storage_sha256") != visual_source.get(
            "storage_sha256"
        ):
            raise ValueError("visual-state receipt differs from replay identity")
        inputs["visual_state_cache"] = {
            "source_ref": VISUAL_CACHE_PATH.relative_to(root).as_posix(),
            "file_sha256": _file_sha256(VISUAL_CACHE_PATH),
            "storage_sha256": visual_source["storage_sha256"],
            "provider_plan_sha256": visual_source["provider_plan_sha256"],
            "detector_version": visual_source["detector_version"],
        }
        inputs["visual_state_receipt"] = {
            "source_ref": VISUAL_RECEIPT_PATH.relative_to(root).as_posix(),
            "file_sha256": _file_sha256(VISUAL_RECEIPT_PATH),
            "network_used": False,
            "api_key_read": False,
        }
    log: dict[str, Any] = {
        "schema_version": DIVERGENCE_LOG_SCHEMA_VERSION,
        "auditor_version": DIVERGENCE_AUDITOR_VERSION,
        "data_role": "development",
        "inputs": inputs,
        "new_kernel": {
            "trace_count": result["summary"]["trace_count"],
            "decided_count": result["summary"]["new_kernel_decided_count"],
            "coverage": result["summary"]["new_kernel_coverage"],
            "verdict_counts": result["summary"]["verdict_counts"],
        },
        "system_alignment": system_summaries,
        "summary": {
            "enhanced_v2_comparable_rows": systems[ENHANCED_SYSTEM_ID][
                "comparable_rows"
            ],
            "enhanced_v2_agreement_rate": systems[ENHANCED_SYSTEM_ID]["agreement_rate"],
            "enhanced_v2_divergence_count": enhanced_divergences,
            "all_system_divergence_count": len(divergences),
            "label_supported_correction_count": corrections,
            "label_supported_regression_count": regressions,
            "unresolved_comparison_count": len(unresolved),
        },
        "divergences": divergences,
        "unresolved_comparisons": unresolved,
        "claim_boundary": {
            "ground_truth_consumed_by_new_kernel": False,
            "ground_truth_consumed_by_posthoc_divergence_audit": True,
            "correction_requires_external_label": True,
            "agreement_is_conditional_on_comparable_rows": True,
            "unsupported_is_not_disagreement": True,
            "heldout_performance_claimed": False,
            "accuracy_claimed": False,
        },
    }
    log["divergence_log_sha256"] = _digest(log)
    return log


def _write_or_compare(path: Path, expected: bytes, *, check: bool) -> None:
    if path.exists():
        if not hmac.compare_digest(path.read_bytes(), expected):
            raise ValueError(
                "committed divergence log differs from deterministic output"
            )
        return
    if check:
        raise ValueError("committed divergence log is missing")
    with path.open("xb") as stream:
        stream.write(expected)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--visual-state-pass", action="store_true")
    args = parser.parse_args(argv)
    log = build_divergence_log(ROOT, visual_state_pass=args.visual_state_pass)
    output_path = (
        VISUAL_RESULT_PATH.with_name("divergence_log.json")
        if args.visual_state_pass
        else OUTPUT_PATH
    )
    _write_or_compare(output_path, _json_bytes(log), check=args.check)
    print(
        json.dumps(
            {
                "coverage": log["new_kernel"]["coverage"],
                "enhanced_v2_agreement_rate": log["summary"][
                    "enhanced_v2_agreement_rate"
                ],
                "enhanced_v2_divergences": log["summary"][
                    "enhanced_v2_divergence_count"
                ],
                "all_divergences": log["summary"]["all_system_divergence_count"],
                "corrections": log["summary"]["label_supported_correction_count"],
                "regressions": log["summary"]["label_supported_regression_count"],
                "divergence_log_sha256": log["divergence_log_sha256"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
