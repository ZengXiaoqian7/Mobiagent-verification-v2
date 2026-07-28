#!/usr/bin/env python3
"""Capture and audit one read-only Harmony observation for learned-verifier v1.

The probe intentionally has no Runner, model, app-launch, click, input, swipe,
or key-event capability.  A human runs it against one authorized Harmony device
to establish the deployment-domain observation contract before any collection.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "harmony-observability-probe-v1"
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def ensure_repository_root_on_path() -> None:
    """Allow direct execution from ``verification_benchmark/tools``.

    Python otherwise puts this tool's directory, rather than the repository
    root, first on ``sys.path`` and cannot resolve the sibling ``runner``
    package used only at the explicit device-I/O boundary.
    """
    root = str(REPOSITORY_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def parse_hdc_targets(stdout: str) -> list[str]:
    """Return serials from `hdc list targets`, rejecting status-only lines."""
    ignored = {"", "[empty]", "[none]", "no targets"}
    targets = []
    for raw in stdout.splitlines():
        item = raw.strip()
        if item and item.lower() not in ignored:
            targets.append(item.split()[0])
    return targets


def hdc_output(hdc: str, *arguments: str) -> str:
    completed = subprocess.run(
        [hdc, *arguments], check=True, text=True, capture_output=True, encoding="utf-8"
    )
    output = completed.stdout.strip()
    if output.startswith("[Fail]") or "\n[Fail]" in output:
        raise RuntimeError(f"hdc command failed: {output}")
    return output


def hdc_target_output(hdc: str, serial: str, *arguments: str) -> str:
    return hdc_output(hdc, "-t", serial, *arguments)


def normalize_hierarchy(raw: Any) -> dict[str, Any]:
    if isinstance(raw, str):
        raw = json.loads(raw)
    if not isinstance(raw, dict):
        raise ValueError("Harmony hierarchy must be a JSON object")
    return raw


def hierarchy_stats(root: dict[str, Any]) -> dict[str, int]:
    pending = [root]
    nodes = clickable = text_or_description = bounded = 0
    while pending:
        item = pending.pop()
        if not isinstance(item, dict):
            continue
        nodes += 1
        attrs = item.get("attributes") or {}
        if not isinstance(attrs, dict):
            attrs = {}
        clickable += str(attrs.get("clickable", "false")).lower() == "true"
        text_or_description += bool(
            attrs.get("text") or attrs.get("originalText") or attrs.get("description") or attrs.get("hint")
        )
        bounded += bool(attrs.get("bounds"))
        children = item.get("children") or []
        if isinstance(children, list):
            pending.extend(children)
    return {
        "node_count": nodes,
        "clickable_node_count": clickable,
        "text_or_description_node_count": text_or_description,
        "bounded_node_count": bounded,
    }


def image_size(path: Path) -> list[int] | None:
    try:
        from PIL import Image

        with Image.open(path) as image:
            return [image.width, image.height]
    except Exception:
        return None


def run_probe(output_dir: Path, hdc: str, expected_serial: str) -> dict[str, Any]:
    if output_dir.exists():
        raise FileExistsError(f"output directory already exists: {output_dir}")
    output_dir.mkdir(parents=True)

    targets = parse_hdc_targets(hdc_output(hdc, "list", "targets"))
    if targets != [expected_serial]:
        raise RuntimeError(
            "refusing capture: expected exactly one authorized target "
            f"{expected_serial!r}, observed {targets!r}"
        )

    device_facts = {
        "brand": hdc_target_output(
            hdc, expected_serial, "shell", "param", "get", "const.product.brand"
        ),
        "model": hdc_target_output(
            hdc, expected_serial, "shell", "param", "get", "const.product.model"
        ),
        "openharmony_fullname": hdc_target_output(
            hdc, expected_serial, "shell", "param", "get", "const.ohos.fullname"
        ),
    }

    # Import lazily: unit tests can exercise the pure audit helpers without a
    # Harmony driver, while actual device I/O is explicit at the CLI boundary.
    ensure_repository_root_on_path()
    from runner.mobiagent.mobiagent import HarmonyDevice, _harmony_hierarchy_to_xml

    device = HarmonyDevice(expected_serial)
    screenshot = output_dir / "current_screen.jpg"
    raw_hierarchy = output_dir / "current_hierarchy.json"
    projected_xml = output_dir / "current_hierarchy.xml"
    try:
        device.screenshot(str(screenshot))
        hierarchy = normalize_hierarchy(device.dump_hierarchy())
    finally:
        device.close()

    raw_hierarchy.write_text(json.dumps(hierarchy, ensure_ascii=False, indent=2), encoding="utf-8")
    projected_xml.write_text(_harmony_hierarchy_to_xml(hierarchy), encoding="utf-8")
    stats = hierarchy_stats(hierarchy)
    report = {
        "schema_version": SCHEMA_VERSION,
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "mode": "read_only_observation",
        "prohibited_operations": ["model_call", "runner", "app_start", "click", "input", "swipe", "key_event"],
        "authorized_serial": expected_serial,
        "observed_targets": targets,
        "device": device_facts,
        "artifacts": {
            "screenshot": {"path": screenshot.name, "sha256": sha256(screenshot), "size": image_size(screenshot)},
            "raw_hierarchy": {"path": raw_hierarchy.name, "sha256": sha256(raw_hierarchy)},
            "xml_projection": {"path": projected_xml.name, "sha256": sha256(projected_xml)},
        },
        "hierarchy_stats": stats,
        "checks": {
            "exactly_one_authorized_target": True,
            "screenshot_nonempty": screenshot.stat().st_size > 0,
            "hierarchy_has_nodes": stats["node_count"] > 0,
            "xml_projection_nonempty": projected_xml.stat().st_size > 0,
        },
    }
    report["status"] = "PASS" if all(report["checks"].values()) else "FAIL"
    (output_dir / "probe_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--expected-serial", required=True)
    parser.add_argument("--hdc", default="hdc")
    args = parser.parse_args()
    try:
        report = run_probe(args.output_dir, args.hdc, args.expected_serial)
    except Exception as exc:
        if args.output_dir.is_dir():
            failure_path = args.output_dir / "probe_failure.json"
            if not failure_path.exists():
                failure_path.write_text(
                    json.dumps(
                        {
                            "schema_version": SCHEMA_VERSION,
                            "status": "FAIL",
                            "mode": "read_only_observation",
                            "authorized_serial": args.expected_serial,
                            "error_type": type(exc).__name__,
                            "error": str(exc),
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"status": report["status"], "report": str(args.output_dir / "probe_report.json")}, ensure_ascii=False))
    return 0 if report["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
