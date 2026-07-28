"""Materialize the deterministic visual-state cache for development replay."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
from pathlib import Path, PurePosixPath

from verification_benchmark.evaluation_framework.batch_replay_alignment import (
    load_batch_replay_manifest,
)
from verification_benchmark.evaluation_framework.contract_router import (
    route_explicit_legacy,
)
from verification_benchmark.evaluation_framework.event_log import (
    trace_bundle_to_event_trace,
)
from verification_benchmark.evaluation_framework.legacy_checker_acquisition import (
    load_local_legacy_checker_evidence,
)
from verification_benchmark.evaluation_framework.legacy_yaml_adapter import (
    adapt_legacy_yaml,
)
from verification_benchmark.evaluation_framework.trace_adapter import (
    load_trace_directory,
)
from verification_benchmark.evaluation_framework.visual_state_evidence_cache import (
    VISUAL_STATE_DETECTOR_VERSION,
    VisualStateCacheEntry,
    VisualStateCacheKey,
    VisualStateEvidenceStorage,
    evaluate_visual_state_image,
    visual_state_cache_jsonl_bytes,
    visual_state_provider_plan,
)


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SOURCE_MANIFEST = (
    ROOT
    / "verification_benchmark/batch_manifests/development/phase3_taobao_search_replay_alignment_second_pass_v1.json"
)
DEFAULT_OUTPUT = (
    ROOT
    / "verification_benchmark/reports/visual_state_capability_expansion/development/phase3_taobao_search_visual_state_v1"
)
ENHANCED_V2_TAG = "enhanced-v2-20260713"
ENHANCED_V2_COMMIT = "652ec29aa0708feb5d56364b9cdf0f4d45bc233b"
ENHANCED_V2_SOURCE_REF = "MobiFlow/avdag/conditions.py:VisualStateChecker"
ENHANCED_V2_SOURCE_FILE_SHA256 = (
    "7f21dc09d8c69aaa9c9d1b70b2cef950294dc449bfb5b1e40eeb326f5fe86528"
)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _write_or_compare(path: Path, expected: bytes, *, check: bool) -> None:
    if path.exists():
        try:
            actual = path.read_bytes()
        except OSError as exc:
            raise ValueError(
                f"existing visual-state artifact is unreadable: {path}"
            ) from exc
        if not hmac.compare_digest(actual, expected):
            raise ValueError(f"existing visual-state artifact differs: {path}")
        return
    if check:
        raise ValueError(f"required visual-state artifact is missing: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(expected)


def materialize(repo_root: Path, source_manifest: Path) -> tuple[bytes, bytes]:
    root = repo_root.resolve()
    manifest = load_batch_replay_manifest(source_manifest)
    contract_path = root / PurePosixPath(manifest.contract.source_ref)
    adapted = adapt_legacy_yaml(
        contract_path,
        source_ref=manifest.contract.source_ref,
        expected_contract_sha256=manifest.contract.contract_sha256,
    )
    contract = route_explicit_legacy(adapted).contract
    plan = visual_state_provider_plan(contract)
    bindings = plan.bindings
    if len(bindings) != 1:
        raise ValueError(
            "phase3 visual-state materialization requires exactly one binding"
        )
    binding = bindings[0]

    image_paths: dict[str, Path] = {}
    source_gaps = []
    for trace_id in manifest.trace_ids:
        trace_root = (
            root / "verification_benchmark" / "traces" / PurePosixPath(trace_id)
        )
        bundle = load_trace_directory(trace_root, trace_ref=trace_id)
        durable = trace_bundle_to_event_trace(bundle, contract, trace_id=trace_id)
        evidence = load_local_legacy_checker_evidence(durable, trace_root)
        frame_refs = {
            item.frame_index: item.screenshot_ref for item in bundle.outcome_frames
        }
        for frame in evidence.frames:
            if frame.screenshot_sha256 is None:
                source_gaps.append(
                    {
                        "trace_id": trace_id,
                        "frame_index": frame.frame_index,
                        "missing_evidence": "SCREENSHOT",
                    }
                )
                continue
            reference = frame_refs.get(frame.frame_index)
            if reference is None:
                raise ValueError("screenshot hash has no source reference")
            path = (trace_root / reference).resolve()
            try:
                path.relative_to(trace_root.resolve())
            except ValueError as exc:
                raise ValueError("screenshot reference escapes trace root") from exc
            actual = _sha256_bytes(path.read_bytes())
            if actual != frame.screenshot_sha256:
                raise ValueError(
                    "screenshot source hash changed during materialization"
                )
            previous = image_paths.setdefault(actual, path)
            if _sha256_bytes(previous.read_bytes()) != actual:
                raise ValueError("deduplicated screenshot identity is inconsistent")

    entries = tuple(
        VisualStateCacheEntry(
            VisualStateCacheKey(
                screenshot_sha256,
                binding.detector_version,
                binding.request_sha256,
            ),
            evaluate_visual_state_image(path, binding.request),
        )
        for screenshot_sha256, path in sorted(image_paths.items())
    )
    storage = VisualStateEvidenceStorage(entries)
    cache_bytes = visual_state_cache_jsonl_bytes(storage)
    counts = {
        decision: sum(entry.output.decision.value == decision for entry in entries)
        for decision in ("LOADED_CONTENT", "LOADING_SKELETON")
    }
    receipt = {
        "schema_version": "harmony-eval-visual-state-materialization-receipt-v1",
        "source_batch_manifest": {
            "source_ref": source_manifest.resolve().relative_to(root).as_posix(),
            "manifest_sha256": manifest.manifest_sha256,
        },
        "provider_plan_sha256": plan.plan_sha256,
        "detector": {
            "detector_version": VISUAL_STATE_DETECTOR_VERSION,
            "frozen_source_tag": ENHANCED_V2_TAG,
            "frozen_source_commit": ENHANCED_V2_COMMIT,
            "frozen_source_ref": ENHANCED_V2_SOURCE_REF,
            "frozen_source_file_sha256": ENHANCED_V2_SOURCE_FILE_SHA256,
            "request_sha256": binding.request_sha256,
            "request": binding.request.payload(),
        },
        "cache": {
            "entry_count": storage.entry_count,
            "unique_screenshot_count": len(image_paths),
            "decision_counts": counts,
            "storage_sha256": storage.storage_sha256,
            "file_sha256": _sha256_bytes(cache_bytes),
        },
        "source_evidence_gaps": sorted(
            source_gaps, key=lambda item: (item["trace_id"], item["frame_index"])
        ),
        "safety_boundary": {
            "network_used": False,
            "api_key_read": False,
            "main_replay_pixel_decoding_allowed": False,
            "main_replay_cache_lookup_only": True,
        },
    }
    return cache_bytes, _canonical_json_bytes(receipt)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, default=ROOT)
    parser.add_argument("--source-manifest", type=Path, default=DEFAULT_SOURCE_MANIFEST)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    cache_bytes, receipt_bytes = materialize(
        args.repo_root.resolve(), args.source_manifest.resolve()
    )
    _write_or_compare(args.output_dir / "cache.jsonl", cache_bytes, check=args.check)
    _write_or_compare(args.output_dir / "receipt.json", receipt_bytes, check=args.check)
    receipt = json.loads(receipt_bytes)
    print(
        f"visual-state cache: {receipt['cache']['entry_count']} entries, "
        f"storage_sha256={receipt['cache']['storage_sha256']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
