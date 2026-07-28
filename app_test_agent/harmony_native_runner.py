"""Run the native Harmony ohosTest App-test agent and intake its report."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import tempfile
import time
from typing import Sequence

from .harmony_intake import intake_report


REPORT_PATTERN = re.compile(
    r"(/data/storage/el2/base/files/reports/[A-Za-z0-9_-]+-\d+\.json)"
)
OHOS_REPORT_RESULT_PATTERN = re.compile(
    r"\b(Failure|Error|Pass)\s*:\s*(\d+)\b", re.IGNORECASE
)
USER_RESPONSE_REMOTE_PATH = "/data/storage/el2/base/files/mobiagent-user-interaction-response.json"


def _hdc(serial: str | None, *args: str, timeout: int = 180) -> subprocess.CompletedProcess[str]:
    command = ["hdc"]
    if serial:
        command.extend(["-t", serial])
    command.extend(args)
    return subprocess.run(
        command,
        text=True,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def _run_test(
    *,
    serial: str,
    bundle: str,
    module: str,
    testcase_remote_path: str,
    api_key_remote_path: str | None,
    timeout_seconds: int,
) -> str:
    command = [
        "shell",
        "aa",
        "test",
        "-b",
        bundle,
        "-m",
        module,
        "-s",
        "unittest",
        "OpenHarmonyTestRunner",
        "-s",
        "test_case_path",
        testcase_remote_path,
    ]
    if api_key_remote_path is not None:
        command.extend(["-s", "api_key_path", api_key_remote_path])
    command.extend(["-s", "timeout", str(timeout_seconds * 1000)])
    result = _hdc(
        serial,
        *command,
        timeout=timeout_seconds + 60,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    _validate_runner_output(result.stdout)
    return result.stdout


def _validate_runner_output(output: str) -> None:
    """Validate both Hypium's aggregate result and its process result code."""
    if "TestFinished-ResultCode: 0" not in output:
        raise RuntimeError(f"Harmony test failed:\n{output}")
    report_lines = [
        line for line in output.splitlines() if "OHOS_REPORT_RESULT" in line
    ]
    if not report_lines:
        raise RuntimeError("Harmony test failed: missing OHOS_REPORT_RESULT")
    counts = {"failure": 0, "error": 0, "pass": 0}
    for line in report_lines:
        for name, value in OHOS_REPORT_RESULT_PATTERN.findall(line):
            counts[name.lower()] += int(value)
    if counts["failure"] > 0 or counts["error"] > 0:
        raise RuntimeError(
            "Harmony test failed: "
            f"OHOS_REPORT_RESULT Failure={counts['failure']} "
            f"Error={counts['error']} Pass={counts['pass']}"
        )
    if counts["pass"] <= 0:
        raise RuntimeError(
            "Harmony test failed: OHOS_REPORT_RESULT has no passing test "
            f"(Failure={counts['failure']} Error={counts['error']} Pass={counts['pass']})"
        )


def _send_sandbox_file(
    serial: str,
    bundle: str,
    local_path: Path,
    remote_path: str,
) -> None:
    result = _hdc(
        serial,
        "file",
        "send",
        "-b",
        bundle,
        str(local_path),
        remote_path,
        timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())


