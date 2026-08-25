"""Check whether one PC verifier runtime profile is ready."""

from __future__ import annotations

import argparse
import json

from pc_client.environment import check_environment


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--profile",
        choices=("core", "test", "package", "android", "harmony"),
        default="core",
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    report = check_environment(args.profile)
    if args.json:
        print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
    else:
        state = "READY" if report.ready else "MISSING"
        print(f"{report.profile}: {state}")
        for requirement in report.requirements:
            marker = "OK" if requirement.available else "MISSING"
            print(f"  [{marker}] {requirement.kind}: {requirement.name}")
    return 0 if report.ready else 2


if __name__ == "__main__":
    raise SystemExit(main())
