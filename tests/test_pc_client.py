from pathlib import Path

import pytest

from pc_client.service import (
    DEVICE_MUTATION_CONFIRMATION,
    PcEvaluationMode,
    PcEvaluationRequest,
    PcEvaluationValidationError,
    run_pc_evaluation,
)


ROOT = Path(__file__).resolve().parents[1]
CASE = ROOT / "examples" / "post_create_app_test.json"


def test_pc_client_mock_runs_canonical_report_pipeline(tmp_path):
    result = run_pc_evaluation(
        PcEvaluationRequest(
            mode=PcEvaluationMode.MOCK,
            test_case_path=CASE,
            output_dir=tmp_path / "mock_run",
            mock_scenario="pass",
            runner_root=ROOT,
        )
    )

    assert result.overall_result == "APP_PASS"
    assert result.report_path == tmp_path / "mock_run" / "report.md"
    assert result.report_path.is_file()
    assert result.summary["device_mutation"] is False


def test_pc_client_device_execution_requires_explicit_mutation_confirmation(tmp_path):
    request = PcEvaluationRequest(
        mode=PcEvaluationMode.DEVICE_EXECUTION,
        test_case_path=CASE,
        output_dir=tmp_path / "device_run",
        device_serial="device-001",
        runner_root=ROOT,
    )

    with pytest.raises(PcEvaluationValidationError, match="副作用确认"):
        request.validated()

    confirmed = PcEvaluationRequest(
        **{
            **request.__dict__,
            "device_mutation_confirmation": DEVICE_MUTATION_CONFIRMATION,
        }
    ).validated()
    assert confirmed.device_serial == "device-001"


def test_pc_client_manifest_mode_requires_manifest(tmp_path):
    request = PcEvaluationRequest(
        mode=PcEvaluationMode.MANIFEST_REPLAY,
        test_case_path=CASE,
        output_dir=tmp_path / "replay",
        runner_root=ROOT,
    )

    with pytest.raises(PcEvaluationValidationError, match="manifest"):
        request.validated()
