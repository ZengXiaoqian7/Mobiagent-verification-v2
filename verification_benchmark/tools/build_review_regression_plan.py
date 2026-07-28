"""Import incorrect user-review rows into a rerunnable evaluation plan."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Optional

from verification_benchmark.evaluation_framework.phase5_intake import Phase5IntakeError
from verification_benchmark.evaluation_framework.review_regression import (
    build_review_regression_plan,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evaluation-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--merge", action="store_true")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = _parser().parse_args(argv)
    payload = build_review_regression_plan(
        args.evaluation_dir, args.output, merge=args.merge
    )
    print(
        json.dumps(
            {
                "status": "REVIEW_REGRESSION_PLAN_READY",
                "output": str(args.output.resolve()),
                "case_count": len(payload["tasks"]),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Phase5IntakeError as exc:
        raise SystemExit(str(exc)) from exc
