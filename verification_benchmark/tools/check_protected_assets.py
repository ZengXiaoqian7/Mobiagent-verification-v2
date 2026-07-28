#!/usr/bin/env python3
"""Verify immutable v2 references and committed-byte hashes for v3 work."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any, Dict, List


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MANIFEST = ROOT / "verification_benchmark/benchmark_v3/manifests/protected_assets.json"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def resolve_ref(name: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(ROOT), "rev-list", "-n", "1", name],
        check=False,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip() if completed.returncode == 0 else ""


def check(manifest: Dict[str, Any]) -> Dict[str, Any]:
    failures: List[Dict[str, Any]] = []
    files = []
    for entry in manifest["files"]:
        path = ROOT / entry["path"]
        actual_hash = sha256(path) if path.is_file() else None
        actual_bytes = path.stat().st_size if path.is_file() else None
        ok = actual_hash == entry["sha256"] and actual_bytes == entry["bytes"]
        row = {"path": entry["path"], "ok": ok, "sha256": actual_hash, "bytes": actual_bytes}
        files.append(row)
        if not ok:
            failures.append(row)

    refs = []
    for name, expected in manifest["frozen_refs"].items():
        actual = resolve_ref(name)
        row = {"ref": name, "expected": expected, "actual": actual, "ok": actual == expected}
        refs.append(row)
        if not row["ok"]:
            failures.append(row)
    return {"ok": not failures, "files": files, "refs": refs, "failures": failures}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    parser.add_argument("--output")
    args = parser.parse_args()
    result = check(json.loads(Path(args.manifest).read_text(encoding="utf-8")))
    rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        Path(args.output).write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
