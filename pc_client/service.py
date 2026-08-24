"""Testable application service used by the PC desktop UI."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from app_test_agent.mobiagent_executor import (
    MobiAgentStepExecutor,
    prepare_mobiagent_preflight,
)
from app_test_agent.mock_executor import MOCK_SCENARIOS, MockStepExecutor, ScriptedStepExecutor
from app_test_agent.orchestrator import run_app_test
from app_test_agent.schema import load_test_case
from app_test_agent.verification_runner import MobiAgentVerificationRunner
from verification_benchmark.evaluation_framework.app_test_manifest_intake import (
    load_app_test_manifest_evidence,
)


DEVICE_MUTATION_CONFIRMATION = "I_UNDERSTAND_DEVICE_MUTATION"


class PcEvaluationMode:
    MANIFEST_REPLAY = "MANIFEST_REPLAY"
    MOCK = "MOCK"
    DEVICE_PREFLIGHT = "DEVICE_PREFLIGHT"
    DEVICE_EXECUTION = "DEVICE_EXECUTION"

    ALL = frozenset(
        {
            MANIFEST_REPLAY,
            MOCK,
            DEVICE_PREFLIGHT,
            DEVICE_EXECUTION,
        }
    )


class PcEvaluationValidationError(ValueError):
    pass


@dataclass(frozen=True)
class PcEvaluationRequest:
    mode: str
    test_case_path: Path
    output_dir: Path
    manifest_path: Path | None = None
    recompute_step_gates: bool = True
    mock_scenario: str = "pass"
    device: str = "Harmony"
    device_serial: str | None = None
    runner_root: Path | None = None
    device_mutation_confirmation: str | None = None

    def validated(self) -> "PcEvaluationRequest":
        mode = str(self.mode).strip().upper()
        if mode not in PcEvaluationMode.ALL:
            raise PcEvaluationValidationError(f"不支持的运行模式：{self.mode}")
        test_case_path = self.test_case_path.resolve(strict=True)
        if not test_case_path.is_file():
            raise PcEvaluationValidationError("测试用例路径必须是文件")
        output_dir = self.output_dir.resolve()
        manifest_path = self.manifest_path
        if mode == PcEvaluationMode.MANIFEST_REPLAY:
            if manifest_path is None:
                raise PcEvaluationValidationError("离线回放必须选择 execution manifest")
            manifest_path = manifest_path.resolve(strict=True)
            if not manifest_path.is_file():
                raise PcEvaluationValidationError("execution manifest 路径必须是文件")
        scenario = str(self.mock_scenario).strip().lower()
        if mode == PcEvaluationMode.MOCK and scenario not in MOCK_SCENARIOS:
            raise PcEvaluationValidationError(f"不支持的 Mock 场景：{scenario}")
        runner_root = self.runner_root.resolve(strict=True) if self.runner_root else None
        serial = str(self.device_serial or "").strip() or None
        if mode == PcEvaluationMode.DEVICE_EXECUTION:
            if self.device_mutation_confirmation != DEVICE_MUTATION_CONFIRMATION:
                raise PcEvaluationValidationError("真机执行尚未完成设备副作用确认")
            if not serial:
                raise PcEvaluationValidationError("真机执行必须填写设备序列号")
        return PcEvaluationRequest(
            mode=mode,
            test_case_path=test_case_path,
            output_dir=output_dir,
            manifest_path=manifest_path,
            recompute_step_gates=bool(self.recompute_step_gates),
            mock_scenario=scenario,
            device=str(self.device or "Harmony").strip() or "Harmony",
            device_serial=serial,
            runner_root=runner_root,
            device_mutation_confirmation=self.device_mutation_confirmation,
        )


@dataclass(frozen=True)
class PcEvaluationResult:
    status: str
    mode: str
    output_dir: Path
    overall_result: str | None = None
    attribution: str | None = None
    report_path: Path | None = None
    summary: Mapping[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "mode": self.mode,
            "output_dir": str(self.output_dir),
            "overall_result": self.overall_result,
            "attribution": self.attribution,
            "report_path": str(self.report_path) if self.report_path else None,
            "summary": dict(self.summary or {}),
        }


def run_pc_evaluation(request: PcEvaluationRequest) -> PcEvaluationResult:
    """Execute one desktop request through the canonical App-test pipeline."""

    request = request.validated()
    test_case = load_test_case(request.test_case_path)
    if request.mode == PcEvaluationMode.DEVICE_PREFLIGHT:
        preflight = prepare_mobiagent_preflight(
            test_case,
            request.output_dir,
            device=request.device,
            device_serial=request.device_serial,
            runner_root=request.runner_root,
        )
        return PcEvaluationResult(
            status="MOBIAGENT_PREFLIGHT_COMPLETE",
            mode=request.mode,
            output_dir=preflight.output_dir,
            summary={
                **preflight.as_dict(),
                "safety": "NO_DEVICE_MUTATION",
            },
        )

    verification_runner = None
    intake_summary: Mapping[str, Any] | None = None
    if request.mode == PcEvaluationMode.MANIFEST_REPLAY:
        assert request.manifest_path is not None
        intake = load_app_test_manifest_evidence(
            test_case=test_case,
            test_case_path=request.test_case_path,
            manifest_path=request.manifest_path,
            recompute_step_gates=request.recompute_step_gates,
        )
        executor = ScriptedStepExecutor(
            intake.execution_record,
            name="pc_client_manifest_replay",
        )
        intake_summary = intake.as_intake_summary()
    elif request.mode == PcEvaluationMode.MOCK:
        executor = MockStepExecutor(scenario=request.mock_scenario)
    else:
        executor = MobiAgentStepExecutor(
            output_dir=request.output_dir,
            device=request.device,
            device_serial=request.device_serial,
            runner_root=request.runner_root,
        )
        verification_runner = MobiAgentVerificationRunner(
            output_dir=request.output_dir,
            device=request.device,
            device_serial=request.device_serial,
            runner_root=request.runner_root,
        )

    report = run_app_test(
        test_case,
        executor,
        request.output_dir,
        verification_runner=verification_runner,
    )
    attribution = report.get("attribution")
    attribution_name = (
        str(attribution.get("attribution"))
        if isinstance(attribution, Mapping) and attribution.get("attribution") is not None
        else None
    )
    report_path = request.output_dir / "report.md"
    return PcEvaluationResult(
        status="APP_TEST_EVALUATION_COMPLETE",
        mode=request.mode,
        output_dir=request.output_dir,
        overall_result=str(report.get("overall_result")),
        attribution=attribution_name,
        report_path=report_path if report_path.is_file() else None,
        summary={
            "test_case_id": test_case.test_case_id,
            "step_count": len(test_case.steps),
            "intake": dict(intake_summary) if intake_summary is not None else None,
            "device_mutation": request.mode == PcEvaluationMode.DEVICE_EXECUTION,
        },
    )


__all__ = [
    "DEVICE_MUTATION_CONFIRMATION",
    "PcEvaluationMode",
    "PcEvaluationRequest",
    "PcEvaluationResult",
    "PcEvaluationValidationError",
    "run_pc_evaluation",
]
