#!/usr/bin/env python3
"""Freeze reviewed real held-out v1 Runner traces and Ground Truth.

This is a one-shot, no-overwrite importer.  It does not call any verifier.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
BENCHMARK = ROOT / "verification_benchmark"
DEFAULT_SOURCE = BENCHMARK / "runner_outputs_cross_app_heldout_challenge_v1"
DEFAULT_TRACE_ROOT = BENCHMARK / "traces" / "heldout_challenge_v1" / "real"
DEFAULT_LABELS = BENCHMARK / "labels_cross_app_heldout_challenge_v1_real.jsonl"
DEFAULT_MANIFEST = BENCHMARK / "frozen" / "heldout_challenge_v1_real_data_manifest.json"


GROUPS = {
    ("哔哩哔哩", "type1"): ("bilibili", "search", "bilibili_search"),
    ("哔哩哔哩", "type4"): ("bilibili", "creator_homepage", "bilibili_creator_homepage_original"),
    ("小红书", "type4"): ("xiaohongshu", "search", "xiaohongshu_search_original"),
    ("小红书", "type2"): ("xiaohongshu", "creator_homepage", "xiaohongshu_creator_homepage_original"),
    ("网易云音乐", "type1"): ("cloudmusic", "search", "cloudmusic_search_original"),
    ("网易云音乐", "type2"): ("cloudmusic", "play", "cloudmusic_play_original"),
    ("高德地图", "location_search"): ("gaode", "location_search", "gaode_location_search_frozen_unmapped"),
    ("高德地图", "route_preview"): ("gaode", "route_preview", "gaode_route_preview_frozen_unmapped"),
}

# Twenty-four successful controls plus all three natural failures form the real
# component of challenge_v1.  The remaining 21 real successes are held_out_v1.
CHALLENGE_SUCCESS = {
    ("哔哩哔哩", "type1"): {1, 2, 3},
    ("哔哩哔哩", "type4"): {1, 2, 3},
    ("小红书", "type4"): {1, 2, 3},
    ("小红书", "type2"): {1, 2, 3},
    ("网易云音乐", "type1"): {1, 2, 3},
    ("网易云音乐", "type2"): {1, 2, 3},
    ("高德地图", "location_search"): {1, 2, 3},
    ("高德地图", "route_preview"): {1, 2, 4},
}

FAILURES = {
    ("哔哩哔哩", "type1", 5): (
        "wrong_entity",
        "目标为水下考古；终态搜索编辑框为接力赛，键盘仍打开且没有目标结果页。",
    ),
    ("哔哩哔哩", "type4", 6): (
        "wrong_terminal_page",
        "目标为老蒋巨靠谱主页；终态是B站推荐首页，没有目标身份或创作者主页分区。",
    ),
    ("高德地图", "route_preview", 3): (
        "blocking_popup",
        "终态仍为武康大楼地点详情并被华为登录弹窗阻断，没有人民广场到武康大楼的路线预览。",
    ),
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def trace_digest(path: Path) -> tuple[str, int, int]:
    digest = hashlib.sha256()
    files = sorted(item for item in path.rglob("*") if item.is_file())
    total_bytes = 0
    for item in files:
        relative = item.relative_to(path).as_posix()
        item_hash = sha256(item)
        size = item.stat().st_size
        total_bytes += size
        digest.update(f"{relative}\0{item_hash}\0{size}\n".encode("utf-8"))
    return digest.hexdigest(), len(files), total_bytes


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default=str(DEFAULT_SOURCE))
    parser.add_argument("--trace-root", default=str(DEFAULT_TRACE_ROOT))
    parser.add_argument("--labels", default=str(DEFAULT_LABELS))
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    args = parser.parse_args()

    source = Path(args.source).resolve()
    trace_root = Path(args.trace_root).resolve()
    labels_path = Path(args.labels).resolve()
    manifest_path = Path(args.manifest).resolve()
    for destination in (trace_root, labels_path, manifest_path):
        if destination.exists():
            raise FileExistsError(f"refusing to overwrite frozen asset: {destination}")
    if not source.is_relative_to(BENCHMARK.resolve()):
        raise RuntimeError(f"source outside benchmark root: {source}")
    if not trace_root.is_relative_to((BENCHMARK / "traces").resolve()):
        raise RuntimeError(f"trace destination outside benchmark traces: {trace_root}")

    reviewed_at = datetime.now().astimezone().isoformat(timespec="seconds")
    rows = []
    trace_entries = []
    for (runner_app, runner_type), (app, task_type, benchmark_task_id) in GROUPS.items():
        group = source / runner_app / runner_type
        task_dirs = sorted((item for item in group.iterdir() if item.is_dir()), key=lambda p: int(p.name))
        if [int(item.name) for item in task_dirs] != list(range(1, 7)):
            raise RuntimeError(f"expected task directories 1..6: {group}")
        for source_trace in task_dirs:
            index = int(source_trace.name)
            actions = load_json(source_trace / "actions.json")
            reacts = load_json(source_trace / "react.json")
            action_count = int(actions["action_count"])
            if len(actions.get("actions", [])) != action_count or len(reacts) != action_count:
                raise RuntimeError(f"action/react mismatch: {source_trace}")
            for suffix in (".jpg", ".json", ".xml"):
                if not (source_trace / f"{action_count}{suffix}").is_file():
                    raise RuntimeError(f"missing terminal {suffix}: {source_trace}")

            trace_id = f"heldout_challenge_v1/real/{app}/{task_type}/{index:02d}"
            destination = BENCHMARK / "traces" / trace_id
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(source_trace, destination)

            failure = FAILURES.get((runner_app, runner_type, index))
            ground_truth = "fail" if failure else "success"
            split = (
                "challenge_v1"
                if failure or index in CHALLENGE_SUCCESS[(runner_app, runner_type)]
                else "held_out_v1"
            )
            notes = failure[1] if failure else "终态截图与XML共同确认目标实体和所要求的已加载页面状态。"
            rows.append({
                "trace_id": trace_id,
                "benchmark_task_id": benchmark_task_id,
                "app": app,
                "task_type": task_type,
                "task_description": actions["task_description"],
                "ground_truth": ground_truth,
                "failure_type": failure[0] if failure else None,
                "expected_slots": {"target_state": task_type},
                "evidence_frames": [action_count],
                "notes": notes,
                "origin": "real",
                "parent_trace_id": None,
                "transformation": None,
                "split": split,
                "label_status": "frozen",
                "reviewers": ["codex", "user"],
                "adjudicator": "user",
                "reviewed_at": reviewed_at,
                "label_frozen_at": reviewed_at,
                "trace_schema": {
                    "source_warnings": [],
                    "action_count": action_count,
                    "react_count": len(reacts),
                    "extra_artifacts": [],
                    "missing_jpg": [],
                    "missing_xml": [],
                },
            })
            digest, file_count, total_bytes = trace_digest(destination)
            trace_entries.append({
                "trace_id": trace_id,
                "source": source_trace.relative_to(ROOT).as_posix(),
                "sha256_tree": digest,
                "file_count": file_count,
                "bytes": total_bytes,
            })

    if len(rows) != 48:
        raise RuntimeError(f"expected 48 labels, found {len(rows)}")
    labels_path.parent.mkdir(parents=True, exist_ok=True)
    labels_path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )
    config = BENCHMARK / "configs" / "runner_cross_app_heldout_challenge_v1.json"
    manifest = {
        "freeze_id": "heldout-challenge-v1-real-ground-truth-freeze",
        "frozen_at": reviewed_at,
        "collection_commit": "266306de4b910dc2c19e144c803232fdcda8b3bf",
        "collection_tag": "heldout-challenge-v1.3-collection-freeze",
        "enhanced_verifier_tag": "enhanced-development-20260712",
        "verifier_executed_before_freeze": False,
        "counts": {
            "total": 48,
            "success": sum(row["ground_truth"] == "success" for row in rows),
            "fail": sum(row["ground_truth"] == "fail" for row in rows),
            "held_out_v1": sum(row["split"] == "held_out_v1" for row in rows),
            "challenge_v1_real": sum(row["split"] == "challenge_v1" for row in rows),
        },
        "files": {
            "task_config": {"path": config.relative_to(ROOT).as_posix(), "sha256": sha256(config)},
            "labels": {"path": labels_path.relative_to(ROOT).as_posix(), "sha256": sha256(labels_path)},
        },
        "traces": trace_entries,
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest["counts"], ensure_ascii=False))
    print(f"labels_sha256={manifest['files']['labels']['sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
