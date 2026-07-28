"""Prepare a create-once ClawMate trace-capability probe directory."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Optional

from verification_benchmark.evaluation_framework.phase5_clawmate_trace_probe import (
    MANIFEST_FILE,
    Phase5IntakeError,
    build_probe_manifest_template,
    semantic_sha256,
)
from verification_benchmark.evaluation_framework.phase5_intake import write_new_json


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--task-id", default="clawmate-crossapp-taobao-xhs-probe-001")
    parser.add_argument(
        "--task-text",
        default=(
            "先在淘宝搜索一个具体商品关键词，读取首屏一个非广告商品的可见品牌/型号短语；"
            "随后打开小红书搜索该短语，等公开结果出现同一商品相关证据后结束。全程只搜索和读取。"
        ),
    )
    parser.add_argument("--initial-app", default="淘宝")
    parser.add_argument("--target-app", default="小红书")
    parser.add_argument("--os-version", default="OpenHarmony-6.1.1.120")
    parser.add_argument("--clawmate-commit", default="d094be5f45fe0acd79d754b46c744480dcf2aba6")
    parser.add_argument("--harmony-app-submodule-commit", default="UNKNOWN_PENDING_OPERATOR_CAPTURE")
    parser.add_argument("--mobiinfer-commit", default="bce7456ce60b5e51b4d3b357f303515860353d8c")
    parser.add_argument("--desktop-version", default="UNKNOWN_PENDING_OPERATOR_CAPTURE")
    parser.add_argument(
        "--inference-backend",
        default="UNKNOWN_PENDING_OPERATOR_CAPTURE",
        choices=["MOBIINFER_ON_DEVICE", "CLAWMATE_DESKTOP_FALLBACK", "UNKNOWN_PENDING_OPERATOR_CAPTURE"],
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--write-template", action="store_true")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = _parser().parse_args(argv)
    manifest = build_probe_manifest_template(
        run_id=args.run_id,
        task_id=args.task_id,
        task_text=args.task_text,
        initial_app=args.initial_app,
        target_app=args.target_app,
        os_version=args.os_version,
        clawmate_commit=args.clawmate_commit,
        harmony_app_submodule_commit=args.harmony_app_submodule_commit,
        mobiinfer_commit=args.mobiinfer_commit,
        desktop_version=args.desktop_version,
        inference_backend=args.inference_backend,
    )
    output_dir = args.output_dir.resolve()
    summary = {
        "status": "TEMPLATE_READY" if not args.write_template else "TEMPLATE_WRITTEN",
        "device_mutation": False,
        "paid_provider_call": False,
        "verifier_allowed_before_gt": False,
        "run_id": args.run_id,
        "task_id": args.task_id,
        "output_dir": str(output_dir),
        "manifest": str(output_dir / MANIFEST_FILE),
        "probe_manifest_semantic_sha256": semantic_sha256(manifest),
        "operator_next_step": (
            "Run ClawMate manually, copy raw screenshots/actions/UI hierarchy/logs into this directory, "
            "then change collection_status to RUN_COMPLETE before intake."
        ),
    }
    if args.write_template:
        if output_dir.exists():
            raise Phase5IntakeError(f"refusing to reuse ClawMate probe directory: {output_dir}")
        write_new_json(output_dir / MANIFEST_FILE, manifest)
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
