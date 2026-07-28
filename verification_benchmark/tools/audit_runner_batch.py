#!/usr/bin/env python3
"""Audit Runner output traces before benchmark import."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List


REPO_ROOT = Path(__file__).resolve().parents[2]
BENCHMARK_ROOT = REPO_ROOT / "verification_benchmark"
TOOLS_DIR = Path(__file__).resolve().parent

if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from check_trace_schema import inspect_trace  # noqa: E402


def rel(path: Path) -> str:
    try:
        return path.resolve().relative_to(REPO_ROOT.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def find_trace_dirs(source_root: Path) -> List[Path]:
    if not source_root.exists():
        return []
    return sorted({path.parent for path in source_root.rglob("actions.json")})


def audit_trace(path: Path) -> Dict[str, Any]:
    schema = inspect_trace(path)
    return {
        "path": rel(path),
        "ok": bool(schema.get("ok")),
        "errors": schema.get("errors") or [],
        "warnings": schema.get("warnings") or [],
        "action_count": schema.get("action_count"),
        "react_count": schema.get("react_count"),
        "extra_artifacts": schema.get("extra_artifacts") or [],
        "missing_jpg": schema.get("missing_jpg") or [],
        "missing_xml": schema.get("missing_xml") or [],
    }


def summarize(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    ok_count = sum(1 for row in rows if row["ok"])
    warning_count = sum(1 for row in rows if row["warnings"])
    error_count = sum(1 for row in rows if row["errors"])
    return {
        "trace_count": len(rows),
        "ok_count": ok_count,
        "warning_count": warning_count,
        "error_count": error_count,
    }


def render_markdown(payload: Dict[str, Any]) -> str:
    summary = payload["summary"]
    lines = [
        "# Runner Batch Audit",
        "",
        f"Generated at: `{payload['generated_at']}`",
        "",
        "## Summary",
        "",
        "| traces | ok | with warnings | with errors |",
        "|---:|---:|---:|---:|",
        (
            f"| {summary['trace_count']} | {summary['ok_count']} | "
            f"{summary['warning_count']} | {summary['error_count']} |"
        ),
        "",
        "## Traces",
        "",
        "| path | ok | actions | react | warnings | errors |",
        "|---|---:|---:|---:|---|---|",
    ]
    for row in payload["traces"]:
        warnings = "<br>".join(row["warnings"]) if row["warnings"] else ""
        errors = "<br>".join(row["errors"]) if row["errors"] else ""
        lines.append(
            "| {path} | {ok} | {actions} | {react} | {warnings} | {errors} |".format(
                path=f"`{row['path']}`",
                ok="yes" if row["ok"] else "no",
                actions=row["action_count"] if row["action_count"] is not None else "",
                react=row["react_count"] if row["react_count"] is not None else "",
                warnings=warnings,
                errors=errors,
            )
        )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit Runner output traces before benchmark import.")
    parser.add_argument(
        "--source-root",
        default=str(BENCHMARK_ROOT / "runner_outputs"),
        help="Directory containing Runner outputs. All nested actions.json files are scanned.",
    )
    parser.add_argument(
        "--output-dir",
        default=str(BENCHMARK_ROOT / "reports" / "runner_batch_audit"),
        help="Directory for runner_batch_audit.json/md.",
    )
    args = parser.parse_args()

    source_root = Path(args.source_root)
    rows = [audit_trace(path) for path in find_trace_dirs(source_root)]
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source_root": rel(source_root),
        "summary": summarize(rows),
        "traces": rows,
    }

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "runner_batch_audit.json"
    md_path = output_dir / "runner_batch_audit.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown(payload), encoding="utf-8")

    print(json.dumps(payload["summary"], ensure_ascii=False, indent=2))
    print(f"json: {rel(json_path)}")
    print(f"markdown: {rel(md_path)}")
    return 1 if payload["summary"]["error_count"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
