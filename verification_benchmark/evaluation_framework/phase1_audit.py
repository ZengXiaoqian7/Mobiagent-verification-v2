"""Phase 1 freeze-audit primitives for deterministic RunReport replay."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, fields, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Tuple

from .event_log import (
    DurableEventTrace,
    read_durable_event_trace,
    write_durable_event_trace,
)
from .models import ContractIR, RunReport
from .replay import replay_event_trace


def _canonical_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {
            field.name: _canonical_value(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, Mapping):
        return {str(key): _canonical_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_canonical_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ValueError(f"RunReport contains a non-canonical value: {type(value).__name__}")


def run_report_payload(report: RunReport) -> dict[str, Any]:
    if not isinstance(report, RunReport):
        raise ValueError("report must be a RunReport")
    payload = _canonical_value(report)
    if not isinstance(payload, dict):  # retained as a hard postcondition
        raise AssertionError("RunReport canonical payload must be an object")
    if payload.get("compiler_provenance") is None:
        payload.pop("compiler_provenance", None)
    if payload.get("checker_acquisition_provenance") is None:
        payload.pop("checker_acquisition_provenance", None)
    return payload


def run_report_sha256(report: RunReport) -> str:
    rendered = json.dumps(
        run_report_payload(report),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(rendered).hexdigest()


@dataclass(frozen=True)
class ReplayDeterminismAudit:
    repetitions: int
    report_sha256: str
    in_memory_hashes: Tuple[str, ...]
    durable_hashes: Tuple[str, ...]

    @property
    def all_equal(self) -> bool:
        return len(set(self.in_memory_hashes + self.durable_hashes)) == 1


def audit_replay_determinism(
    contract: ContractIR,
    trace: DurableEventTrace,
    durable_path: Path | str,
    *,
    repetitions: int = 10,
) -> ReplayDeterminismAudit:
    """Assert N in-memory and N fresh durable reads produce one report hash."""

    if not isinstance(repetitions, int) or isinstance(repetitions, bool) or repetitions <= 0:
        raise ValueError("repetitions must be a positive integer")
    trace.validate()
    in_memory_hashes = tuple(
        run_report_sha256(replay_event_trace(contract, trace))
        for _ in range(repetitions)
    )
    write_durable_event_trace(durable_path, trace)
    durable_hashes = tuple(
        run_report_sha256(
            replay_event_trace(contract, read_durable_event_trace(durable_path))
        )
        for _ in range(repetitions)
    )
    combined = in_memory_hashes + durable_hashes
    if len(set(combined)) != 1:
        raise AssertionError(
            "RunReport determinism violated across in-memory/durable replay: "
            f"{sorted(set(combined))}"
        )
    return ReplayDeterminismAudit(
        repetitions=repetitions,
        report_sha256=combined[0],
        in_memory_hashes=in_memory_hashes,
        durable_hashes=durable_hashes,
    )
