#!/usr/bin/env python3
"""Freeze the second-reviewed semireal labels and balanced challenge v1.

This is a one-shot, no-overwrite data-freeze operation.  It validates every
derived trace checksum and does not import or execute a verifier.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
BENCHMARK = ROOT / "verification_benchmark"
TRACE_ROOT = BENCHMARK / "traces" / "heldout_challenge_v1" / "semireal"
DRAFT_LABELS = BENCHMARK / "labels_cross_app_heldout_challenge_v1_semireal_draft.jsonl"
REAL_LABELS = BENCHMARK / "labels_cross_app_heldout_challenge_v1_real.jsonl"
REAL_MANIFEST = BENCHMARK / "frozen" / "heldout_challenge_v1_real_data_manifest.json"
FINAL_LABELS = BENCHMARK / "labels_cross_app_heldout_challenge_v1_semireal.jsonl"
CHALLENGE_LABELS = BENCHMARK / "labels_cross_app_heldout_challenge_v1_challenge.jsonl"
FINAL_MANIFEST = BENCHMARK / "frozen" / "heldout_challenge_v1_challenge_data_manifest.json"


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows), encoding="utf-8")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def trace_digest(path: Path) -> tuple[str, int, int]:
    digest = hashlib.sha256()
    files = sorted(item for item in path.rglob("*") if item.is_file())
    total = 0
    for item in files:
        relative = item.relative_to(path).as_posix()
        item_hash = file_sha256(item)
        size = item.stat().st_size
        digest.update(f"{relative}\0{item_hash}\0{size}\n".encode("utf-8"))
        total += size
    return digest.hexdigest(), len(files), total


def assert_counts(rows: list[dict], expected: dict[tuple, int], keys: tuple[str, ...], name: str) -> None:
    actual = Counter(tuple(row.get(key) for key in keys) for row in rows)
    if actual != Counter(expected):
        raise SystemExit(f"{name} count mismatch: expected={dict(expected)}, actual={dict(actual)}")


def main() -> int:
    for output in (FINAL_LABELS, CHALLENGE_LABELS, FINAL_MANIFEST):
        if output.exists():
            raise SystemExit(f"refusing to overwrite frozen output: {output}")
    for source in (DRAFT_LABELS, REAL_LABELS, REAL_MANIFEST):
        if not source.is_file():
            raise SystemExit(f"required freeze input missing: {source}")

    draft = load_jsonl(DRAFT_LABELS)
    real = load_jsonl(REAL_LABELS)
    assert_counts(draft, {(failure,): count for failure, count in {
        "wrong_entity": 3, "wrong_terminal_page": 3, "blocking_popup": 3,
        "partial_completion": 4, "loading_final_state": 4, "left_success_state": 4,
    }.items()}, ("failure_type",), "semireal failure taxonomy")
    assert_counts(real, {("challenge_v1", "success"): 24, ("challenge_v1", "fail"): 3,
                         ("held_out_v1", "success"): 21}, ("split", "ground_truth"), "real split")

    if any(row.get("label_status") != "pending_second_review" for row in draft):
        raise SystemExit("all draft labels must still be pending_second_review")
    if any(row.get("origin") != "semireal" or row.get("ground_truth") != "fail" for row in draft):
        raise SystemExit("semireal draft contains an unexpected origin or Ground Truth")
    if any(row.get("label_status") != "frozen" for row in real):
        raise SystemExit("real labels are not fully frozen")

    trace_entries = []
    for row in draft:
        trace_path = BENCHMARK / "traces" / row["trace_id"]
        digest, file_count, byte_count = trace_digest(trace_path)
        if digest != row.get("sha256_tree"):
            raise SystemExit(f"trace checksum mismatch: {row['trace_id']}")
        trace_entries.append({"trace_id": row["trace_id"], "sha256_tree": digest,
                              "file_count": file_count, "bytes": byte_count})

    frozen_at = datetime.now().astimezone().isoformat(timespec="seconds")
    semireal = []
    for source in draft:
        row = dict(source)
        row.update({"label_status": "frozen", "reviewers": ["codex", "user"],
                    "adjudicator": "user", "label_frozen_at": frozen_at})
        semireal.append(row)
    write_jsonl(FINAL_LABELS, semireal)

    real_challenge = [row for row in real if row.get("split") == "challenge_v1"]
    challenge = real_challenge + semireal
    assert_counts(challenge, {("real", "success"): 24, ("real", "fail"): 3,
                              ("semireal", "fail"): 21}, ("origin", "ground_truth"), "challenge")
    write_jsonl(CHALLENGE_LABELS, challenge)

    real_manifest = json.loads(REAL_MANIFEST.read_text(encoding="utf-8"))
    manifest = {
        "freeze_id": "heldout-challenge-v1-ground-truth-freeze",
        "frozen_at": frozen_at,
        "review_confirmation": "user confirmation in experiment conversation on 2026-07-13",
        "verifier_executed_before_freeze": False,
        "enhanced_verifier_tag": "enhanced-development-20260712",
        "enhanced_verifier_commit": "7079cc7c44a53a52a886e2133c632b1cc306570c",
        "real_freeze_id": real_manifest["freeze_id"],
        "counts": {"semireal": 21, "challenge_total": 48, "challenge_success": 24,
                   "challenge_fail": 24, "challenge_real": 27, "challenge_semireal": 21},
        "files": {
            "real_labels": {"path": str(REAL_LABELS.relative_to(ROOT)).replace("\\", "/"),
                            "sha256": file_sha256(REAL_LABELS)},
            "semireal_labels": {"path": str(FINAL_LABELS.relative_to(ROOT)).replace("\\", "/"),
                                "sha256": file_sha256(FINAL_LABELS)},
            "challenge_labels": {"path": str(CHALLENGE_LABELS.relative_to(ROOT)).replace("\\", "/"),
                                 "sha256": file_sha256(CHALLENGE_LABELS)},
        },
        "semireal_traces": trace_entries,
    }
    FINAL_MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"frozen_at": frozen_at, "files": manifest["files"], "counts": manifest["counts"]},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
