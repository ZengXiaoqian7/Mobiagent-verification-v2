#!/usr/bin/env python3
"""Validate Runner task JSON files before launching phone experiments."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Iterable, List


MOJIBAKE_MARKERS = (
    "娣",
    "鍦",
    "绱",
    "閿",
    "椤",
    "缁",
    "骞",
    "€",
    "�",
)


def iter_text_values(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for nested in value.values():
            yield from iter_text_values(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from iter_text_values(nested)


def suspicious_texts(data: Any) -> List[str]:
    found: List[str] = []
    for text in iter_text_values(data):
        if any(marker in text for marker in MOJIBAKE_MARKERS):
            found.append(text)
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate a Runner task JSON file.")
    parser.add_argument("task_file")
    args = parser.parse_args()

    path = Path(args.task_file)
    data = json.loads(path.read_text(encoding="utf-8"))
    suspicious = suspicious_texts(data)

    print(f"file: {path}")
    print(f"top_level_type: {type(data).__name__}")
    if isinstance(data, list):
        print(f"task_groups: {len(data)}")

    print("\n[text values as unicode_escape]")
    for index, text in enumerate(iter_text_values(data), 1):
        print(f"{index}. {text.encode('unicode_escape').decode('ascii')}")

    if suspicious:
        print("\nstatus: suspicious")
        print("Possible mojibake markers were found in the actual UTF-8 content.")
        for text in suspicious:
            print(f"- {text.encode('unicode_escape').decode('ascii')}")
        return 1

    print("\nstatus: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
