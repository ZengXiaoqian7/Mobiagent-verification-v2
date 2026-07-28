#!/usr/bin/env python3
"""Materialize a non-destructive semantic-XML redaction stress dataset."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any


SEMANTIC_ATTRIBUTES = ("text", "content-desc", "resource-id")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def numbered_xml_paths(trace: Path) -> list[Path]:
    return sorted(
        (path for path in trace.glob("*.xml") if path.stem.isdigit()),
        key=lambda path: int(path.stem),
    )


def redact_xml(source: Path, destination: Path) -> None:
    tree = ET.parse(source)
    for node in tree.iter():
        for attribute in SEMANTIC_ATTRIBUTES:
            if attribute in node.attrib:
                node.set(attribute, "")
    tree.write(destination, encoding="utf-8", xml_declaration=True)


def validate_redaction(source: Path, derived: Path) -> None:
    source_nodes = list(ET.parse(source).iter())
    derived_nodes = list(ET.parse(derived).iter())
    if len(source_nodes) != len(derived_nodes):
        raise ValueError(f"node count changed: {source} -> {derived}")
    for index, (before, after) in enumerate(zip(source_nodes, derived_nodes)):
        if before.tag != after.tag:
            raise ValueError(f"node tag changed at {index}: {source}")
        before_nonsemantic = {k: v for k, v in before.attrib.items() if k not in SEMANTIC_ATTRIBUTES}
        after_nonsemantic = {k: v for k, v in after.attrib.items() if k not in SEMANTIC_ATTRIBUTES}
        if before_nonsemantic != after_nonsemantic:
            raise ValueError(f"non-semantic attribute changed at node {index}: {source}")
        if any(after.attrib.get(attribute) not in (None, "") for attribute in SEMANTIC_ATTRIBUTES):
            raise ValueError(f"semantic attribute was retained at node {index}: {derived}")


def copy_and_redact(source_trace: Path, destination_trace: Path) -> list[dict[str, Any]]:
    if destination_trace.exists():
        raise FileExistsError(f"refusing to overwrite derived trace: {destination_trace}")
    destination_trace.mkdir(parents=True)
    records: list[dict[str, Any]] = []
    for source in sorted(source_trace.iterdir(), key=lambda path: path.name):
        if not source.is_file():
            raise ValueError(f"unexpected nested directory in source trace: {source}")
        destination = destination_trace / source.name
        if source.suffix == ".xml" and source.stem.isdigit():
            redact_xml(source, destination)
            validate_redaction(source, destination)
            transformation = "semantic_xml_redaction"
        else:
            shutil.copy2(source, destination)
            if sha256(source) != sha256(destination):
                raise ValueError(f"byte-copy verification failed: {source}")
            transformation = "byte_copy"
        records.append({
            "path": source.name,
            "transformation": transformation,
            "source_sha256": sha256(source),
            "derived_sha256": sha256(destination),
        })
    if not numbered_xml_paths(destination_trace):
        raise ValueError(f"derived trace has no numbered XML: {destination_trace}")
    return records


def materialize(manifest_path: Path, derived_root: Path) -> dict[str, Any]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("status") != "development_preregistered_not_yet_materialized":
        raise ValueError("unexpected manifest status; refuse to materialize")
    source_root = Path(manifest["source_trace_root"])
    source_labels = Path(manifest["source_labels"])
    if not source_root.is_dir():
        raise FileNotFoundError(f"source trace root does not exist: {source_root}")
    if not source_labels.is_file():
        raise FileNotFoundError(f"source labels do not exist: {source_labels}")
    if derived_root.exists():
        raise FileExistsError(f"refusing to overwrite derived root: {derived_root}")
    derived_root.mkdir(parents=True)
    rows = []
    try:
        for sample in manifest["samples"]:
            trace_id = sample["trace_id"]
            source_trace = source_root / trace_id
            destination_trace = derived_root / trace_id
            if not source_trace.is_dir():
                raise FileNotFoundError(f"source trace missing: {source_trace}")
            rows.append({
                "trace_id": trace_id,
                "terminal_frame": sample["terminal_frame"],
                "files": copy_and_redact(source_trace, destination_trace),
            })
        write_derived_labels(manifest, source_labels, derived_root)
        report = {
            "dataset_id": manifest["dataset_id"],
            "data_status": manifest["data_status"],
            "manifest_sha256": sha256(manifest_path),
            "source_trace_root": str(source_root),
            "derived_trace_root": str(derived_root),
            "sample_count": len(rows),
            "rows": rows,
        }
        (derived_root / "materialization_manifest.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return report
    except Exception:
        # Do not delete potentially useful diagnostic output automatically.
        raise


def write_derived_labels(manifest: dict[str, Any], source_labels: Path, derived_root: Path) -> Path:
    output = derived_root / "labels.jsonl"
    if output.exists():
        raise FileExistsError(f"refusing to overwrite derived labels: {output}")
    requested = [sample["trace_id"] for sample in manifest["samples"]]
    source_rows = {
        row["trace_id"]: row
        for row in (json.loads(line) for line in source_labels.read_text(encoding="utf-8").splitlines() if line.strip())
    }
    missing = [trace_id for trace_id in requested if trace_id not in source_rows]
    if missing:
        raise ValueError(f"source labels missing requested traces: {missing}")
    derived_rows = []
    for trace_id in requested:
        row = dict(source_rows[trace_id])
        row["source_trace_id"] = trace_id
        row["derived_dataset_id"] = manifest["dataset_id"]
        row["derived_xml_transformation"] = "semantic_xml_redaction"
        row["split"] = "development_accessibility_poor_xml_stress_v1"
        derived_rows.append(row)
    output.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in derived_rows), encoding="utf-8"
    )
    return output


def write_labels_only(manifest_path: Path, derived_root: Path) -> Path:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not derived_root.is_dir():
        raise FileNotFoundError(f"derived root does not exist: {derived_root}")
    return write_derived_labels(manifest, Path(manifest["source_labels"]), derived_root)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--derived-root", required=True, type=Path)
    parser.add_argument("--labels-only", action="store_true")
    args = parser.parse_args()
    if args.labels_only:
        output = write_labels_only(args.manifest, args.derived_root)
        print(json.dumps({"labels": str(output)}, ensure_ascii=False))
        return 0
    report = materialize(args.manifest, args.derived_root)
    print(json.dumps({
        "dataset_id": report["dataset_id"],
        "sample_count": report["sample_count"],
        "derived_trace_root": report["derived_trace_root"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
