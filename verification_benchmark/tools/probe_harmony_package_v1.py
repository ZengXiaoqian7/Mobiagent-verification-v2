"""Read-only, hash-bound Harmony package/version probe."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from verification_benchmark.tools.probe_harmony_observability_v1 import (
    hdc_output,
    hdc_target_output,
    parse_hdc_targets,
)


SCHEMA_VERSION = "harmony-package-probe-v1"
PACKAGE_NAME = re.compile(r"^[a-zA-Z0-9_.]+$")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def parse_version_dump(raw: str, package: str) -> dict[str, Any]:
    if package not in raw:
        raise ValueError(f"bundle name missing from bm dump: {package}")
    version_codes = re.findall(r'"versionCode"\s*:\s*(\d+)', raw)
    version_names = re.findall(r'"versionName"\s*:\s*"([^"]+)"', raw)
    if not version_codes or not version_names:
        raise ValueError(f"version fields missing from bm dump: {package}")
    return {
        "package": package,
        "version_name": version_names[-1],
        "version_code": int(version_codes[-1]),
    }


def run_probe(
    *, output_dir: Path, hdc: str, expected_serial: str, packages: list[str]
) -> dict[str, Any]:
    if output_dir.exists():
        raise FileExistsError(f"output directory already exists: {output_dir}")
    if not packages or len(set(packages)) != len(packages):
        raise ValueError("package list must be non-empty and unique")
    if any(not PACKAGE_NAME.fullmatch(package) for package in packages):
        raise ValueError("invalid package name")
    output_dir.mkdir(parents=True)
    targets = parse_hdc_targets(hdc_output(hdc, "list", "targets"))
    if targets != [expected_serial]:
        raise RuntimeError(
            f"expected one authorized target {expected_serial!r}, observed {targets!r}"
        )
    device = {
        "model": hdc_target_output(
            hdc, expected_serial, "shell", "param", "get", "const.product.model"
        ),
        "openharmony_fullname": hdc_target_output(
            hdc, expected_serial, "shell", "param", "get", "const.ohos.fullname"
        ),
    }
    commands_executed = [
        [hdc, "list", "targets"],
        [
            hdc,
            "-t",
            expected_serial,
            "shell",
            "param",
            "get",
            "const.product.model",
        ],
        [
            hdc,
            "-t",
            expected_serial,
            "shell",
            "param",
            "get",
            "const.ohos.fullname",
        ],
    ]
    package_rows = []
    for index, package in enumerate(packages, 1):
        command = [
            hdc,
            "-t",
            expected_serial,
            "shell",
            "bm",
            "dump",
            "-n",
            package,
        ]
        raw = hdc_target_output(hdc, expected_serial, *command[3:])
        commands_executed.append(command)
        raw_path = output_dir / f"package_{index:02d}_bm_dump.txt"
        raw_path.write_text(raw + "\n", encoding="utf-8")
        parsed = parse_version_dump(raw, package)
        package_rows.append(
            {
                **parsed,
                "raw_dump_path": raw_path.name,
                "raw_dump_sha256": sha256(raw_path),
            }
        )
    report = {
        "schema_version": SCHEMA_VERSION,
        "status": "PASS",
        "mode": "READ_ONLY_PACKAGE_VERSION_PROBE",
        "prohibited_operations": [
            "model_call",
            "runner",
            "app_start",
            "click",
            "input",
            "swipe",
            "key_event",
            "install",
            "uninstall",
        ],
        "authorized_serial": expected_serial,
        "observed_targets": targets,
        "commands_executed": commands_executed,
        "device": device,
        "packages": package_rows,
    }
    (output_dir / "package_probe_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--expected-serial", required=True)
    parser.add_argument("--package", action="append", required=True)
    parser.add_argument("--hdc", default="hdc")
    args = parser.parse_args()
    try:
        report = run_probe(
            output_dir=args.output_dir,
            hdc=args.hdc,
            expected_serial=args.expected_serial,
            packages=args.package,
        )
    except Exception as exc:
        if args.output_dir.is_dir():
            failure_path = args.output_dir / "package_probe_failure.json"
            if not failure_path.exists():
                failure_path.write_text(
                    json.dumps(
                        {
                            "schema_version": SCHEMA_VERSION,
                            "status": "FAIL",
                            "authorized_serial": args.expected_serial,
                            "error_type": type(exc).__name__,
                            "error": str(exc),
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
        print(f"FAIL: {exc}")
        return 2
    print(
        json.dumps(
            {
                "status": report["status"],
                "report": str(args.output_dir / "package_probe_report.json"),
                "packages": report["packages"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
