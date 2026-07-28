#!/usr/bin/env python3
"""Freeze confirmed v2 semireal and combined Ground Truth before evaluation."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
BENCHMARK = ROOT / "verification_benchmark"
DRAFT = BENCHMARK / "labels_cross_app_heldout_challenge_v2_semireal_draft.jsonl"
REAL = BENCHMARK / "labels_cross_app_heldout_challenge_v2_real.jsonl"
SEMIREAL = BENCHMARK / "labels_cross_app_heldout_challenge_v2_semireal.jsonl"
COMBINED = BENCHMARK / "labels_cross_app_heldout_challenge_v2.jsonl"
DRAFT_MANIFEST = BENCHMARK / "frozen" / "heldout_challenge_v2_semireal_draft_manifest.json"
MANIFEST = BENCHMARK / "frozen" / "heldout_challenge_v2_ground_truth_manifest.json"
AUDIT = BENCHMARK / "reports" / "cross_app_heldout_challenge_v2_semireal_draft_audit" / "runner_batch_audit.json"
REVIEW = BENCHMARK / "reports" / "cross_app_heldout_challenge_v2_semireal_review.md"
SOURCE_ROOT = Path(r"D:\Lab\MobiAgent-heldout-v2-data")
BACKUP_ROOT = Path(r"D:\Lab\MobiAgent-heldout-v2-data-backup-20260713")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tree(path: Path) -> tuple[str, int, int]:
    digest = hashlib.sha256()
    count = total = 0
    for item in sorted(candidate for candidate in path.rglob("*") if candidate.is_file()):
        relative = item.relative_to(path).as_posix()
        item_hash, size = sha256(item), item.stat().st_size
        digest.update(f"{relative}\0{item_hash}\0{size}\n".encode())
        count += 1
        total += size
    return digest.hexdigest(), count, total


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows), encoding="utf-8")


def file_record(path: Path) -> dict:
    return {"path": path.relative_to(ROOT).as_posix(), "sha256": sha256(path)}


def main() -> int:
    for output in (SEMIREAL, COMBINED, MANIFEST):
        if output.exists():
            raise FileExistsError(f"refusing to overwrite frozen asset: {output}")

    draft_manifest = json.loads(DRAFT_MANIFEST.read_text(encoding="utf-8"))
    if draft_manifest["status"] != "pending_human_adjudication":
        raise RuntimeError("unexpected semireal draft status")
    if sha256(DRAFT) != draft_manifest["labels"]["sha256"]:
        raise RuntimeError("semireal draft checksum mismatch")
    if not draft_manifest["storage"]["backup_verified"]:
        raise RuntimeError("semireal backup was not verified")

    real = load_jsonl(REAL)
    rows = load_jsonl(DRAFT)
    if len(real) != 12 or len(rows) != 10:
        raise RuntimeError("unexpected real/semireal row count")
    if (sum(row["ground_truth"] == "success" for row in real), sum(row["ground_truth"] == "fail" for row in real)) != (11, 1):
        raise RuntimeError("unexpected frozen real balance")
    if any(row["ground_truth"] != "fail" or row["label_status"] != "pending_human_adjudication" for row in rows):
        raise RuntimeError("semireal draft is not ten pending failures")
    if {row["origin"] for row in rows} != {"semireal"}:
        raise RuntimeError("semireal origin separation failed")

    frozen_at = datetime.now().astimezone().isoformat(timespec="seconds")
    trace_records = []
    real_ids = {row["trace_id"] for row in real if row["ground_truth"] == "success"}
    for row in rows:
        if row["parent_trace_id"] not in real_ids:
            raise RuntimeError(f"parent is not a frozen real success: {row['trace_id']}")
        if row.get("donor_trace_id") and row["donor_trace_id"] not in real_ids:
            raise RuntimeError(f"donor is not a frozen real success: {row['trace_id']}")
        source = SOURCE_ROOT / Path(row["trace_id"])
        backup = BACKUP_ROOT / Path(row["trace_id"])
        source_tree = tree(source)
        backup_tree = tree(backup)
        if source_tree != backup_tree or source_tree[0] != row["sha256_tree"]:
            raise RuntimeError(f"trace checksum mismatch: {row['trace_id']}")
        trace_records.append({
            "trace_id": row["trace_id"], "parent_trace_id": row["parent_trace_id"],
            "donor_trace_id": row.get("donor_trace_id"), "transformation": row["transformation"],
            "source": str(source), "backup": str(backup), "sha256_tree": source_tree[0],
            "file_count": source_tree[1], "bytes": source_tree[2], "backup_verified": True,
        })
        row["label_status"] = "frozen"
        row["reviewers"] = ["codex_visual_review", "user"]
        row["adjudicator"] = "user"
        row["label_frozen_at"] = frozen_at

    write_jsonl(SEMIREAL, rows)
    write_jsonl(COMBINED, real + rows)
    manifest = {
        "freeze_id": "heldout-challenge-v2-ground-truth-freeze",
        "frozen_at": frozen_at,
        "draft_review_commit": "da76bfb70562f115b93541489b4e405962baf9b8",
        "real_ground_truth_tag": "heldout-challenge-v2-real-ground-truth-freeze",
        "enhanced_verifier_tag": "enhanced-v2-20260713",
        "enhanced_verifier_commit": "652ec29aa0708feb5d56364b9cdf0f4d45bc233b",
        "verifier_executed_before_freeze": False,
        "human_adjudication": {"adjudicator": "user", "confirmed": True},
        "counts": {
            "combined": 22, "success": 11, "fail": 11,
            "real": {"total": 12, "success": 11, "fail": 1},
            "semireal": {"total": 10, "success": 0, "fail": 10},
        },
        "files": {
            "real_labels": file_record(REAL), "semireal_draft_labels": file_record(DRAFT),
            "semireal_labels": file_record(SEMIREAL), "combined_labels": file_record(COMBINED),
            "draft_manifest": file_record(DRAFT_MANIFEST), "schema_audit": file_record(AUDIT),
            "human_review": file_record(REVIEW),
        },
        "storage": {
            "primary_root": str(SOURCE_ROOT), "backup_root": str(BACKUP_ROOT),
            "copies": 2, "all_semireal_trace_copies_verified": True,
        },
        "traces": trace_records,
    }
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest["counts"], ensure_ascii=False))
    print(f"semireal_sha256={manifest['files']['semireal_labels']['sha256']}")
    print(f"combined_sha256={manifest['files']['combined_labels']['sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
