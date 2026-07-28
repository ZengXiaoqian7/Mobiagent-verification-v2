#!/usr/bin/env python3
"""Resolve only policy-authorized action-bound development frames."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from verification_benchmark.evaluation_framework.action_frame_resolver import (  # noqa: E402
    resolve_action_bound_frames,
    write_resolved_catalog_outputs,
)


DEFAULT_BASE_CATALOG = (
    "verification_benchmark/reports/recording_planner/development/v1/catalog.json"
)
DEFAULT_PLANNER_CONFIG = (
    "verification_benchmark/recording_planner/development/"
    "coverage_planner_v1.config.json"
)
DEFAULT_POLICY = (
    "verification_benchmark/recording_planner/development/"
    "action_frame_resolution_policy_v1.json"
)
DEFAULT_OUTPUT = "verification_benchmark/reports/recording_planner/development/v2"


def _rooted(repository: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else repository / path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Apply hash-bound, reasoning-free action/frame rules with zero network."
        )
    )
    parser.add_argument("--repository-root", default=str(ROOT))
    parser.add_argument("--base-catalog", default=DEFAULT_BASE_CATALOG)
    parser.add_argument("--planner-config", default=DEFAULT_PLANNER_CONFIG)
    parser.add_argument("--policy", default=DEFAULT_POLICY)
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    repository = Path(args.repository_root).resolve()
    result = resolve_action_bound_frames(
        repository,
        base_catalog_path=_rooted(repository, args.base_catalog),
        planner_config_path=_rooted(repository, args.planner_config),
        policy_path=_rooted(repository, args.policy),
    )
    write_resolved_catalog_outputs(result, _rooted(repository, args.output))
    summary = result.catalog["summary"]
    print(
        json.dumps(
            {
                "status": "RESOLVED_REVIEW_PENDING",
                "catalog_sha256": result.catalog["catalog_sha256"],
                "auto_resolved": result.catalog["frame_resolution"][
                    "auto_resolved_count"
                ],
                "unresolved": summary["unresolved_selection_count"],
                "unique_jobs": summary["unique_job_count"],
                "cached": summary["cached_unique_job_count"],
                "uncached": summary["uncached_unique_job_count"],
                "draft_shards": summary["draft_shard_count"],
                "worst_case_requests": summary["worst_case_requests"],
                "real_http_requests": 0,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
