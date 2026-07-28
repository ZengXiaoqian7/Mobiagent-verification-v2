"""Run the phone-owned Harmony AgentRuntime through the narrow HDC I/O bridge.

The PC process intentionally transports only a testcase file and HDC I/O.  It
does not instantiate a MobiAgent model executor or decide the result; the HAP
writes the authoritative report and this module merely exports/intakes it.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import secrets
import subprocess
import time
from typing import Any

from .harmony_hdc_bridge import HdcBridgeServer, HdcBridgeService, _hdc_provision_phone, _hdc_rport
from .harmony_intake import intake_report


DEFAULT_BUNDLE = "com.zengxq.mobiagentprobe"
# This physical Harmony device has a verified persistent reverse mapping on
# this port.  Replacing it with a different port while the target is
# foregrounded produces peer resets on this device, so each run replaces only
# the local server behind this stable channel.
PHONE_PORT = 19130
PC_PORT = 9130
REPORT_PATTERN = re.compile(r"(/data/storage/el2/(?:base/)?(?:haps/entry/)?files/reports/[A-Za-z0-9_-]+-\d+\.json)")


def _hdc(serial: str, *args: str, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["hdc", "-t", serial, *args],
        text=True,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def _raise_if_failed(result: subprocess.CompletedProcess[str]) -> None:
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "HDC command failed")


def _wait_for_phone_report(serial: str, test_case_id: str, started_at_ms: int, timeout_seconds: int) -> str:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        result = _hdc(serial, "shell", "hilog", "-x", timeout=60)
        _raise_if_failed(result)
        candidates: list[tuple[int, str]] = []
        for match in REPORT_PATTERN.finditer(result.stdout):
            path = match.group(1)
            if f"/{test_case_id}-" not in path:
                continue
            try:
                timestamp = int(path.rsplit("-", 1)[-1].removesuffix(".json"))
            except ValueError:
                continue
            if timestamp >= started_at_ms:
                candidates.append((timestamp, path))
        if candidates:
            return max(candidates)[1]
        time.sleep(2)
    raise TimeoutError(f"phone AgentRuntime did not emit a report for {test_case_id}")


def _send_case(serial: str, bundle: str, source: Path, remote_path: str) -> None:
    _raise_if_failed(_hdc(serial, "file", "send", "-b", bundle, str(source), remote_path, timeout=120))


def _export_report(serial: str, bundle: str, remote_path: str, output_dir: Path) -> Path:
    destination = output_dir / "phone_report.json"
    _raise_if_failed(_hdc(serial, "file", "recv", "-b", bundle, remote_path, str(destination), timeout=120))
    if not destination.is_file():
        raise RuntimeError("HDC did not export the phone report")
    return destination


def run_phone_owned_harmony_test(
    *,
    testcase_path: Path,
    serial: str,
    output_dir: Path,
    api_key_file: Path | None = None,
    bundle: str = DEFAULT_BUNDLE,
    timeout_seconds: int = 600,
) -> dict[str, Any]:
    """Execute a testcase inside the installed HAP and offline-intake its report."""
    testcase = json.loads(testcase_path.read_text(encoding="utf-8"))
    test_case_id = str(testcase.get("test_case_id") or "")
    if not test_case_id:
        raise ValueError("test case is missing test_case_id")
    if api_key_file is None or not api_key_file.is_file():
        raise ValueError("a readable temporary model credential file is required for a phone-owned run")
    output_dir.mkdir(parents=True, exist_ok=True)
    file_name = f"mobiagent-phone-case-{int(time.time() * 1000)}.json"
    remote_case = f"/data/storage/el2/base/haps/entry/files/{file_name}"
    token = secrets.token_urlsafe(32)
    model_api_key = api_key_file.read_text(encoding="utf-8").strip()
    if not model_api_key:
        raise ValueError("temporary model credential file is empty")
    server = HdcBridgeServer(HdcBridgeService(serial=serial, token=token), port=PC_PORT)
    sent = False
    try:
        server.start()
        _hdc_rport(serial, PHONE_PORT, server.port)
        # HDC acknowledges rport creation before the phone-side TCP listener
        # is always usable.  Do not let a freshly foregrounded HAP race that
        # transport setup and misclassify the resulting peer error as a test
        # execution failure.
        time.sleep(1.5)
        _send_case(serial, bundle, testcase_path, remote_case)
        sent = True
        started_at_ms = int(time.time() * 1000)
        _hdc_provision_phone(
            serial,
            bundle,
            PHONE_PORT,
            token,
            model_api_key=model_api_key,
            test_case_file=file_name,
        )
        remote_report = _wait_for_phone_report(serial, test_case_id, started_at_ms, timeout_seconds)
        report_path = _export_report(serial, bundle, remote_report, output_dir)
        intake = intake_report(report_path, output_dir / "pc_offline_review")
        payload = json.loads(report_path.read_text(encoding="utf-8-sig"))
        return {
            "status": "HARMONY_PHONE_AGENT_COMPLETE",
            "test_case_id": test_case_id,
            "device_serial": serial,
            "phone_status": payload.get("status"),
            "remote_report": remote_report,
            "report_path": str(report_path.resolve()),
            "offline_review": intake["summary"],
        }
    finally:
        # The testcase and all session values are per-run.  Neither output
        # directory nor bridge logs receive the model credential.
        if sent:
            _hdc(serial, "shell", "-b", bundle, "rm", "-f", remote_case, timeout=30)
        server.close()