def send_user_interaction_response(
    *,
    serial: str,
    response: str,
    bundle: str = "com.zengxq.mobiagentprobe",
) -> None:
    """Resume a paused native run through its sandbox response file."""
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        suffix=".json",
        delete=False,
    ) as handle:
        local_path = Path(handle.name)
        json.dump(
            {
                "schema_version": "mobiagent-user-interaction-v1",
                "response": response,
                "responded_at": int(time.time() * 1000),
            },
            handle,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    try:
        result = _hdc(
            serial,
            "file",
            "send",
            "-b",
            bundle,
            str(local_path),
            USER_RESPONSE_REMOTE_PATH,
            timeout=60,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    finally:
        local_path.unlink(missing_ok=True)


def _latest_report(serial: str, test_case_id: str, started_at_ms: int) -> str:
    logs = _hdc(serial, "shell", "hilog", "-x", timeout=60)
    if logs.returncode != 0:
        raise RuntimeError(logs.stderr.strip() or logs.stdout.strip())
    candidates: list[tuple[int, str]] = []
    for match in REPORT_PATTERN.finditer(logs.stdout):
        path = match.group(1)
        if f"/{test_case_id}-" not in path:
            continue
        timestamp_text = path.rsplit("-", 1)[-1].removesuffix(".json")
        try:
            timestamp = int(timestamp_text)
        except ValueError:
            continue
        if timestamp >= started_at_ms:
            candidates.append((timestamp, path))
    if not candidates:
        raise RuntimeError(
            f"Harmony test completed but no report was found for {test_case_id}"
        )
    return max(candidates)[1]


def _export_report(serial: str, bundle: str, remote_path: str, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    destination = output_dir / "phone_report.json"
    result = _hdc(
        serial,
        "file",
        "recv",
        "-b",
        bundle,
        remote_path,
        str(destination),
        timeout=120,
    )
    if result.returncode != 0 or not destination.is_file():
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return destination


def run_native_harmony_test(
    *,
    testcase_path: Path,
    serial: str,
    output_dir: Path,
    bundle: str = "com.zengxq.mobiagentprobe",
    module: str = "entry_test",
    timeout_seconds: int = 60,
    api_key_file: Path | None = None,
) -> dict[str, object]:
    testcase = json.loads(testcase_path.read_text(encoding="utf-8"))
    test_case_id = str(testcase.get("test_case_id") or "")
    if not test_case_id:
        raise ValueError("test case is missing test_case_id")
    output_dir.mkdir(parents=True, exist_ok=True)
    local_test_case = output_dir / "test_case.normalized.json"
    local_test_case.write_text(
        json.dumps(testcase, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    remote_test_case = f"/data/storage/el2/base/files/mobiagent-test-case-{int(time.time() * 1000)}.json"
    if api_key_file is not None:
        if not api_key_file.is_file():
            raise ValueError("--api-key-file must name a readable regular file")
        remote_api_key = f"/data/storage/el2/base/files/mobiagent-model-secret-{int(time.time() * 1000)}.txt"
    else:
        remote_api_key = None
    _send_sandbox_file(serial, bundle, local_test_case, remote_test_case)
    try:
        if api_key_file is not None and remote_api_key is not None:
            _send_sandbox_file(serial, bundle, api_key_file, remote_api_key)
        started_at_ms = int(time.time() * 1000)
        runner_output = _run_test(
            serial=serial,
            bundle=bundle,
            module=module,
            testcase_remote_path=remote_test_case,
            api_key_remote_path=remote_api_key,
            timeout_seconds=timeout_seconds,
        )
    finally:
        _hdc(serial, "shell", "-b", bundle, "rm", "-f", remote_test_case, timeout=30)
        if remote_api_key is not None:
            _hdc(serial, "shell", "-b", bundle, "rm", "-f", remote_api_key, timeout=30)
    remote_report = _latest_report(serial, test_case_id, started_at_ms)
    report_path = _export_report(serial, bundle, remote_report, output_dir)
    offline_dir = output_dir / "pc_offline_review"
    intake = intake_report(report_path, offline_dir)
    return {
        "status": "HARMONY_NATIVE_TEST_COMPLETE",
        "test_case_id": test_case_id,
        "device_serial": serial,
        "remote_report": remote_report,
        "report_path": str(report_path.resolve()),
        "offline_review": intake["summary"],
        "runner_output": runner_output,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-test-case", type=Path)
    parser.add_argument("--app-test-device-serial", required=True)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--device-bundle", default="com.zengxq.mobiagentprobe")
    parser.add_argument("--test-module", default="entry_test")
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument(
        "--api-key-file",
        type=Path,
        help="temporary local model-secret file; it is sent only to the app sandbox and never copied to the output directory",
    )
    parser.add_argument("--user-response", help="respond to a paused info/call_user action and exit")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.user_response is not None:
        send_user_interaction_response(
            serial=args.app_test_device_serial,
            response=args.user_response,
            bundle=args.device_bundle,
        )
        return 0
    if args.app_test_case is None or args.output_dir is None:
        raise SystemExit("--app-test-case and --output-dir are required unless --user-response is used")
    result = run_native_harmony_test(
        testcase_path=args.app_test_case,
        serial=args.app_test_device_serial,
        output_dir=args.output_dir,
        bundle=args.device_bundle,
        module=args.test_module,
        timeout_seconds=args.timeout,
        api_key_file=args.api_key_file,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
