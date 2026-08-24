"""Evaluate a frozen PC App-test replay cohort against human truth."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from verification_benchmark.evaluation_framework.app_test_replay_baseline import (
    evaluate_replay_baseline,
    load_replay_baseline_cases,
    render_replay_baseline_markdown,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument(
        "--asset-root",
        type=Path,
        help="root used to resolve relative test-case and manifest paths",
    )
    parser.add_argument("--output-json", type=Path)
    parser.add_argument("--output-markdown", type=Path)
    parser.add_argument(
        "--trust-historical-step-gates",
        action="store_true",
        help="diagnostic only: do not recompute Step Gate decisions from raw trace facts",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    cases = load_replay_baseline_cases(args.config, asset_root=args.asset_root)
    report = evaluate_replay_baseline(
        cases,
        recompute_step_gates=not args.trust_historical_step_gates,
    )
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output_json is not None:
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(rendered, encoding="utf-8")
    if args.output_markdown is not None:
        args.output_markdown.parent.mkdir(parents=True, exist_ok=True)
        args.output_markdown.write_text(
            render_replay_baseline_markdown(report),
            encoding="utf-8",
        )
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
