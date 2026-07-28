#!/usr/bin/env python3
"""Generate deterministic development-only recording catalog and draft shards."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from verification_benchmark.evaluation_framework.recording_coverage_planner import (  # noqa: E402
    plan_development_recording_coverage,
    write_planner_outputs,
)


DEFAULT_CONFIG = (
    "verification_benchmark/recording_planner/development/"
    "coverage_planner_v1.config.json"
)
DEFAULT_OUTPUT = "verification_benchmark/reports/recording_planner/development/v1"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Plan review-pending OCR/LLM recording coverage with zero network."
    )
    parser.add_argument("--repository-root", default=str(ROOT))
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    repository = Path(args.repository_root).resolve()
    config = Path(args.config)
    if not config.is_absolute():
        config = repository / config
    output = Path(args.output)
    if not output.is_absolute():
        output = repository / output
    result = plan_development_recording_coverage(repository, config)
    write_planner_outputs(result, output)
    summary = result.catalog["summary"]
    print(
        json.dumps(
            {
                "status": "PLANNED_REVIEW_PENDING",
                "catalog_sha256": result.catalog["catalog_sha256"],
                "candidate_records": summary["candidate_record_count"],
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
