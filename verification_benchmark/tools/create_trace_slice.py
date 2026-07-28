#!/usr/bin/env python3
"""Create a clean trace slice from an existing Runner trace.

This is useful for benchmark construction when a real trace contains extra
numeric artifacts after the recorded action list, or when creating a deliberate
truncated negative sample from a reviewed trace.
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from typing import Any, Dict, List


def _read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _copy_frame_artifacts(source: Path, dest: Path, index: int) -> None:
    for suffix in (".jpg", ".xml"):
        src = source / f"{index}{suffix}"
        if src.exists():
            shutil.copy2(src, dest / src.name)


def create_slice(
    source: Path,
    dest: Path,
    *,
    end_frame: int,
    overwrite: bool = False,
    stop_reason: str | None = None,
) -> Dict[str, Any]:
    if not source.is_dir():
        raise FileNotFoundError(f"source trace not found: {source}")
    if end_frame < 1:
        raise ValueError("--end-frame must be >= 1")
    if dest.exists():
        if not overwrite:
            raise FileExistsError(f"destination already exists: {dest}")
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)

    actions_meta = _read_json(source / "actions.json")
    reacts = _read_json(source / "react.json")
    if not isinstance(actions_meta, dict) or not isinstance(actions_meta.get("actions"), list):
        raise ValueError("actions.json must contain an object with an actions list")
    if not isinstance(reacts, list):
        raise ValueError("react.json must contain a list")

    actions: List[Dict[str, Any]] = list(actions_meta["actions"][:end_frame])
    sliced_reacts = list(reacts[:end_frame])

    for new_index, action in enumerate(actions, 1):
        if isinstance(action, dict):
            action["action_index"] = new_index
    for new_index, react in enumerate(sliced_reacts, 1):
        if isinstance(react, dict):
            react["action_index"] = new_index

    for index in range(1, end_frame + 1):
        _copy_frame_artifacts(source, dest, index)

    actions_meta["actions"] = actions
    actions_meta["action_count"] = len(actions)
    if stop_reason:
        actions_meta["stop_reason"] = stop_reason

    _write_json(dest / "actions.json", actions_meta)
    _write_json(dest / "react.json", sliced_reacts)

    return {
        "source": str(source),
        "dest": str(dest),
        "end_frame": end_frame,
        "action_count": len(actions),
        "react_count": len(sliced_reacts),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a trace slice from an existing trace.")
    parser.add_argument("--source", required=True)
    parser.add_argument("--dest", required=True)
    parser.add_argument("--end-frame", type=int, required=True)
    parser.add_argument("--stop-reason", default=None)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    result = create_slice(
        Path(args.source),
        Path(args.dest),
        end_frame=args.end_frame,
        stop_reason=args.stop_reason,
        overwrite=args.overwrite,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
