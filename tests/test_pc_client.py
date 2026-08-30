from pathlib import Path

import pytest
import pc_client.service as pc_service

from pc_client.service import (
    DEVICE_MUTATION_CONFIRMATION,
    PcEvaluationMode,
    PcEvaluationRequest,
    PcEvaluationValidationError,
    format_model_event_for_display,
    run_pc_evaluation,
)
from utils.load_md_prompt import REQUIRED_RUNTIME_PROMPTS, validate_runtime_prompt_assets


ROOT = Path(__file__).resolve().parents[1]
CASE = ROOT / "examples" / "post_create_app_test.json"


def test_pc_runtime_prompt_assets_load_for_source_and_frozen_layout_contract():
    loaded = validate_runtime_prompt_assets()

    assert loaded == REQUIRED_RUNTIME_PROMPTS
    assert "grounder_qwen3_bbox.md" in loaded
    assert "planner_oneshot_harmony.md" in loaded


def test_pc_build_and_acceptance_require_frozen_runtime_prompts():
    build_script = (ROOT / "build_pc_client.ps1").read_text(encoding="utf-8")
    acceptance_script = (ROOT / "verify_pc_release.ps1").read_text(encoding="utf-8")

    assert '--add-data "$RepoRoot\\prompts;prompts"' in build_script
    assert "Assert-RuntimePromptSmoke" in acceptance_script
    assert "runtime_prompt_assets" in acceptance_script


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
    live_events = []
    event_sink = live_events.append
    request = PcEvaluationRequest(
        mode=PcEvaluationMode.DEVICE_EXECUTION,
        test_case_path=CASE,
        output_dir=tmp_path / "device_run",
        device_serial="device-001",
        runner_root=ROOT,
        model_event_sink=event_sink,
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
    assert confirmed.model_event_sink is event_sink


def test_pc_model_event_display_includes_reasoning_and_retry_state():
    response = format_model_event_for_display(
        {
            "event_type": "MODEL_RESPONSE_RECEIVED",
            "role": "Decider",
            "step_id": "open_editor",
            "business_attempt": 1,
            "model_attempt": 1,
            "duration_ms": 321,
            "response_text": '{"reasoning":"open the editor","action":"click"}',
        }
    )
    failure = format_model_event_for_display(
        {
            "event_type": "MODEL_ATTEMPT_FAILED",
            "role": "Grounder",
            "step_id": "open_editor",
            "business_attempt": 1,
            "model_attempt": 1,
            "error_type": "ValueError",
            "error": "bbox missing",
            "retry_scheduled": True,
        }
    )

    assert "[Decider]" in response
    assert "open the editor" in response
    assert "321 ms" in response
    assert "[Grounder]" in failure
    assert "retry_scheduled" in failure
    assert "bbox missing" in failure


def test_pc_device_execution_forwards_live_model_event_sink(monkeypatch, tmp_path):
    captured = {}
    event_sink = lambda event: captured.setdefault("event", event)

    class FakeExecutor:
        def __init__(self, **kwargs):
            captured["executor_kwargs"] = kwargs

    class FakeVerificationRunner:
        def __init__(self, **kwargs):
            captured["verification_kwargs"] = kwargs

    monkeypatch.setattr(pc_service, "MobiAgentStepExecutor", FakeExecutor)
    monkeypatch.setattr(
        pc_service,
        "MobiAgentVerificationRunner",
        FakeVerificationRunner,
    )
    monkeypatch.setattr(
        pc_service,
        "run_app_test",
        lambda *_args, **_kwargs: {
            "overall_result": "INCONCLUSIVE",
            "attribution": {"attribution": "INCONCLUSIVE"},
        },
    )

    result = run_pc_evaluation(
        PcEvaluationRequest(
            mode=PcEvaluationMode.DEVICE_EXECUTION,
            test_case_path=CASE,
            output_dir=tmp_path / "device_run",
            device_serial="device-001",
            runner_root=ROOT,
            device_mutation_confirmation=DEVICE_MUTATION_CONFIRMATION,
            model_event_sink=event_sink,
        )
    )

    assert result.overall_result == "INCONCLUSIVE"
    assert captured["executor_kwargs"]["model_event_sink"] is event_sink


def test_pc_client_manifest_mode_requires_manifest(tmp_path):
    request = PcEvaluationRequest(
        mode=PcEvaluationMode.MANIFEST_REPLAY,
        test_case_path=CASE,
        output_dir=tmp_path / "replay",
        runner_root=ROOT,
    )

    with pytest.raises(PcEvaluationValidationError, match="manifest"):
        request.validated()
