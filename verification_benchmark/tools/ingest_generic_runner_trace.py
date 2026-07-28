"""Strictly bind a generic Runner output tree to a create-once intake receipt."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Optional

from verification_benchmark.evaluation_framework.phase5_intake import (
    Phase5IntakeError,
    semantic_sha256,
    source_file_manifest,
    strict_json_bytes,
    write_new_json,
)
from verification_benchmark.evaluation_framework.phase5_trace_case import trace_dir


RECEIPT = "generic_intake_receipt.json"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--receipt-dir", type=Path, required=True)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = _parser().parse_args(argv)
    run_dir = args.run_dir.resolve(strict=True)
    manifest_path = run_dir / "run_manifest.json"
    manifest = strict_json_bytes(
        manifest_path.read_bytes(), context="generic run manifest"
    )
    if manifest.get("status") != "RUN_COMPLETE" or manifest.get("runner_exit_code") != 0:
        raise Phase5IntakeError("generic Runner output is not complete")
    trace = trace_dir(run_dir, manifest)
    if not (trace / "actions.json").is_file():
        raise Phase5IntakeError("generic Runner trace is missing actions.json")
    receipt = {
        "schema_version": "mobiagent-generic-intake-receipt-v1",
        "run_id": manifest.get("run_id"),
        "task_id": manifest.get("task_id"),
        "task_spec_sha256": manifest.get("task_spec_sha256"),
        "source_tree_sha256": semantic_sha256(list(source_file_manifest(run_dir))),
        "trace_relpath": manifest.get("trace_relpath"),
        "status": "ACCEPTED",
    }
    destination = args.receipt_dir.resolve() / RECEIPT
    write_new_json(destination, receipt)
    print(json.dumps({"status": "ACCEPTED", "receipt": str(destination)}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
