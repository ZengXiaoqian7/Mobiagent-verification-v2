"""Materialize or verify the deterministic Phase 3 development replay artifacts."""

from __future__ import annotations

import argparse
import hmac
from pathlib import Path

from verification_benchmark.evaluation_framework.batch_replay_alignment import (
    batch_replay_result_json_schema,
    batch_result_json_bytes,
    load_batch_replay_manifest,
    run_batch_replay,
)
from verification_benchmark.evaluation_framework.event_log import (
    event_trace_sha256,
    read_durable_event_trace,
    write_durable_event_trace,
)


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MANIFEST = (
    ROOT
    / "verification_benchmark/batch_manifests/development/phase3_taobao_search_replay_alignment_v1.json"
)
DEFAULT_OUTPUT = (
    ROOT
    / "verification_benchmark/reports/audit_batch/development/phase3_taobao_search_replay_alignment_v1"
)
DEFAULT_SCHEMA = (
    ROOT
    / "verification_benchmark/schemas/development_batch_replay_result_v1.schema.json"
)


def _write_or_compare(path: Path, expected: bytes, *, check: bool) -> None:
    if path.exists():
        try:
            actual = path.read_bytes()
        except OSError as exc:
            raise ValueError(f"existing artifact is unreadable: {path}") from exc
        if not hmac.compare_digest(actual, expected):
            raise ValueError(f"existing deterministic artifact differs: {path}")
        return
    if check:
        raise ValueError(f"required deterministic artifact is missing: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(expected)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, default=ROOT)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)

    root = args.repo_root.resolve()
    manifest = load_batch_replay_manifest(args.manifest)
    result, traces = run_batch_replay(root, manifest)
    rows = result["traces"]
    for row, trace in zip(rows, traces):
        destination = args.output_dir / row["durable_trace_ref"]
        if destination.exists():
            replayed = read_durable_event_trace(destination)
            if not hmac.compare_digest(
                event_trace_sha256(replayed), row["event_trace_sha256"]
            ):
                raise ValueError(f"existing durable trace differs: {destination}")
        elif args.check:
            raise ValueError(f"required durable trace is missing: {destination}")
        else:
            write_durable_event_trace(destination, trace)
    _write_or_compare(
        args.output_dir / "batch_result.json",
        batch_result_json_bytes(result),
        check=args.check,
    )
    _write_or_compare(
        args.schema,
        batch_result_json_bytes(
            batch_replay_result_json_schema(result["schema_version"])
        ),
        check=args.check,
    )
    print(
        f"{manifest.batch_id}: {result['summary']['trace_count']} traces, "
        f"result_sha256={result['result_sha256']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
