#!/usr/bin/env python3
"""Build the exact, zero-network VCR manifest for the Phase 3 eight-trace batch."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from verification_benchmark.evaluation_framework import (  # noqa: E402
    EvidenceCacheKey,
    PrecomputedEvidenceStorage,
    RecordedLlmRequestIR,
    RecordedOcrRequestIR,
    RecordedProviderBindingIR,
    RecordedProviderKind,
    RecordedProviderPlan,
    adapt_legacy_yaml,
    contract_sha256,
    load_batch_replay_manifest,
    load_local_legacy_checker_evidence,
    load_trace_directory,
    trace_bundle_to_event_trace,
)
from verification_benchmark.tools.record_precomputed_evidence import (  # noqa: E402
    AUTHORIZED_BASE_URL,
    AUTHORIZED_MODEL,
    LLM_PROMPT_TEMPLATE_VERSION,
    RecordingJob,
    RecordingManifest,
)


INVENTORY_SCHEMA_VERSION = "harmony-eval-exact-vcr-inventory-v1"
GENERATOR_VERSION = "harmony-eval-phase3-exact-vcr-generator-v1"
BATCH_MANIFEST_PATH = (
    ROOT
    / "verification_benchmark/batch_manifests/development/phase3_taobao_search_replay_alignment_v1.json"
)
OUTPUT_MANIFEST_PATH = (
    ROOT
    / "verification_benchmark/recording_manifests/development/phase3_taobao_search_full_vcr_v1.json"
)
OUTPUT_AUDIT_PATH = (
    ROOT
    / "verification_benchmark/reports/recording_preflight/development/phase3_taobao_search_full_vcr_v1.audit.json"
)
HUMAN_OUTPUT_REF = (
    "verification_benchmark/reports/recording_capability_expansion/development/"
    "phase3_taobao_search_full_vcr_v1"
)


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


def _write_or_compare(path: Path, expected: bytes, *, check: bool) -> None:
    if path.exists():
        actual = path.read_bytes()
        if not hmac.compare_digest(actual, expected):
            raise ValueError(f"deterministic preflight artifact differs: {path}")
        return
    if check:
        raise ValueError(f"deterministic preflight artifact is missing: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(expected)


def _request_for(checker: Any) -> tuple[RecordedProviderKind, Any]:
    if checker.checker_id == "ocr":
        return RecordedProviderKind.OCR, RecordedOcrRequestIR()
    if checker.checker_id == "llm":
        prompt = checker.parameters.get("prompt")
        return (
            RecordedProviderKind.LLM,
            RecordedLlmRequestIR(prompt, LLM_PROMPT_TEMPLATE_VERSION),
        )
    raise ValueError("exact VCR inventory accepts only OCR/LLM checkers")


def build_exact_inventory(
    root: Path = ROOT,
) -> tuple[RecordingManifest, dict[str, Any]]:
    root = root.resolve()
    batch_manifest = load_batch_replay_manifest(BATCH_MANIFEST_PATH)
    contract_path = root / batch_manifest.contract.source_ref
    adapted = adapt_legacy_yaml(
        contract_path,
        source_ref=batch_manifest.contract.source_ref,
        expected_contract_sha256=batch_manifest.contract.contract_sha256,
    )
    contract = adapted.contract
    if contract.dag is None:
        raise ValueError("exact VCR inventory requires a Legacy DAG Contract")

    bindings = []
    non_vcr_blockers = []
    for node in contract.dag.nodes:
        for checker in node.checkers:
            if checker.checker_id in {"ocr", "llm"}:
                kind, request = _request_for(checker)
                bindings.append(
                    RecordedProviderBindingIR(
                        node.node_id,
                        checker.checker_id,
                        kind,
                        AUTHORIZED_MODEL,
                        request,
                    )
                )
            elif checker.checker_id in {"visual_state", "icons"}:
                non_vcr_blockers.append(
                    {
                        "node_id": node.node_id,
                        "checker_id": checker.checker_id,
                        "condition_operator": node.condition_operator.value,
                        "parameters": {
                            key: list(value) if isinstance(value, tuple) else value
                            for key, value in checker.parameters.items()
                        },
                        "reason": "NO_RECORDED_PROVIDER_KIND_AND_LOCAL_RUNTIME_UNSUPPORTED",
                    }
                )
    plan = RecordedProviderPlan(contract_sha256(contract), tuple(bindings))
    plan.validate_against(contract)

    cache_path = root / batch_manifest.cache.source_ref
    storage = PrecomputedEvidenceStorage.from_jsonl(cache_path)
    if storage.storage_sha256 != batch_manifest.cache.storage_sha256:
        raise ValueError("seed cache storage SHA-256 drifted")

    occurrences: dict[tuple[str, str, str], dict[str, Any]] = {}
    source_frame_count = 0
    for trace_id in batch_manifest.trace_ids:
        trace_root = (
            root / "verification_benchmark" / "traces" / Path(*trace_id.split("/"))
        )
        bundle = load_trace_directory(trace_root, trace_ref=trace_id)
        durable = trace_bundle_to_event_trace(bundle, contract, trace_id=trace_id)
        evidence = load_local_legacy_checker_evidence(durable, trace_root)
        screenshot_refs = {
            frame.frame_index: frame.screenshot_ref for frame in bundle.outcome_frames
        }
        for frame in evidence.frames:
            if frame.screenshot_sha256 is None:
                continue
            screenshot_ref = screenshot_refs.get(frame.frame_index)
            if screenshot_ref is None:
                raise ValueError(
                    "checker evidence screenshot has no immutable source ref"
                )
            screenshot_path = trace_root / screenshot_ref
            if _file_sha256(screenshot_path) != frame.screenshot_sha256:
                raise ValueError("screenshot bytes drifted during exact inventory")
            source_frame_count += 1
            for binding in bindings:
                key = EvidenceCacheKey(
                    frame.screenshot_sha256,
                    AUTHORIZED_MODEL,
                    binding.request_sha256,
                )
                identity = (
                    binding.provider_kind.value,
                    key.screenshot_sha256,
                    key.request_sha256,
                )
                consumer = {
                    "trace_id": trace_id,
                    "frame_index": frame.frame_index,
                    "screenshot_ref": f"{trace_id}/{screenshot_ref}",
                    "node_id": binding.node_id,
                    "checker_id": binding.checker_id,
                }
                current = occurrences.get(identity)
                if current is None:
                    current = {
                        "provider_kind": binding.provider_kind,
                        "request": binding.request,
                        "request_sha256": binding.request_sha256,
                        "screenshot_sha256": frame.screenshot_sha256,
                        "image_bytes": screenshot_path.stat().st_size,
                        "references": [],
                        "consumers": [],
                    }
                    occurrences[identity] = current
                current["references"].append(consumer["screenshot_ref"])
                current["consumers"].append(consumer)

    jobs = []
    job_audit = []
    cached_count = 0
    missing_count = 0
    missing_image_bytes = 0
    for identity, item in sorted(occurrences.items()):
        kind = item["provider_kind"]
        request = item["request"]
        screenshot_ref = min(item["references"])
        job = RecordingJob(
            job_id=(
                f"phase3-{kind.value.casefold()}-{item['screenshot_sha256'][:12]}-"
                f"{item['request_sha256'][:12]}"
            ),
            provider_kind=kind,
            screenshot_ref=screenshot_ref,
            screenshot_sha256=item["screenshot_sha256"],
            request=request,
        )
        jobs.append(job)
        key = EvidenceCacheKey(
            item["screenshot_sha256"], AUTHORIZED_MODEL, item["request_sha256"]
        )
        cached = storage.lookup(kind, key) is not None
        if cached:
            cached_count += 1
        else:
            missing_count += 1
            missing_image_bytes += item["image_bytes"]
        request_payload = job.payload()["request"]
        job_audit.append(
            {
                "job_id": job.job_id,
                "provider_kind": kind.value,
                "screenshot_ref": screenshot_ref,
                "screenshot_sha256": item["screenshot_sha256"],
                "image_bytes": item["image_bytes"],
                "request": request_payload,
                "request_sha256": item["request_sha256"],
                "cache_status": "CACHED" if cached else "MISS",
                "consumers": sorted(
                    item["consumers"],
                    key=lambda value: (
                        value["trace_id"],
                        value["frame_index"],
                        value["node_id"],
                        value["checker_id"],
                    ),
                ),
            }
        )
    manifest = RecordingManifest(AUTHORIZED_MODEL, tuple(jobs))
    manifest.validate()
    manifest_payload = {
        "schema_version": manifest.schema_version,
        "model_version": manifest.model_version,
        "jobs": [job.payload() for job in manifest.jobs],
    }
    manifest_file_sha256 = hashlib.sha256(_json_bytes(manifest_payload)).hexdigest()
    unique_screenshots = {item["screenshot_sha256"] for item in occurrences.values()}
    audit: dict[str, Any] = {
        "schema_version": INVENTORY_SCHEMA_VERSION,
        "generator_version": GENERATOR_VERSION,
        "development_only": True,
        "network_requests": 0,
        "api_key_reads": 0,
        "batch": {
            "source_ref": BATCH_MANIFEST_PATH.relative_to(root).as_posix(),
            "file_sha256": _file_sha256(BATCH_MANIFEST_PATH),
            "manifest_sha256": batch_manifest.manifest_sha256,
            "trace_count": len(batch_manifest.trace_ids),
            "trace_ids": list(batch_manifest.trace_ids),
        },
        "contract": {
            "source_ref": batch_manifest.contract.source_ref,
            "file_sha256": batch_manifest.contract.file_sha256,
            "contract_sha256": batch_manifest.contract.contract_sha256,
        },
        "provider_plan": {
            "provider_plan_sha256": plan.plan_sha256,
            "binding_count": len(plan.bindings),
            "bindings": [binding.payload() for binding in plan.bindings],
        },
        "seed_cache": {
            "source_ref": batch_manifest.cache.source_ref,
            "file_sha256": batch_manifest.cache.file_sha256,
            "storage_sha256": storage.storage_sha256,
            "entry_count": storage.entry_count,
            "must_be_copied_before_resume": True,
        },
        "recording_manifest": {
            "source_ref": OUTPUT_MANIFEST_PATH.relative_to(root).as_posix(),
            "manifest_sha256": manifest.manifest_sha256,
            "file_sha256": manifest_file_sha256,
            "model_version": AUTHORIZED_MODEL,
            "authorized_base_url": AUTHORIZED_BASE_URL,
        },
        "inventory": {
            "source_frame_occurrence_count": source_frame_count,
            "unique_screenshot_count": len(unique_screenshots),
            "provider_lookup_occurrence_count": source_frame_count * len(bindings),
            "unique_composite_key_count": len(jobs),
            "cached_composite_key_count": cached_count,
            "missing_composite_key_count": missing_count,
            "duplicate_elimination_count": source_frame_count * len(bindings)
            - len(jobs),
            "missing_request_image_bytes": missing_image_bytes,
            "jobs": job_audit,
        },
        "execution_policy": {
            "execution_owner": "HUMAN_ONLY",
            "human_execution_authorized_by_user": True,
            "ai_network_execution_forbidden": True,
            "max_attempts": 1,
            "request_budget": missing_count,
            "worst_case_requests_after_seed_copy": missing_count,
            "output_cache_ref": f"{HUMAN_OUTPUT_REF}/cache.jsonl",
            "receipt_ref": f"{HUMAN_OUTPUT_REF}/receipt.json",
        },
        "non_vcr_blockers": non_vcr_blockers,
        "claim_boundary": {
            "exact_scope": (
                "Every unique OCR/LLM composite lookup key reachable for every screenshot "
                "frame in the frozen eight-trace batch."
            ),
            "post_recording_coverage": "NOT_CLAIMED",
            "unsupported_clearance": "NOT_GUARANTEED_DUE_TO_NON_VCR_BLOCKERS",
            "agreement_rate": None,
            "accuracy": None,
            "heldout_performance_claimed": False,
        },
    }
    audit["audit_sha256"] = _digest(audit)
    return manifest, audit


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    manifest, audit = build_exact_inventory(ROOT)
    manifest_payload = {
        "schema_version": manifest.schema_version,
        "model_version": manifest.model_version,
        "jobs": [job.payload() for job in manifest.jobs],
    }
    _write_or_compare(
        OUTPUT_MANIFEST_PATH, _json_bytes(manifest_payload), check=args.check
    )
    _write_or_compare(OUTPUT_AUDIT_PATH, _json_bytes(audit), check=args.check)
    print(
        json.dumps(
            {
                "status": "PREFLIGHT_ONLY_NO_NETWORK",
                "manifest_sha256": manifest.manifest_sha256,
                "unique_jobs": len(manifest.jobs),
                "cached": audit["inventory"]["cached_composite_key_count"],
                "missing": audit["inventory"]["missing_composite_key_count"],
                "request_budget": audit["execution_policy"]["request_budget"],
                "non_vcr_blockers": len(audit["non_vcr_blockers"]),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
