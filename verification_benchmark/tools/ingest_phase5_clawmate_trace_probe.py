"""Create or verify a ClawMate trace-capability probe receipt."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Optional

from verification_benchmark.evaluation_framework.phase5_clawmate_trace_probe import (
    PROBE_ID,
    Phase5IntakeError,
    build_probe_receipt,
    strict_json_bytes,
    validate_probe_receipt,
    verify_probe_receipt,
)
from verification_benchmark.evaluation_framework.phase5_intake import write_new_json


RECEIPT_FILE = "phase5_clawmate_trace_probe_receipt.json"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--export-dir", type=Path, required=True)
    parser.add_argument("--receipt-dir", type=Path)
    parser.add_argument("--verify-receipt", type=Path)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = _parser().parse_args(argv)
    export_dir = args.export_dir.resolve(strict=True)
    if args.verify_receipt is not None:
        receipt = validate_probe_receipt(
            strict_json_bytes(
                args.verify_receipt.read_bytes(),
                context="ClawMate trace probe receipt",
            )
        )
        verify_probe_receipt(receipt, export_dir)
        print(
            json.dumps(
                {"status": "CLAWMATE_PROBE_RECEIPT_VERIFIED", "run_id": receipt["run_id"]},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    if args.receipt_dir is None:
        raise Phase5IntakeError("--receipt-dir is required when creating a receipt")
    receipt_dir = args.receipt_dir.resolve()
    if receipt_dir.exists():
        raise Phase5IntakeError(f"refusing to reuse or overwrite receipt directory: {receipt_dir}")
    if receipt_dir.is_relative_to(export_dir) or export_dir.is_relative_to(receipt_dir):
        raise Phase5IntakeError("export and receipt directories must not overlap")
    receipt = build_probe_receipt(export_dir=export_dir)
    write_new_json(receipt_dir / RECEIPT_FILE, receipt)
    print(
        json.dumps(
            {
                "status": receipt["status"],
                "probe_id": PROBE_ID,
                "run_id": receipt["run_id"],
                "task_id": receipt["task_id"],
                "canonical_adapter_ready": receipt["canonical_adapter_ready"],
                "errors": receipt["errors"],
                "source_tree_sha256": receipt["source_tree_sha256"],
                "receipt": str(receipt_dir / RECEIPT_FILE),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if receipt["status"] == "ACCEPTED_FOR_TRACE_CAPABILITY_REVIEW" else 2


if __name__ == "__main__":
    raise SystemExit(main())
