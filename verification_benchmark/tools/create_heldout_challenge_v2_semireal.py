#!/usr/bin/env python3
"""Create the provenance-preserving semireal challenge-v2 draft.

This tool derives traces only from frozen, human-confirmed real successes. It
does not call any verifier and refuses to overwrite an existing asset.
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
REAL_LABELS = BENCHMARK / "labels_cross_app_heldout_challenge_v2_real.jsonl"
REAL_MANIFEST = BENCHMARK / "frozen" / "heldout_challenge_v2_real_data_manifest.json"
DEFAULT_DATA_ROOT = Path(r"D:\Lab\MobiAgent-heldout-v2-data")
DEFAULT_LABELS = BENCHMARK / "labels_cross_app_heldout_challenge_v2_semireal_draft.jsonl"
DEFAULT_MANIFEST = BENCHMARK / "frozen" / "heldout_challenge_v2_semireal_draft_manifest.json"


# Five mechanisms, two samples each. Every retained/donated frame was visually
# reviewed before this specification was written.
SPECS = [
    {"id": "wrong_entity_01", "kind": "terminal_swap", "failure_type": "wrong_entity",
     "parent": "real/哔哩哔哩/type4/1", "donor": "real/哔哩哔哩/type4/2"},
    {"id": "wrong_entity_02", "kind": "terminal_swap", "failure_type": "wrong_entity",
     "parent": "real/网易云音乐/type1/2", "donor": "real/网易云音乐/type1/3"},
    {"id": "wrong_terminal_page_01", "kind": "truncate", "failure_type": "wrong_terminal_page",
     "parent": "real/哔哩哔哩/type4/3", "end_frame": 12},
    {"id": "wrong_terminal_page_02", "kind": "truncate", "failure_type": "wrong_terminal_page",
     "parent": "real/网易云音乐/type2/1", "end_frame": 5},
    {"id": "loading_final_state_01", "kind": "truncate", "failure_type": "loading_final_state",
     "parent": "real/网易云音乐/type1/1", "end_frame": 3},
    {"id": "loading_final_state_02", "kind": "truncate", "failure_type": "loading_final_state",
     "parent": "real/高德地图/location_search/2", "end_frame": 2},
    {"id": "partial_completion_01", "kind": "truncate", "failure_type": "partial_completion",
     "parent": "real/网易云音乐/type2/2", "end_frame": 5},
    {"id": "partial_completion_02", "kind": "truncate", "failure_type": "partial_completion",
     "parent": "real/网易云音乐/type2/3", "end_frame": 4},
    {"id": "left_success_state_01", "kind": "leave_success", "failure_type": "left_success_state",
     "parent": "real/哔哩哔哩/type4/2", "leave_frame": 1},
    {"id": "left_success_state_02", "kind": "leave_success", "failure_type": "left_success_state",
     "parent": "real/高德地图/location_search/1", "leave_frame": 1},
]


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sha256(path: Path) -> str:
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
        item_hash = sha256(item)
        size = item.stat().st_size
        total += size
        digest.update(f"{relative}\0{item_hash}\0{size}\n".encode())
    return digest.hexdigest(), len(files), total


def copy_frame(source: Path, source_frame: int, destination: Path, destination_frame: int) -> None:
    for suffix in (".jpg", ".json", ".xml"):
        path = source / f"{source_frame}{suffix}"
        if not path.is_file():
            raise FileNotFoundError(path)
        shutil.copy2(path, destination / f"{destination_frame}{suffix}")


def false_done_action(index: int) -> dict:
    return {"type": "done", "status": "success", "message": "semireal false done", "action_index": index}


def false_done_react(index: int) -> dict:
    return {
        "reasoning": "Semireal challenge: agent incorrectly declares success in this observable state.",
        "function": {"name": "done", "parameters": {"status": "success"}},
        "action_index": index,
    }


def write_metadata(destination: Path, metadata: dict, actions: list, reacts: list) -> None:
    metadata["stop_reason"] = "TASK_COMPLETED_SUCCESS"
    metadata["actions"] = actions
    metadata["action_count"] = len(actions)
    write_json(destination / "actions.json", metadata)
    write_json(destination / "react.json", reacts)


def build_truncate(parent: Path, destination: Path, end_frame: int) -> str:
    metadata = read_json(parent / "actions.json")
    if end_frame >= int(metadata["action_count"]):
        raise ValueError(f"truncate must remove the real terminal frame: {parent} @ {end_frame}")
    actions = metadata["actions"][:end_frame]
    reacts = read_json(parent / "react.json")[:end_frame]
    for frame in range(1, end_frame + 1):
        copy_frame(parent, frame, destination, frame)
    actions[-1] = false_done_action(end_frame)
    reacts[-1] = false_done_react(end_frame)
    write_metadata(destination, metadata, actions, reacts)
    return f"retained parent frames 1..{end_frame}; replaced action/react {end_frame} with false done"


def build_terminal_swap(parent: Path, donor: Path, destination: Path) -> str:
    metadata = read_json(parent / "actions.json")
    actions = metadata["actions"]
    reacts = read_json(parent / "react.json")
    end = int(metadata["action_count"])
    donor_end = int(read_json(donor / "actions.json")["action_count"])
    for frame in range(1, end + 1):
        copy_frame(parent, frame, destination, frame)
    copy_frame(donor, donor_end, destination, end)
    actions[-1] = false_done_action(end)
    reacts[-1] = false_done_react(end)
    write_metadata(destination, metadata, actions, reacts)
    return f"retained parent frames 1..{end - 1}; replaced terminal frame {end} with donor terminal frame {donor_end}"


def build_leave_success(parent: Path, destination: Path, leave_frame: int) -> str:
    metadata = read_json(parent / "actions.json")
    actions = metadata["actions"]
    reacts = read_json(parent / "react.json")
    end = int(metadata["action_count"])
    for frame in range(1, end + 1):
        copy_frame(parent, frame, destination, frame)
    copy_frame(parent, leave_frame, destination, end + 1)
    actions[-1] = {"type": "press_home", "action_index": end}
    reacts[-1] = {
        "reasoning": "Leaves the correct terminal state before evaluation.",
        "function": {"name": "press_home", "parameters": {}},
        "action_index": end,
    }
    actions.append(false_done_action(end + 1))
    reacts.append(false_done_react(end + 1))
    write_metadata(destination, metadata, actions, reacts)
    return f"retained successful frames 1..{end}; appended non-target parent frame {leave_frame} as terminal frame {end + 1}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", default=str(DEFAULT_DATA_ROOT))
    parser.add_argument("--labels", default=str(DEFAULT_LABELS))
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    args = parser.parse_args()
    data_root = Path(args.data_root).resolve()
    destination_root = data_root / "semireal"
    labels_path = Path(args.labels).resolve()
    manifest_path = Path(args.manifest).resolve()
    for path in (destination_root, labels_path, manifest_path):
        if path.exists():
            raise FileExistsError(f"refusing to overwrite challenge asset: {path}")

    real_manifest = read_json(REAL_MANIFEST)
    if real_manifest["human_adjudication"] != {"adjudicator": "user", "confirmed": True}:
        raise RuntimeError("real Ground Truth has not been frozen by the user")
    if sha256(REAL_LABELS) != real_manifest["files"]["frozen_labels"]["sha256"]:
        raise RuntimeError("frozen real labels checksum mismatch")
    real_labels = {
        row["trace_id"]: row
        for row in (json.loads(line) for line in REAL_LABELS.read_text(encoding="utf-8").splitlines() if line.strip())
    }

    created_at = datetime.now().astimezone().isoformat(timespec="seconds")
    rows, entries = [], []
    for spec in SPECS:
        parent_id = spec["parent"]
        parent_label = real_labels[parent_id]
        if parent_label["label_status"] != "frozen" or parent_label["ground_truth"] != "success":
            raise RuntimeError(f"parent is not a frozen real success: {parent_id}")
        parent = data_root / Path(parent_id)
        trace_id = f"semireal/{spec['failure_type']}/{spec['id']}"
        destination = data_root / Path(trace_id)
        destination.mkdir(parents=True, exist_ok=False)
        donor_id = None
        if spec["kind"] == "truncate":
            transformation = build_truncate(parent, destination, int(spec["end_frame"]))
        elif spec["kind"] == "terminal_swap":
            donor_id = spec["donor"]
            donor_label = real_labels[donor_id]
            if donor_label["label_status"] != "frozen" or donor_label["ground_truth"] != "success":
                raise RuntimeError(f"donor is not a frozen real success: {donor_id}")
            transformation = build_terminal_swap(parent, data_root / Path(donor_id), destination)
        elif spec["kind"] == "leave_success":
            transformation = build_leave_success(parent, destination, int(spec["leave_frame"]))
        else:
            raise ValueError(spec["kind"])

        evidence = int(read_json(destination / "actions.json")["action_count"])
        digest, file_count, total_bytes = trace_digest(destination)
        row = {
            **{key: parent_label[key] for key in (
                "benchmark_task_id", "app", "task_type", "task_description", "expected_slots"
            )},
            "trace_id": trace_id,
            "ground_truth": "fail",
            "failure_type": spec["failure_type"],
            "evidence_frames": [evidence],
            "notes": f"Semireal challenge v2 draft; {transformation}.",
            "origin": "semireal",
            "parent_trace_id": parent_id,
            "donor_trace_id": donor_id,
            "transformation": transformation,
            "split": "heldout_challenge_v2_semireal",
            "label_status": "pending_human_adjudication",
            "reviewers": ["codex_visual_review"],
            "adjudicator": None,
            "reviewed_at": created_at,
            "label_frozen_at": None,
            "trace_schema": {
                "source_warnings": [], "action_count": evidence, "react_count": evidence,
                "extra_artifacts": [], "missing_jpg": [], "missing_xml": [],
            },
            "sha256_tree": digest,
        }
        rows.append(row)
        entries.append({
            "trace_id": trace_id, "parent_trace_id": parent_id, "donor_trace_id": donor_id,
            "transformation": transformation, "sha256_tree": digest,
            "file_count": file_count, "bytes": total_bytes,
        })

    labels_path.write_text("".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows), encoding="utf-8")
    counts = {
        failure_type: sum(row["failure_type"] == failure_type for row in rows)
        for failure_type in sorted({row["failure_type"] for row in rows})
    }
    manifest = {
        "status": "pending_human_adjudication",
        "created_at": created_at,
        "real_ground_truth_tag": "heldout-challenge-v2-real-ground-truth-freeze",
        "real_ground_truth_commit": "8287d9aaa7a1f2877b00d7f59a31842ab2dc1d76",
        "enhanced_verifier_tag": "enhanced-v2-20260713",
        "verifier_executed_before_draft": False,
        "count": len(rows),
        "failure_type_counts": counts,
        "storage": {"primary_root": str(data_root), "backup_root": None, "backup_verified": False},
        "labels": {"path": labels_path.relative_to(ROOT).as_posix(), "sha256": sha256(labels_path)},
        "traces": entries,
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    write_json(manifest_path, manifest)
    print(json.dumps({"count": len(rows), "failure_types": counts}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
