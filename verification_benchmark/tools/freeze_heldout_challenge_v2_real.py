#!/usr/bin/env python3
"""Freeze adjudicated held-out v2 real labels and external trace checksums.

This one-shot tool does not import raw traces and does not call any verifier.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
BENCHMARK = ROOT / "verification_benchmark"
DEFAULT_DRAFT = BENCHMARK / "labels_cross_app_heldout_challenge_v2_real_draft.jsonl"
DEFAULT_LABELS = BENCHMARK / "labels_cross_app_heldout_challenge_v2_real.jsonl"
DEFAULT_MANIFEST = BENCHMARK / "frozen" / "heldout_challenge_v2_real_data_manifest.json"
DEFAULT_SOURCE_ROOT = Path(r"D:\Lab\MobiAgent-heldout-v2-data")
DEFAULT_BACKUP_ROOT = Path(r"D:\Lab\MobiAgent-heldout-v2-data-backup-20260713")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def trace_digest(path: Path) -> tuple[str, int, int, list[dict[str, object]]]:
    digest = hashlib.sha256()
    entries = []
    total_bytes = 0
    for item in sorted(candidate for candidate in path.rglob("*") if candidate.is_file()):
        relative = item.relative_to(path).as_posix()
        item_hash = sha256(item)
        size = item.stat().st_size
        total_bytes += size
        digest.update(f"{relative}\0{item_hash}\0{size}\n".encode("utf-8"))
        entries.append({"path": relative, "sha256": item_hash, "bytes": size})
    return digest.hexdigest(), len(entries), total_bytes, entries


def load_jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--draft", default=str(DEFAULT_DRAFT))
    parser.add_argument("--labels", default=str(DEFAULT_LABELS))
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    parser.add_argument("--source-root", default=str(DEFAULT_SOURCE_ROOT))
    parser.add_argument("--backup-root", default=str(DEFAULT_BACKUP_ROOT))
    args = parser.parse_args()

    draft = Path(args.draft).resolve()
    labels = Path(args.labels).resolve()
    manifest = Path(args.manifest).resolve()
    source_root = Path(args.source_root).resolve()
    backup_root = Path(args.backup_root).resolve()
    for output in (labels, manifest):
        if output.exists():
            raise FileExistsError(f"refusing to overwrite frozen asset: {output}")

    rows = load_jsonl(draft)
    if len(rows) != 12:
        raise RuntimeError(f"expected 12 real rows, found {len(rows)}")
    if sum(row["ground_truth"] == "success" for row in rows) != 11:
        raise RuntimeError("expected 11 adjudicated successes")
    if sum(row["ground_truth"] == "fail" for row in rows) != 1:
        raise RuntimeError("expected one adjudicated natural failure")
    if any(row.get("label_status") != "pending_human_adjudication" for row in rows):
        raise RuntimeError("draft contains a row outside pending_human_adjudication")

    frozen_at = datetime.now().astimezone().isoformat(timespec="seconds")
    traces = []
    for row in rows:
        trace_id = str(row["trace_id"])
        source = source_root / trace_id
        backup = backup_root / trace_id
        if not source.is_dir() or not backup.is_dir():
            raise FileNotFoundError(f"missing source or backup for {trace_id}")
        source_tree, file_count, byte_count, files = trace_digest(source)
        backup_tree, backup_files, backup_bytes, _ = trace_digest(backup)
        if (source_tree, file_count, byte_count) != (backup_tree, backup_files, backup_bytes):
            raise RuntimeError(f"backup mismatch: {trace_id}")
        traces.append({
            "trace_id": trace_id,
            "source": str(source),
            "backup": str(backup),
            "sha256_tree": source_tree,
            "file_count": file_count,
            "bytes": byte_count,
            "backup_verified": True,
            "files": files,
        })
        row["label_status"] = "frozen"
        row["reviewers"] = ["codex_visual_review", "user"]
        row["adjudicator"] = "user"
        row["label_frozen_at"] = frozen_at

    labels.write_text(
        "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )
    task_config = BENCHMARK / "configs" / "runner_cross_app_heldout_challenge_v2.json"
    audit = BENCHMARK / "reports" / "cross_app_heldout_challenge_v2_schema_audit" / "runner_batch_audit.json"
    output = {
        "freeze_id": "heldout-challenge-v2-real-ground-truth-freeze",
        "frozen_at": frozen_at,
        "collection_tag": "heldout-challenge-v2-collection-freeze",
        "collection_commit": "dbfc1d7d645f767136b7bd1c0fffd41568902e79",
        "draft_review_commit": "84e9df4",
        "enhanced_verifier_tag": "enhanced-v2-20260713",
        "enhanced_verifier_commit": "652ec29aa0708feb5d56364b9cdf0f4d45bc233b",
        "verifier_executed_before_freeze": False,
        "human_adjudication": {"adjudicator": "user", "confirmed": True},
        "counts": {"total": 12, "success": 11, "fail": 1, "real": 12},
        "files": {
            "task_config": {"path": task_config.relative_to(ROOT).as_posix(), "sha256": sha256(task_config)},
            "draft_labels": {"path": draft.relative_to(ROOT).as_posix(), "sha256": sha256(draft)},
            "frozen_labels": {"path": labels.relative_to(ROOT).as_posix(), "sha256": sha256(labels)},
            "schema_audit": {"path": audit.relative_to(ROOT).as_posix(), "sha256": sha256(audit)},
        },
        "storage": {
            "primary_root": str(source_root),
            "backup_root": str(backup_root),
            "copies": 2,
            "all_trace_copies_verified": True,
        },
        "traces": traces,
    }
    manifest.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(output["counts"], ensure_ascii=False))
    print(f"labels_sha256={output['files']['frozen_labels']['sha256']}")
    print(f"traces={len(traces)} files={sum(item['file_count'] for item in traces)} bytes={sum(item['bytes'] for item in traces)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
