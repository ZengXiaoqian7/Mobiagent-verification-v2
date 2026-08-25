from __future__ import annotations

import pytest
from types import SimpleNamespace

import app_test_agent.harmony_native_runner as native_runner
from app_test_agent.harmony_native_runner import _validate_runner_output


def _output(*, failure: int = 0, error: int = 0, passed: int = 1) -> str:
    return (
        "OHOS_REPORT_RESULT: stream=Tests run: 1, "
        f"Failure: {failure}, Error: {error}, Pass: {passed}, Ignore: 0\n"
        "TestFinished-ResultCode: 0"
    )


def test_native_runner_accepts_hypium_pass_and_zero_failures():
    _validate_runner_output(_output())


@pytest.mark.parametrize(
    "failure,error",
    [(1, 0), (0, 1)],
)
def test_native_runner_rejects_hypium_failure_or_error(failure: int, error: int):
    with pytest.raises(RuntimeError, match="OHOS_REPORT_RESULT"):
        _validate_runner_output(_output(failure=failure, error=error, passed=0))


def test_native_runner_rejects_result_code_even_when_report_passes():
    output = _output().replace("TestFinished-ResultCode: 0", "TestFinished-ResultCode: 1")
    with pytest.raises(RuntimeError, match="Harmony test failed"):
        _validate_runner_output(output)


def test_native_runner_rejects_missing_aggregate_result():
    with pytest.raises(RuntimeError, match="missing OHOS_REPORT_RESULT"):
        _validate_runner_output("TestFinished-ResultCode: 0")


def test_native_runner_passes_temporary_secret_only_by_sandbox_path(monkeypatch):
    calls = []

    def fake_hdc(serial, *args, **kwargs):
        calls.append((serial, args, kwargs))
        return SimpleNamespace(returncode=0, stdout=_output(), stderr="")

    monkeypatch.setattr(native_runner, "_hdc", fake_hdc)
    native_runner._run_test(
        serial="device-1",
        bundle="com.example.probe",
        module="entry_test",
        testcase_remote_path="/data/storage/el2/base/files/case.json",
        api_key_remote_path="/data/storage/el2/base/files/model-secret.txt",
        timeout_seconds=60,
    )
    command = calls[0][1]
    assert "api_key_path" in command
    assert "/data/storage/el2/base/files/model-secret.txt" in command
    assert "temporary-secret-value" not in command


def test_native_runner_cli_can_resume_user_interaction(monkeypatch):
    calls = []
    monkeypatch.setattr(
        native_runner,
        "send_user_interaction_response",
        lambda **kwargs: calls.append(kwargs),
    )
    assert native_runner.main(
        [
            "--app-test-device-serial",
            "device-1",
            "--user-response",
            "继续",
        ]
    ) == 0
    assert calls == [
        {
            "serial": "device-1",
            "response": "继续",
            "bundle": "com.zengxq.mobiagentprobe",
        }
    ]
