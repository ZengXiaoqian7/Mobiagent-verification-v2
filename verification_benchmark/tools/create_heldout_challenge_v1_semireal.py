#!/usr/bin/env python3
"""Create the reviewed-frame semireal component of challenge v1.

The tool never modifies real traces.  Every derived trace records its parent,
optional donor, exact frame transformation, and deterministic tree checksum.
It does not call a verifier.
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
REAL_ROOT = BENCHMARK / "traces" / "heldout_challenge_v1" / "real"
DEST_ROOT = BENCHMARK / "traces" / "heldout_challenge_v1" / "semireal"
REAL_LABELS = BENCHMARK / "labels_cross_app_heldout_challenge_v1_real.jsonl"
DRAFT_LABELS = BENCHMARK / "labels_cross_app_heldout_challenge_v1_semireal_draft.jsonl"
DRAFT_MANIFEST = BENCHMARK / "frozen" / "heldout_challenge_v1_semireal_draft_manifest.json"


SPECS = [
    # Wrong entity: preserve the parent's process but replace the observable
    # terminal state with another reviewed real target in the same App/family.
    {"id": "wrong_entity_01", "kind": "terminal_swap", "failure_type": "wrong_entity",
     "parent": "bilibili/search/01", "donor": "bilibili/search/02"},
    {"id": "wrong_entity_02", "kind": "terminal_swap", "failure_type": "wrong_entity",
     "parent": "xiaohongshu/search/01", "donor": "xiaohongshu/search/02"},
    {"id": "wrong_entity_03", "kind": "terminal_swap", "failure_type": "wrong_entity",
     "parent": "cloudmusic/search/01", "donor": "cloudmusic/search/02"},

    # Correct entity is visible in a search-results state, but the required
    # profile/player terminal page has not been reached.
    {"id": "wrong_terminal_page_01", "kind": "truncate", "failure_type": "wrong_terminal_page",
     "parent": "bilibili/creator_homepage/01", "end_frame": 3},
    {"id": "wrong_terminal_page_02", "kind": "truncate", "failure_type": "wrong_terminal_page",
     "parent": "xiaohongshu/creator_homepage/01", "end_frame": 4},
    {"id": "wrong_terminal_page_03", "kind": "truncate", "failure_type": "wrong_terminal_page",
     "parent": "cloudmusic/play/01", "end_frame": 5},

    # Blocking UI states are all preserved real frames: two Gaode overlays and
    # one Bilibili login gate donated by another reviewed real trace.
    {"id": "blocking_popup_01", "kind": "truncate", "failure_type": "blocking_popup",
     "parent": "gaode/location_search/01", "end_frame": 1},
    {"id": "blocking_popup_02", "kind": "truncate", "failure_type": "blocking_popup",
     "parent": "gaode/route_preview/01", "end_frame": 3},
    {"id": "blocking_popup_03", "kind": "terminal_frame_donor", "failure_type": "blocking_popup",
     "parent": "bilibili/creator_homepage/02", "donor": "bilibili/creator_homepage/06", "donor_frame": 9},

    # Partial process states: target query/result evidence exists but one or
    # more required steps remain.
    {"id": "partial_completion_01", "kind": "truncate", "failure_type": "partial_completion",
     "parent": "bilibili/search/01", "end_frame": 2},
    {"id": "partial_completion_02", "kind": "truncate", "failure_type": "partial_completion",
     "parent": "xiaohongshu/creator_homepage/02", "end_frame": 4},
    {"id": "partial_completion_03", "kind": "truncate", "failure_type": "partial_completion",
     "parent": "cloudmusic/play/02", "end_frame": 5},
    {"id": "partial_completion_04", "kind": "truncate", "failure_type": "partial_completion",
     "parent": "gaode/route_preview/02", "end_frame": 7},

    # Loading frames are directly preserved from four Apps.
    {"id": "loading_01", "kind": "truncate", "failure_type": "loading_final_state",
     "parent": "bilibili/creator_homepage/01", "end_frame": 2},
    {"id": "loading_02", "kind": "truncate", "failure_type": "loading_final_state",
     "parent": "xiaohongshu/search/01", "end_frame": 3},
    {"id": "loading_03", "kind": "truncate", "failure_type": "loading_final_state",
     "parent": "cloudmusic/search/01", "end_frame": 4},
    {"id": "loading_04", "kind": "truncate", "failure_type": "loading_final_state",
     "parent": "gaode/location_search/01", "end_frame": 5},

    # A correct terminal frame is retained as process evidence, followed by a
    # real earlier home/non-target frame from the same parent.
    {"id": "left_success_state_01", "kind": "leave_success", "failure_type": "left_success_state",
     "parent": "bilibili/search/03", "leave_frame": 1},
    {"id": "left_success_state_02", "kind": "leave_success", "failure_type": "left_success_state",
     "parent": "xiaohongshu/search/03", "leave_frame": 1},
    {"id": "left_success_state_03", "kind": "leave_success", "failure_type": "left_success_state",
     "parent": "cloudmusic/search/03", "leave_frame": 2},
    {"id": "left_success_state_04", "kind": "leave_success", "failure_type": "left_success_state",
     "parent": "gaode/location_search/03", "leave_frame": 1},
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


def frame_files(trace: Path, frame: int) -> list[Path]:
    result = []
    for suffix in (".jpg", ".json", ".xml"):
        path = trace / f"{frame}{suffix}"
        if not path.is_file():
            raise FileNotFoundError(path)
        result.append(path)
    return result


def copy_frame(source: Path, source_frame: int, destination: Path, destination_frame: int) -> None:
    for path in frame_files(source, source_frame):
        shutil.copy2(path, destination / f"{destination_frame}{path.suffix}")


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
    actions = read_json(parent / "actions.json")["actions"][:end_frame]
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
    return f"retained parent frames 1..{end - 1}; terminal frame {end} replaced by donor terminal frame {donor_end}"


def build_terminal_donor(parent: Path, donor: Path, donor_frame: int, destination: Path) -> str:
    metadata = read_json(parent / "actions.json")
    actions = metadata["actions"]
    reacts = read_json(parent / "react.json")
    end = int(metadata["action_count"])
    for frame in range(1, end + 1):
        copy_frame(parent, frame, destination, frame)
    copy_frame(donor, donor_frame, destination, end)
    actions[-1] = false_done_action(end)
    reacts[-1] = false_done_react(end)
    write_metadata(destination, metadata, actions, reacts)
    return f"retained parent frames 1..{end - 1}; terminal frame {end} replaced by reviewed donor frame {donor_frame}"


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
    return f"retained correct parent frames 1..{end}; appended parent non-target frame {leave_frame} as terminal frame {end + 1}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--destination", default=str(DEST_ROOT))
    parser.add_argument("--labels", default=str(DRAFT_LABELS))
    parser.add_argument("--manifest", default=str(DRAFT_MANIFEST))
    args = parser.parse_args()
    destination_root = Path(args.destination).resolve()
    labels_path = Path(args.labels).resolve()
    manifest_path = Path(args.manifest).resolve()
    for path in (destination_root, labels_path, manifest_path):
        if path.exists():
            raise FileExistsError(f"refusing to overwrite challenge asset: {path}")
    if not destination_root.is_relative_to((BENCHMARK / "traces").resolve()):
        raise RuntimeError(f"destination outside benchmark traces: {destination_root}")

    real_labels = {
        row["trace_id"]: row
        for row in (json.loads(line) for line in REAL_LABELS.read_text(encoding="utf-8").splitlines() if line.strip())
    }
    created_at = datetime.now().astimezone().isoformat(timespec="seconds")
    rows = []
    entries = []
    for spec in SPECS:
        parent_relative = spec["parent"]
        parent = REAL_ROOT / parent_relative
        parent_id = f"heldout_challenge_v1/real/{parent_relative.replace(chr(92), '/')}"
        parent_label = real_labels[parent_id]
        if parent_label["split"] != "challenge_v1" or parent_label["ground_truth"] != "success":
            raise RuntimeError(f"semireal parent is not a frozen challenge success: {parent_id}")
        trace_id = f"heldout_challenge_v1/semireal/{spec['failure_type']}/{spec['id']}"
        destination = BENCHMARK / "traces" / trace_id
        destination.mkdir(parents=True, exist_ok=False)
        donor_id = None
        if spec["kind"] == "truncate":
            transformation = build_truncate(parent, destination, int(spec["end_frame"]))
        elif spec["kind"] in {"terminal_swap", "terminal_frame_donor"}:
            donor_relative = spec["donor"]
            donor = REAL_ROOT / donor_relative
            donor_id = f"heldout_challenge_v1/real/{donor_relative.replace(chr(92), '/')}"
            if spec["kind"] == "terminal_swap":
                transformation = build_terminal_swap(parent, donor, destination)
            else:
                transformation = build_terminal_donor(parent, donor, int(spec["donor_frame"]), destination)
        elif spec["kind"] == "leave_success":
            transformation = build_leave_success(parent, destination, int(spec["leave_frame"]))
        else:
            raise ValueError(spec["kind"])

        metadata = read_json(destination / "actions.json")
        evidence = int(metadata["action_count"])
        digest, file_count, total_bytes = trace_digest(destination)
        row = {
            **{key: parent_label[key] for key in (
                "benchmark_task_id", "app", "task_type", "task_description", "expected_slots"
            )},
            "trace_id": trace_id,
            "ground_truth": "fail",
            "failure_type": spec["failure_type"],
            "evidence_frames": [evidence],
            "notes": f"Semireal challenge; {transformation}.",
            "origin": "semireal",
            "parent_trace_id": parent_id,
            "donor_trace_id": donor_id,
            "transformation": transformation,
            "split": "challenge_v1",
            "label_status": "pending_second_review",
            "reviewers": ["codex"],
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
            "trace_id": trace_id,
            "parent_trace_id": parent_id,
            "donor_trace_id": donor_id,
            "transformation": transformation,
            "sha256_tree": digest,
            "file_count": file_count,
            "bytes": total_bytes,
        })

    labels_path.parent.mkdir(parents=True, exist_ok=True)
    labels_path.write_text("".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows), encoding="utf-8")
    manifest = {
        "status": "pending_second_review",
        "created_at": created_at,
        "real_ground_truth_tag": "heldout-challenge-v1-real-ground-truth-freeze",
        "verifier_executed": False,
        "count": len(rows),
        "failure_type_counts": {
            failure_type: sum(row["failure_type"] == failure_type for row in rows)
            for failure_type in sorted({row["failure_type"] for row in rows})
        },
        "labels": {"path": labels_path.relative_to(ROOT).as_posix(), "sha256": sha256(labels_path)},
        "traces": entries,
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"count": len(rows), "failure_types": manifest["failure_type_counts"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
