"""Create or verify an immutable, Ground-Truth-free Phase 5 intake receipt."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Optional

from verification_benchmark.evaluation_framework.phase5_intake import (
    Phase5IntakeError,
    build_intake_receipt,
    load_experiment_manifest,
    strict_json_bytes,
    validate_intake_receipt,
    verify_intake_receipt,
    write_new_json,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MANIFEST = (
    REPO_ROOT
    / "verification_benchmark"
    / "experiments"
    / "phase5_harmony_blackbox_pilot_v1.json"
)
RECEIPT_FILE = "phase5_intake_receipt.json"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Strict Phase 5 intake; never copies or mutates raw run files."
    )
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--receipt-dir", type=Path)
    parser.add_argument("--verify-receipt", type=Path)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = _parser().parse_args(argv)
    run_dir = args.run_dir.resolve(strict=True)
    if args.verify_receipt is not None:
        receipt = validate_intake_receipt(
            strict_json_bytes(
                args.verify_receipt.read_bytes(), context="Phase 5 intake receipt"
            )
        )
        verify_intake_receipt(receipt, run_dir)
        print(
            json.dumps(
                {"status": "RECEIPT_VERIFIED", "run_id": receipt["run_id"]},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    if args.receipt_dir is None:
        raise Phase5IntakeError("--receipt-dir is required when creating a receipt")
    receipt_dir = args.receipt_dir.resolve()
    if receipt_dir.exists():
        raise Phase5IntakeError(
            f"refusing to reuse or overwrite receipt directory: {receipt_dir}"
        )
    if receipt_dir.is_relative_to(run_dir) or run_dir.is_relative_to(receipt_dir):
        raise Phase5IntakeError("raw run and receipt directories must not overlap")
    manifest = load_experiment_manifest(args.manifest)
    receipt = validate_intake_receipt(
        build_intake_receipt(experiment_manifest=manifest, run_dir=run_dir)
    )
    write_new_json(receipt_dir / RECEIPT_FILE, receipt)
    print(
        json.dumps(
            {
                "status": receipt["status"],
                "run_id": receipt["run_id"],
                "source_tree_sha256": receipt["source_tree_sha256"],
                "receipt": str(receipt_dir / RECEIPT_FILE),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if receipt["status"] == "ACCEPTED_PENDING_BLIND_REVIEW" else 2


if __name__ == "__main__":
    raise SystemExit(main())
