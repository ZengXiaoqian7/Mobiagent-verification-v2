#!/usr/bin/env python3
"""Generate checksums and dependency metadata for the Enhanced baseline."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import platform
import subprocess
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "verification_benchmark" / "frozen" / "enhanced_development_20260712.json"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def main() -> int:
    patterns = [
        "MobiFlow/avdag/*.py",
        "verification_benchmark/rules/**/*.yaml",
        "verification_benchmark/configs/*.json",
        "verification_benchmark/configs/*.example",
        "verification_benchmark/tools/evaluate_benchmark.py",
        "verification_benchmark/tools/evaluate_baselines_v2.py",
        "verification_benchmark/tools/run_legacy_verifier.py",
        "requirements*.txt",
        "MobiFlow/pyproject.toml",
    ]
    files = sorted({path for pattern in patterns for path in ROOT.glob(pattern) if path.is_file()})
    dependencies = {}
    for name in ("PyYAML", "Pillow", "numpy", "opencv-python", "uiautomator2", "langchain-openai", "paddleocr", "pytesseract"):
        try:
            dependencies[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            dependencies[name] = None
    payload = {
        "freeze_id": "enhanced-development-20260712",
        "status": "development",
        "warning": "Includes post-hoc CloudMusic changes; not a held-out result.",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "git_head_before_freeze_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "dependencies": dependencies,
        "files": [
            {"path": path.relative_to(ROOT).as_posix(), "sha256": sha256(path), "bytes": path.stat().st_size}
            for path in files
        ],
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(OUTPUT), "files": len(files), "dependencies": dependencies}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
