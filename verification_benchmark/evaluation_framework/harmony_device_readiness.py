"""Fail-fast HDC readiness gate used before creating a Runner attempt."""

from __future__ import annotations

import shutil
import subprocess
import time
from dataclasses import dataclass

from .phase5_intake import Phase5IntakeError


HDC_READY_TOKEN = "MOBIAGENT_HDC_READY"


@dataclass(frozen=True)
class HdcReadiness:
    serial: str
    attempts_used: int
    executable: str


def wait_for_hdc_shell(
    serial: str,
    *,
    attempts: int = 6,
    interval_seconds: float = 2.0,
    command_timeout_seconds: float = 8.0,
) -> HdcReadiness:
    """Require a working device shell, not merely a listed USB target."""

    if not isinstance(serial, str) or not serial.strip():
        raise Phase5IntakeError("Harmony device serial is required")
    if attempts <= 0:
        raise ValueError("attempts must be positive")
    executable = shutil.which("hdc")
    if executable is None:
        raise Phase5IntakeError("hdc executable is unavailable")
    last_detail = "no HDC attempt completed"
    command = [executable, "-t", serial.strip(), "shell", f"echo {HDC_READY_TOKEN}"]
    for attempt in range(1, attempts + 1):
        try:
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=command_timeout_seconds,
            )
            combined = "\n".join((result.stdout or "", result.stderr or "")).strip()
            if (
                result.returncode == 0
                and HDC_READY_TOKEN in combined
                and "[Fail]" not in combined
            ):
                return HdcReadiness(serial.strip(), attempt, executable)
            last_detail = (
                f"exit={result.returncode}; output={combined[-600:] or '<empty>'}"
            )
        except subprocess.TimeoutExpired:
            last_detail = f"command timed out after {command_timeout_seconds:g}s"
        if attempt < attempts and interval_seconds > 0:
            time.sleep(interval_seconds)
    raise Phase5IntakeError(
        "Harmony HDC shell is not ready after "
        f"{attempts} attempts for {serial.strip()}: {last_detail}. "
        "Unlock the device, approve USB/HDC debugging, then retry with a new run-id."
    )


__all__ = ["HDC_READY_TOKEN", "HdcReadiness", "wait_for_hdc_shell"]
