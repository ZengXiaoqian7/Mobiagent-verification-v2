#!/usr/bin/env python3
"""Add reproducible Wilson intervals to an existing frozen evaluator output.

This utility reads an existing JSON report. It never runs a verifier and never
modifies the source result, labels, traces, or freeze manifests.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from verification_framework.evaluation import compute_metrics, grouped_metrics  # noqa: E402


def augment(report: Dict[str, Any]) -> Dict[str, Any]:
    systems = {}
    for name, payload in report["systems"].items():
        rows = payload["results"]
        systems[name] = {
            "metrics": compute_metrics(rows),
            "grouped": {key: grouped_metrics(rows, key) for key in ("app", "task_type", "failure_type")},
        }
    return {
        "schema_version": "3.0-derived-statistics",
        "source_schema_version": report.get("schema_version"),
        "source_inputs": report.get("inputs", {}),
        "systems": systems,
        "provenance": {
            "operation": "read-only metric derivation",
            "verifier_rerun": False,
            "ground_truth_changed": False,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output")
    args = parser.parse_args()
    derived = augment(json.loads(Path(args.input).read_text(encoding="utf-8")))
    rendered = json.dumps(derived, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        Path(args.output).write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
