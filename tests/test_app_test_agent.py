from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

from PIL import Image
import pytest

import app_test_agent.mobiagent_executor as mobiagent_executor_module
from app_test_agent.executor import (
    EvidenceState,
    ExecutionRecord,
    StepExecutionResult,
    StepStatus,
    completed_step,
)
import app_test_agent.execution_verifier as execution_verifier
from app_test_agent.contract import compile_app_test_contract
from app_test_agent.manifest import ManifestIntakeError, load_execution_manifest
from app_test_agent.manifest_executor import ManifestReplayExecutor
from app_test_agent.mobiagent_executor import (
    MOBIAGENT_STEP_PAYLOAD_SCHEMA_VERSION,
    MobiAgentStepExecutor,
    _resolve_decider_aligned_text_target,
    _resolve_exact_text_target,
    _resolve_hierarchy_control_target,
    _needs_navigation_context_recovery,
    _retry_is_safe,
    _TargetNotFound,
    _evaluate_post_action_context,
    prepare_mobiagent_preflight,
    reject_unimplemented_device_execution,
)
from app_test_agent.mock_executor import MockStepExecutor
from app_test_agent.mock_executor import ScriptedStepExecutor
from app_test_agent.orchestrator import run_app_test
from app_test_agent.raw_step_gate_replay import recompute_step_gates_from_raw_trace
from app_test_agent.offline_verifier import OfflineTraceRole, review_app_test_trace
from app_test_agent.run_envelope import canonical_sha256, load_run_envelope
from app_test_agent.schema import (
    TestCaseError as AppTestCaseError,
    TestCaseSpec as AppTestCaseSpec,
    load_test_case,
)
from app_test_agent.step_gate import (
    evaluate_dispatch_failure_gate,
    evaluate_micro_action_gate,
    evaluate_step_gate,
)
from app_test_agent.verifier import OverallResult
from app_test_agent.verification_runner import (
    MobiAgentVerificationRunner,
    ScriptedVerificationRunner,
    VerificationRunResult,
    VerificationRunStatus,
    VerificationStepResult,
    _dangerous_step_text,
)
from app_test_agent.verification_intent import (
    compile_verification_intent,
    effective_verification_steps,
)
from verification_benchmark.evaluation_framework.app_test_manifest_intake import (
    load_app_test_manifest_evidence,
)
from verification_benchmark.evaluation_framework.app_test_replay_baseline import (
    summarize_replay_rows,
)


ROOT = Path(__file__).resolve().parents[1]
CASE = ROOT / "examples" / "post_create_app_test.json"
MINIMAL_USER_CASE = ROOT / "examples" / "minimal_user_view_app_test.json"


def _run(scenario: str):
    return run_app_test(
        load_test_case(CASE),
        MockStepExecutor(scenario=scenario),
    )


def _case_payload() -> dict:
    return json.loads(CASE.read_text(encoding="utf-8"))


def _frame(frame_id: int, texts: tuple[str, ...], relative: int = 500) -> dict:
    return {
        "frame_id": frame_id,
        "timestamp_ms": frame_id * 1000,
        "relative_to_action_ms": 0 if frame_id == 0 else relative,
        "screenshot": f"mock://test/{frame_id}.png",
        "screenshot_sha256": f"{frame_id:064x}",
        "hierarchy": f"mock://test/{frame_id}.xml",
        "hierarchy_sha256": f"{frame_id + 1:064x}",
        "stability": "STABLE",
        "visible_texts": list(texts),
    }


def _scripted_record(
    spec: AppTestCaseSpec,
    *,
    visible_texts: tuple[str, ...] = ("Feed", "hello test 123"),
    initial_texts: tuple[str, ...] = ("Feed",),
    after_submit_texts: tuple[str, ...] | None = None,
    evidence_sufficient: bool = True,
) -> ExecutionRecord:
    steps = tuple(completed_step(step, spec, index) for index, step in enumerate(spec.steps))
    frame_offsets: dict[int, int] = {}
    max_wait = spec.observation_policy.get("max_wait_ms")
    delays = spec.observation_policy.get("delays_ms", ())
    if (
        isinstance(max_wait, int)
        and not isinstance(max_wait, bool)
        and max_wait >= 500
        and isinstance(delays, (list, tuple))
    ):
        offsets = (
            ([0] if spec.observation_policy.get("immediate") is True else [])
            + sorted(
                {
                    value
                    for value in delays
                    if isinstance(value, int)
                    and not isinstance(value, bool)
                    and 0 <= value <= max_wait
                }
            )
        )
        selected_step_ids = {
            assertion.after_step
            for assertion in spec.expected_results
            if assertion.type in {"TEXT_VISIBLE", "TEXT_ABSENT"}
            and assertion.after_step is not None
        }
        if spec.forbidden_effects and steps:
            selected_step_ids.add(steps[-1].step_id)
        next_frame_id = max(
            frame_id
            for result in steps
            for frame_id in (
                *((result.pre_frame,) if result.pre_frame is not None else ()),
                *result.post_frames,
            )
        ) + 1
        expanded: list[StepExecutionResult] = []
        for result in steps:
            if result.step_id not in selected_step_ids or not result.post_frames or not offsets:
                expanded.append(result)
                continue
            frame_ids = [result.post_frames[0]]
            while len(frame_ids) < len(offsets):
                frame_ids.append(next_frame_id)
                next_frame_id += 1
            frame_offsets.update(dict(zip(frame_ids, offsets)))
            expanded.append(replace(result, post_frames=tuple(frame_ids)))
        steps = tuple(expanded)
    frame_texts = {"0": list(initial_texts)}
    frames = [_frame(0, initial_texts, 0)]
    for result in steps:
        texts = after_submit_texts if result.step_id == "submit_post" and after_submit_texts is not None else visible_texts
        for frame_id in result.post_frames:
            frame_texts[str(frame_id)] = list(texts)
            frames.append(_frame(frame_id, texts, frame_offsets.get(frame_id, 500)))
    return ExecutionRecord(
        test_case_id=spec.test_case_id,
        executor="script-fixture",
        step_results=steps,
        final_state=EvidenceState(
            visible_texts=visible_texts,
            state_changed=True,
            success_signals=("success",),
            evidence_sufficient=evidence_sufficient,
        ),
        metadata={
            "initial_visible_texts": list(initial_texts),
            "frame_visible_texts": frame_texts,
            "frames": frames,
        },
    )


def test_raw_step_gate_replay_rejects_historical_success_for_wrong_raw_target(tmp_path):
    spec = load_test_case(CASE)
    step = spec.steps[0]
    trace_dir = tmp_path / "raw_trace"
    trace_dir.mkdir()
    (trace_dir / "actions.json").write_text(
        json.dumps(
            {
                "actions": [
                    {
                        "action_index": 1,
                        "step_id": step.step_id,
                        "type": "click",
                        "target_match": False,
                        "click_point": [20, 20],
                        "resolved_bounds": [10, 10, 30, 30],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    historical = StepExecutionResult(
        step_id=step.step_id,
        status=StepStatus.STEP_COMPLETED,
        action_type=step.action_type,
        target=step.target,
        pre_frame=0,
        post_frames=(1,),
        evidence={
            "action_ids": [1],
            "gate_decision": "CONTINUE",
            "target_evidence": "CONFORMANT",
            "action_conformance": "CONFORMANT",
        },
    )
    record = ExecutionRecord(
        test_case_id=spec.test_case_id,
        executor="historical-fixture",
        step_results=(historical,),
        final_state=EvidenceState(),
        raw_trace_dir=str(trace_dir),
        metadata={
            "frames": [
                _frame(0, ("Feed", "Post"), 0),
                _frame(1, ("Editor",), 500),
            ]
        },
    )

    replayed = recompute_step_gates_from_raw_trace(spec, record)

    result = replayed.step_results[0]
    audit = result.evidence["raw_step_gate_replay"]
    assert result.status == StepStatus.STEP_FAILED
    assert result.evidence["gate_decision"] == "TEST_EXECUTION_FAIL"
    assert result.evidence["target_evidence"] == "NON_CONFORMANT"
    assert audit["historical"]["status"] == StepStatus.STEP_COMPLETED
    assert audit["current"]["status"] == StepStatus.STEP_FAILED
    assert audit["changed"] is True


def test_raw_step_gate_replay_preserves_terminal_environment_classification(tmp_path):
    spec = load_test_case(CASE)
    step = spec.steps[0]
    trace_dir = tmp_path / "raw_trace"
    trace_dir.mkdir()
    historical = StepExecutionResult(
        step_id=step.step_id,
        status=StepStatus.ENV_BLOCKED,
        action_type=step.action_type,
        target=step.target,
        blocker="login required",
        error="login required",
    )
    record = ExecutionRecord(
        test_case_id=spec.test_case_id,
        executor="historical-fixture",
        step_results=(historical,),
        final_state=EvidenceState(),
        raw_trace_dir=str(trace_dir),
    )

    replayed = recompute_step_gates_from_raw_trace(spec, record)

    result = replayed.step_results[0]
    assert result.status == StepStatus.ENV_BLOCKED
    assert result.blocker == "login required"
    assert result.evidence["raw_step_gate_replay"]["recomputed"] is False


def test_raw_step_gate_replay_maps_unperformed_retry_to_inconclusive(tmp_path):
    payload = _case_payload()
    payload["steps"][0]["target"] = {
        "role": "conversation",
        "text_candidates": ["Editor"],
    }
    spec = AppTestCaseSpec.from_json(payload)
    step = spec.steps[0]
    trace_dir = tmp_path / "raw_trace"
    trace_dir.mkdir()
    (trace_dir / "actions.json").write_text(
        json.dumps(
            {
                "actions": [
                    {
                        "action_index": 1,
                        "step_id": step.step_id,
                        "type": "click",
                        "target_match": True,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    historical = StepExecutionResult(
        step_id=step.step_id,
        status=StepStatus.INCONCLUSIVE,
        action_type=step.action_type,
        target=step.target,
        pre_frame=0,
        post_frames=(1,),
        evidence={"action_ids": [1]},
    )
    record = ExecutionRecord(
        test_case_id=spec.test_case_id,
        executor="historical-fixture",
        step_results=(historical,),
        final_state=EvidenceState(),
        raw_trace_dir=str(trace_dir),
        metadata={
            "frames": [
                _frame(0, ("Feed", "Post"), 0),
                _frame(1, ("Feed", "Post"), 500),
            ]
        },
    )

    replayed = recompute_step_gates_from_raw_trace(spec, record)

    result = replayed.step_results[0]
    assert result.status == StepStatus.INCONCLUSIVE
    assert result.evidence["gate_decision"] == "RETRY"


def test_step_gate_rejects_input_dispatch_when_declared_value_never_reaches_ui():
    spec = load_test_case(CASE)
    step = spec.steps[1]
    expected_value = step.resolved_value(spec.test_data)

    gate = evaluate_step_gate(
        test_case=spec,
        step=step,
        action_record={
            "type": "click_input",
            "action_index": 2,
            "text": expected_value,
            "target_match": True,
            "click_point": [500, 500],
            "runtime_bounds": [400, 400, 600, 600],
            "input_effect": {
                "status": "NON_CONFORMANT",
                "expected_value": expected_value,
                "visible_texts": ["Editor", "Write something"],
            },
        },
        attempt=1,
        pre_frame=_frame(1, ("Editor", "Write something"), 0),
        post_frames=(_frame(2, ("Editor", "Write something"), 3000),),
        next_step=spec.steps[2],
    )

    assert gate.target_evidence == "CONFORMANT"
    assert gate.action_conformance == "NON_CONFORMANT"
    assert gate.gate_decision == "TEST_EXECUTION_FAIL"
    assert "input value" in gate.reason


def test_replay_baseline_reports_confusion_and_high_risk_error_rates():
    rows = [
        {
            "case_id": "pass",
            "availability": "EVALUATED",
            "ground_truth": "APP_PASS",
            "predicted": "APP_PASS",
            "correct": True,
        },
        {
            "case_id": "false-pass",
            "availability": "EVALUATED",
            "ground_truth": "APP_FAIL",
            "predicted": "APP_PASS",
            "correct": False,
        },
        {
            "case_id": "wrong-attribution",
            "availability": "EVALUATED",
            "ground_truth": "ENV_BLOCKED",
            "predicted": "TEST_EXECUTION_FAIL",
            "correct": False,
        },
        {
            "case_id": "missing",
            "availability": "UNAVAILABLE",
            "ground_truth": "INCONCLUSIVE",
            "predicted": None,
            "correct": None,
        },
    ]

    report = summarize_replay_rows(rows, recompute_step_gates=True)

    assert report["summary"]["configured_cases"] == 4
    assert report["summary"]["evaluated_cases"] == 3
    assert report["summary"]["unavailable_cases"] == 1
    assert report["summary"]["exact_accuracy"] == pytest.approx(1 / 3)
    assert report["summary"]["false_pass_rate"] == pytest.approx(1 / 3)
    assert report["summary"]["execution_misattribution_count"] == 1
    assert report["summary"]["attribution_error_rate"] == pytest.approx(2 / 3)
    assert report["confusion_matrix"]["APP_FAIL"]["APP_PASS"] == 1
    assert report["confusion_matrix"]["ENV_BLOCKED"]["TEST_EXECUTION_FAIL"] == 1


def _payload_with_verification_steps() -> dict:
    payload = _case_payload()
    payload["expected_results"] = [payload["expected_results"][0]]
    payload["verification_steps"] = [
        {
            "verification_step_id": "open_profile_feed",
            "instruction": "Navigate to the profile feed that should list the created post",
            "action_type": "NAVIGATE",
            "target": {"surface": "feed_or_post_detail"},
        },
        {
            "verification_step_id": "wait_for_feed",
            "instruction": "Wait for the result feed to load",
            "action_type": "WAIT",
            "timeout_seconds": 5,
        },
        {
            "verification_step_id": "observe_result_feed",
            "instruction": "Observe the feed for the unique post content",
            "action_type": "OBSERVE",
            "target": {"surface": "feed_or_post_detail"},
        },
    ]
    payload["verification_policy"] = {
        "max_steps": 5,
        "timeout_seconds": 20,
        "max_retries": 1,
    }
    return payload


def _direct_unknown_record(spec: AppTestCaseSpec) -> ExecutionRecord:
    return _scripted_record(
        spec,
        visible_texts=("Feed",),
        initial_texts=("Feed",),
        after_submit_texts=("Feed",),
        evidence_sufficient=False,
    )


class _SurfaceScopedVerificationRunner:
    name = "surface_scoped_fixture"

    def __init__(
        self,
        *,
        expected_text: str,
        text_before_surface: bool,
        text_after_surface: bool,
        report_surface_frame: bool = True,
        after_surface_extra_texts: tuple[str, ...] = (),
        verification_target: dict | None = None,
        before_base_texts: tuple[str, ...] = ("Feed",),
        surface_base_texts: tuple[str, ...] = ("笔记", "Other post"),
    ) -> None:
        self.expected_text = expected_text
        self.text_before_surface = text_before_surface
        self.text_after_surface = text_after_surface
        self.report_surface_frame = report_surface_frame
        self.after_surface_extra_texts = after_surface_extra_texts
        self.verification_target = verification_target or {}
        self.before_base_texts = before_base_texts
        self.surface_base_texts = surface_base_texts

    def execute(self, *, test_case, business_execution, contract):
        del business_execution
        before_texts = list(self.before_base_texts)
        if self.text_before_surface:
            before_texts.append(self.expected_text)
        surface_texts = list(self.surface_base_texts)
        if self.text_after_surface:
            surface_texts.append(self.expected_text)
        surface_texts.extend(self.after_surface_extra_texts)
        frames = [
            _frame(1, tuple(before_texts)),
            _frame(2, tuple(surface_texts)),
        ]
        record = ExecutionRecord(
            test_case_id=test_case.test_case_id,
            executor=self.name,
            step_results=(),
            final_state=EvidenceState(
                visible_texts=tuple(dict.fromkeys(before_texts + surface_texts)),
                evidence_sufficient=True,
                notes=("surface scoped fixture",),
            ),
            metadata={
                "frames": frames,
                "frame_visible_texts": {
                    "1": before_texts,
                    "2": surface_texts,
                },
            },
        )
        reached = (
            VerificationStepResult(
                verification_step_id="observe_surface",
                status=VerificationRunStatus.COMPLETED,
                action_type="OBSERVE",
                target=self.verification_target,
                observation_frames=(2,),
                reached_surface=True,
            )
            if self.report_surface_frame
            else VerificationStepResult(
                verification_step_id="observe_surface",
                status=VerificationRunStatus.COMPLETED,
                action_type="OBSERVE",
                target=self.verification_target,
                observation_frames=(2,),
                reached_surface=None,
            )
        )
        return VerificationRunResult(
            status=VerificationRunStatus.COMPLETED,
            used_runner=True,
            reason="surface fixture completed",
            target_surface="own_note_list",
            reached_surface=True,
            observation_sufficient=True,
            step_results=(reached,),
            observation_record=record,
            contract_sha256=contract.sha256,
        )


class _CountingVerificationRunner:
    name = "counting_verification_fixture"

    def __init__(self, scenario: str = "found") -> None:
        self.calls = 0
        self._runner = ScriptedVerificationRunner(scenario=scenario)

    def execute(self, *, test_case, business_execution, contract):
        self.calls += 1
        return self._runner.execute(
            test_case=test_case,
            business_execution=business_execution,
            contract=contract,
        )


def test_mock_pass_maps_to_app_pass():
    report = _run("pass")
    assert report["overall_result"] == OverallResult.APP_PASS
    assert report["execution_result"]["status"] == "COMPLETED"
    assert report["app_behavior_result"]["status"] == "SATISFIED"


def test_mock_app_fail_is_attributed_to_app():
    report = _run("app_fail")
    assert report["overall_result"] == OverallResult.APP_FAIL
    assert report["attribution"]["attribution"] == "APP_DEFECT"


def test_mock_execution_fail_is_not_app_fail():
    report = _run("execution_fail")
    assert report["overall_result"] == OverallResult.TEST_EXECUTION_FAIL
    assert report["app_behavior_result"]["status"] == "NOT_EVALUATED"


def test_mock_env_blocked_is_separate_result():
    report = _run("env_blocked")
    assert report["overall_result"] == OverallResult.ENV_BLOCKED


def test_mock_inconclusive_when_evidence_is_insufficient():
    report = _run("inconclusive")
    assert report["overall_result"] == OverallResult.INCONCLUSIVE


def test_stage2_wrong_step_order_is_execution_failure_not_app_failure():
    report = _run("wrong_order")
    assert report["overall_result"] == OverallResult.TEST_EXECUTION_FAIL
    assert report["attribution"]["attribution"] == "EXECUTOR"
    assert report["app_behavior_result"]["status"] == "NOT_EVALUATED"


def test_stage2_input_mismatch_is_execution_failure_not_app_failure():
    report = _run("input_mismatch")
    assert report["overall_result"] == OverallResult.TEST_EXECUTION_FAIL
    assert report["execution_result"]["failed_step"] == "input_post_content"
    assert report["app_behavior_result"]["status"] == "NOT_EVALUATED"


def test_stage2_environment_blocker_does_not_run_app_oracle():
    report = _run("env_blocked")
    assert report["overall_result"] == OverallResult.ENV_BLOCKED
    assert report["app_behavior_result"]["status"] == "NOT_EVALUATED"


def test_forbidden_effect_is_a_required_absence_constraint():
    report = _run("forbidden_effect")
    assert report["overall_result"] == OverallResult.APP_FAIL
    assert report["app_behavior_result"]["status"] == "VIOLATED"


def test_success_signal_with_insufficient_evidence_is_inconclusive():
    payload = _case_payload()
    payload["expected_results"] = [
        {"assertion_id": "success_signal", "type": "SUCCESS_SIGNAL"}
    ]
    spec = AppTestCaseSpec.from_json(payload)
    report = run_app_test(
        spec,
        ScriptedStepExecutor(_scripted_record(spec, evidence_sufficient=False)),
        run_id="success-signal-insufficient-evidence",
    )
    assert report["app_behavior_result"]["status"] == "UNKNOWN_EVIDENCE"
    assert report["overall_result"] == OverallResult.INCONCLUSIVE


def test_stage2_executor_unsupported_maps_to_unsupported():
    report = _run("unsupported")
    assert report["overall_result"] == OverallResult.UNSUPPORTED
    assert report["attribution"]["attribution"] == "SYSTEM_UNSUPPORTED"
    assert report["app_behavior_result"]["status"] == "NOT_EVALUATED"


def test_stage2_scripted_executor_can_replace_mock_backend():
    spec = load_test_case(CASE)
    record = _scripted_record(spec)
    report = run_app_test(spec, ScriptedStepExecutor(record))
    assert report["executor"] == "scripted"
    assert report["overall_result"] == OverallResult.APP_PASS


def test_stage2_optional_assertion_violation_does_not_fail_required_result():
    payload = _case_payload()
    payload["expected_results"].append(
        {
            "assertion_id": "optional_toast_visible",
            "type": "TEXT_VISIBLE",
            "expected_value": "optional toast",
            "required": False,
        }
    )
    spec = AppTestCaseSpec.from_json(payload)
    record = _scripted_record(spec)
    report = run_app_test(spec, ScriptedStepExecutor(record))
    assert report["overall_result"] == OverallResult.APP_PASS


def test_stage3_run_writes_execution_manifest(tmp_path):
    spec = load_test_case(CASE)
    report = run_app_test(
        spec,
        MockStepExecutor(scenario="pass"),
        output_dir=tmp_path,
        run_id="stage3-manifest-pass",
    )
    manifest_path = tmp_path / "test_execution_manifest.json"
    assert report["overall_result"] == OverallResult.APP_PASS
    assert manifest_path.is_file()
    manifest = load_execution_manifest(manifest_path, spec)
    assert manifest.run_id == "stage3-manifest-pass"
    assert manifest.test_case_sha256 == spec.sha256
    assert [step.step_id for step in manifest.steps] == [
        step.step_id for step in spec.steps
    ]
    assert manifest.steps[0].dispatch_status == "ACTION_DISPATCHED"
    assert manifest.steps[0].conformance_status == "CONFORMANT"
    assert manifest.steps[0].effect_status == "NOT_EVALUATED"


def test_stage3_manifest_replay_executor_preserves_result(tmp_path):
    spec = load_test_case(CASE)
    run_app_test(
        spec,
        MockStepExecutor(scenario="pass"),
        output_dir=tmp_path / "source",
        run_id="stage3-forbidden",
    )
    report = run_app_test(
        spec,
        ManifestReplayExecutor(tmp_path / "source" / "test_execution_manifest.json"),
        output_dir=tmp_path / "replay",
        run_id="stage3-replay",
    )
    assert report["executor"] == "manifest_replay"
    assert report["overall_result"] == OverallResult.APP_PASS


def test_stage3_manifest_rejects_test_case_hash_mismatch(tmp_path):
    spec = load_test_case(CASE)
    run_app_test(
        spec,
        MockStepExecutor(scenario="pass"),
        output_dir=tmp_path,
        run_id="stage3-hash-mismatch",
    )
    manifest_path = tmp_path / "test_execution_manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["test_case_sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    try:
        load_execution_manifest(manifest_path, spec)
    except ManifestIntakeError as exc:
        assert "test_case_sha256" in str(exc)
    else:
        raise AssertionError("manifest with mismatched test_case_sha256 should fail")


def test_stage3_manifest_rejects_conformant_step_without_post_frame(tmp_path):
    spec = load_test_case(CASE)
    run_app_test(
        spec,
        MockStepExecutor(scenario="pass"),
        output_dir=tmp_path,
        run_id="stage3-missing-post-frame",
    )
    manifest_path = tmp_path / "test_execution_manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["steps"][0]["post_frames"] = []
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    try:
        load_execution_manifest(manifest_path, spec)
    except ManifestIntakeError as exc:
        assert "post observation frame" in str(exc)
    else:
        raise AssertionError("conformant step without post frame should fail")


@pytest.mark.parametrize("scenario", ["execution_fail", "env_blocked"])
def test_manifest_accepts_strict_early_termination_prefix(tmp_path, scenario):
    spec = load_test_case(CASE)
    report = run_app_test(
        spec,
        MockStepExecutor(scenario=scenario),
        output_dir=tmp_path,
        run_id=f"early-prefix-{scenario}",
    )

    manifest = load_execution_manifest(
        tmp_path / "test_execution_manifest.json",
        spec,
        report["contract_sha256"],
    )

    actual_ids = [step.step_id for step in manifest.steps]
    expected_ids = [step.step_id for step in spec.steps]
    assert actual_ids == expected_ids[: len(actual_ids)]
    assert manifest.steps[-1].conformance_status != "CONFORMANT"


def test_manifest_rejects_truncated_all_conformant_prefix(tmp_path):
    spec = load_test_case(CASE)
    report = run_app_test(
        spec,
        MockStepExecutor(scenario="pass"),
        output_dir=tmp_path,
        run_id="truncated-conformant-prefix",
    )
    manifest_path = tmp_path / "test_execution_manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["steps"] = payload["steps"][:-1]
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ManifestIntakeError, match="terminal non-conformant step"):
        load_execution_manifest(manifest_path, spec, report["contract_sha256"])


def test_manifest_rejects_empty_step_sequence(tmp_path):
    spec = load_test_case(CASE)
    report = run_app_test(
        spec,
        MockStepExecutor(scenario="pass"),
        output_dir=tmp_path,
        run_id="empty-manifest-steps",
    )
    manifest_path = tmp_path / "test_execution_manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["steps"] = []
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ManifestIntakeError, match="steps must be a non-empty list"):
        load_execution_manifest(manifest_path, spec, report["contract_sha256"])


def test_stage4_mobiagent_preflight_writes_step_bound_payload(tmp_path):
    spec = load_test_case(CASE)
    result = prepare_mobiagent_preflight(
        spec,
        tmp_path,
        run_id="stage4-preflight",
        device="Harmony",
        device_serial="TEST_SERIAL",
        runner_root=ROOT,
    )
    payload = json.loads(result.payload_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == MOBIAGENT_STEP_PAYLOAD_SCHEMA_VERSION
    assert payload["run_id"] == "stage4-preflight"
    assert payload["test_case_sha256"] == spec.sha256
    assert payload["runner_constraints"]["one_step_per_call"] is True
    assert payload["runner_constraints"]["app_result_not_decided_by_runner"] is True
    assert [step["step_id"] for step in payload["steps"]] == [
        step.step_id for step in spec.steps
    ]
    assert payload["steps"][1]["value"] == "hello test 123"
    assert "Do not skip" in payload["steps"][0]["runner_prompt"]


def test_stage4_mobiagent_preflight_manifest_is_accepted(tmp_path):
    spec = load_test_case(CASE)
    result = prepare_mobiagent_preflight(
        spec,
        tmp_path,
        run_id="stage4-preflight-manifest",
    )
    manifest = load_execution_manifest(result.manifest_path, spec)
    assert manifest.executor == "mobiagent_preflight"
    assert manifest.steps[0].dispatch_status == "ACTION_NOT_DISPATCHED"
    assert manifest.steps[0].conformance_status == "UNKNOWN"
    assert manifest.steps[0].effect_status == "NOT_EVALUATED"
    assert manifest.final_state.evidence_sufficient is False


def test_stage4_mobiagent_device_execution_is_explicitly_not_implemented():
    try:
        reject_unimplemented_device_execution()
    except AppTestCaseError as exc:
        assert "requires --execute-runner" in str(exc)
    else:
        raise AssertionError("device execution must require explicit runner execution")


def test_stage_a_minimal_user_view_case_needs_no_target_or_expected_after():
    spec = load_test_case(MINIMAL_USER_CASE).with_runtime_context(run_id="minimal-001")
    assert [step.instruction for step in spec.steps] == [
        "打开发布入口",
        "输入测试内容",
        "点击发布",
    ]
    assert [step.action_type for step in spec.steps] == ["CLICK", "INPUT", "CLICK"]
    assert all(not step.target for step in spec.steps)
    assert spec.steps[1].value_ref == "post_content"
    assert spec.expected_results[0].after_step is None
    assert spec.expected_results[0].expected_value_ref == "post_content"


def test_stage_a_minimal_user_view_case_runs_through_mock_executor():
    spec = load_test_case(MINIMAL_USER_CASE)
    report = run_app_test(
        spec,
        MockStepExecutor(scenario="pass"),
        run_id="minimal-mock",
    )
    assert report["overall_result"] == OverallResult.APP_PASS
    assert report["contract"]["test_case_sha256"] == spec.with_runtime_context(
        run_id="minimal-mock"
    ).sha256
    steps = report["contract"]["execution_contract"]["steps"]
    assert all(step["target"] == {} for step in steps)
    assert all(step["target_is_legacy_hint"] is False for step in steps)


def test_stage_a_goal_step_generates_runtime_content_for_final_assertion():
    payload = _case_payload()
    payload["test_case_id"] = "goal-runtime-generated-001"
    payload["test_data"] = {}
    payload["steps"] = ["完成一次文字发帖"]
    payload["expected_results"] = ["可以在个人主页看到刚才发布内容"]
    spec = AppTestCaseSpec.from_json(payload)
    runtime = spec.with_runtime_context(run_id="goal-free")
    assert runtime.steps[0].action_type == "GUI_TASK"
    assert runtime.steps[0].step_mode == "GOAL"
    assert runtime.steps[0].value_ref == "__generated_post_content"
    assert runtime.expected_results[0].expected_value_ref == "__generated_post_content"
    assert runtime.test_data["__generated_post_content"] == "app_test_goal-free_post_content"
    contract = compile_app_test_contract(runtime)
    step_contract = contract.execution_contract["steps"][0]
    assert step_contract["step_mode"] == "GOAL"
    assert step_contract["expected_value"] == "app_test_goal-free_post_content"
    assert contract.execution_contract["runtime_generated_data"] == {
        "__generated_post_content": "app_test_goal-free_post_content"
    }


class _FakeMobiAgentDevice:
    def __init__(self):
        self.state = "feed"
        self.input_text = ""
        self.started_package = None
        self.clicks: list[tuple[int, int]] = []
        self.swipes: list[tuple[int, int, int, int]] = []
        self.long_presses: list[tuple[int, int]] = []
        self.keyevents: list[object] = []

    def app_start(self, package):
        self.started_package = package

    def start_app(self, app):
        self.started_package = app

    def screenshot(self, path):
        Image.new("RGB", (1080, 2444), "white").save(path, format="JPEG")

    def dump_hierarchy(self):
        if self.state == "feed":
            return """<hierarchy><node text="Feed" bounds="[0,0][100,80]" /><node text="Post" clickable="true" bounds="[400,2100][680,2400]" /></hierarchy>"""
        if self.state == "editor":
            return """<hierarchy><node text="正文" class="EditText" bounds="[80,300][1000,700]" /><node text="Post" clickable="true" bounds="[820,60][1060,180]" /></hierarchy>"""
        return f"""<hierarchy><node text="Feed" bounds="[0,0][100,80]" /><node text="{self.input_text}" bounds="[80,300][1000,700]" /></hierarchy>"""

    def click(self, x, y):
        self.clicks.append((x, y))
        if self.state == "feed":
            self.state = "editor"
        elif self.state == "editor" and y < 250:
            self.state = "posted"

    def input(self, text):
        self.input_text = text

    def swipe_with_coords(self, start_x, start_y, end_x, end_y):
        self.swipes.append((start_x, start_y, end_x, end_y))

    def long_press(self, x, y):
        self.long_presses.append((x, y))

    def keyevent(self, key):
        self.keyevents.append(key)


class _FakeJsonMobiAgentDevice(_FakeMobiAgentDevice):
    def dump_hierarchy(self):
        submit = None
        if self.state == "posted":
            text = self.input_text
            extra = {
                "attributes": {
                    "type": "Text",
                    "originalText": text,
                    "text": text,
                    "bounds": "[80,300][1000,700]",
                    "visible": "true",
                },
                "children": [],
            }
        elif self.state == "editor":
            extra = {
                "attributes": {
                    "type": "EditText",
                    "originalText": "正文",
                    "text": "正文",
                    "bounds": "[80,300][1000,700]",
                    "visible": "true",
                },
                "children": [],
            }
            submit = {
                "attributes": {
                    "type": "Button",
                    "originalText": "Post",
                    "text": "Post",
                    "bounds": "[820,60][1060,180]",
                    "visible": "true",
                    "clickable": "true",
                },
                "children": [],
            }
        else:
            extra = {
                "attributes": {
                    "type": "Image",
                    "id": "publish_icon_without_text",
                    "bounds": "[480,2200][600,2400]",
                    "visible": "true",
                },
                "children": [],
            }
            submit = None
        children = [
            {
                "attributes": {
                    "type": "Text",
                    "originalText": "Feed",
                    "text": "Feed",
                    "bounds": "[0,0][120,80]",
                    "visible": "true",
                },
                "children": [],
            },
            extra,
        ]
        if submit is not None:
            children.append(submit)
        return {
            "attributes": {
                "type": "root",
                "bounds": "[0,0][1080,2444]",
                "visible": "true",
            },
            "children": children,
        }


class _NoProgressMobiAgentDevice(_FakeMobiAgentDevice):
    def dump_hierarchy(self):
        return """<hierarchy><node text="Feed" bounds="[0,0][100,80]" /><node text="Post" clickable="true" bounds="[400,2100][680,2400]" /></hierarchy>"""

    def click(self, x, y):
        self.clicks.append((x, y))


class _PostActionEnvBlockedDevice(_FakeMobiAgentDevice):
    def click(self, x, y):
        self.clicks.append((x, y))
        self.state = "login"

    def dump_hierarchy(self):
        if self.state == "login":
            return """<hierarchy><node text="Please log in" bounds="[80,300][900,440]" /></hierarchy>"""
        return super().dump_hierarchy()


class _AsyncEditorMobiAgentDevice(_FakeMobiAgentDevice):
    def __init__(self):
        super().__init__()
        self.transitioning = False
        self.transition_dumps = 0

    def click(self, x, y):
        self.clicks.append((x, y))
        if self.state == "feed":
            self.transitioning = True

    def dump_hierarchy(self):
        if self.transitioning:
            self.transition_dumps += 1
            if self.transition_dumps == 1:
                return """<hierarchy><node text="Feed" bounds="[0,0][100,80]" /><node text="Post" clickable="true" bounds="[400,2100][680,2400]" /></hierarchy>"""
            self.state = "editor"
            self.transitioning = False
        return super().dump_hierarchy()


class _ScrollableMessageListDevice(_FakeMobiAgentDevice):
    def __init__(self):
        super().__init__()
        self.scrolled = False

    def dump_hierarchy(self):
        if self.scrolled:
            return (
                '<hierarchy><node text="Feed" bounds="[0,0][100,80]" />'
                '<node text="消息" bounds="[0,80][200,160]" />'
                '<node text="张三" clickable="true" bounds="[40,600][900,760]" />'
                "</hierarchy>"
            )
        return (
            '<hierarchy><node text="Feed" bounds="[0,0][100,80]" />'
            '<node text="消息" bounds="[0,80][200,160]" /></hierarchy>'
        )

    def swipe_with_coords(self, start_x, start_y, end_x, end_y):
        super().swipe_with_coords(start_x, start_y, end_x, end_y)
        self.scrolled = True


class _PublishCompletionOnlyDevice(_FakeMobiAgentDevice):
    def dump_hierarchy(self):
        if self.state == "posted":
            return """<hierarchy><node text="发布完成" bounds="[40,100][500,180]" /><node text="我" clickable="true" bounds="[900,2200][1040,2380]" /></hierarchy>"""
        return super().dump_hierarchy()


def _runner_bbox(bounds: tuple[int, int, int, int]) -> list[int]:
    return [int(item * 0.5) for item in bounds]


def _fake_step_decider(intent, test_case, current_frame):
    del test_case, current_frame
    if intent.action_family == "INPUT":
        return {
            "reasoning": "fake step-bound input model response",
            "action": "click_input",
            "parameters": {
                "target_element": "editor body",
                "bbox": _runner_bbox((80, 300, 1000, 700)),
            },
        }
    if intent.step_id == "open_post_editor":
        bounds = (400, 2100, 680, 2400)
        target = "post creation entry"
    else:
        bounds = (820, 60, 1060, 180)
        target = "submit post button"
    return {
        "reasoning": "fake step-bound click model response",
        "action": "click",
        "parameters": {
            "target_element": target,
            "bbox": _runner_bbox(bounds),
        },
    }


def _goal_micro_action_decider():
    calls = {"count": 0}

    def decide(intent, test_case, current_frame):
        del test_case
        calls["count"] += 1
        texts = set(current_frame.get("visible_texts", []))
        if "Feed" in texts and "Post" in texts and calls["count"] == 1:
            action = {
                "action": "click",
                "parameters": {
                    "target_element": "post creation entry",
                    "bbox": _runner_bbox((400, 2100, 680, 2400)),
                },
            }
        elif calls["count"] == 2:
            action = {
                "action": "click_input",
                "parameters": {
                    "target_element": "editor body",
                    "bbox": _runner_bbox((80, 300, 1000, 700)),
                },
            }
        elif calls["count"] == 3:
            action = {
                "action": "click",
                "parameters": {
                    "target_element": "submit post button",
                    "bbox": _runner_bbox((820, 60, 1060, 180)),
                },
            }
        else:
            action = {"action": "done", "parameters": {"status": "success"}}
        assert intent.step_mode == "GOAL"
        return {
            "reasoning": f"fake goal planner decided micro-action {calls['count']} from latest page",
            **action,
        }

    decide.calls = calls
    return decide


def _goal_publish_completion_decider():
    calls = {"count": 0}

    def decide(intent, test_case, current_frame):
        del test_case, current_frame
        calls["count"] += 1
        assert intent.step_mode == "GOAL"
        if calls["count"] == 1:
            return {
                "reasoning": "open publisher from feed",
                "action": "click",
                "parameters": {
                    "target_element": "post creation entry",
                    "bbox": _runner_bbox((400, 2100, 680, 2400)),
                },
            }
        if calls["count"] == 2:
            return {
                "reasoning": "fill generated post text",
                "action": "click_input",
                "parameters": {
                    "target_element": "editor body",
                    "bbox": _runner_bbox((80, 300, 1000, 700)),
                },
            }
        if calls["count"] == 3:
            return {
                "reasoning": "publish post",
                "action": "click",
                "parameters": {
                    "target_element": "submit post button",
                    "bbox": _runner_bbox((820, 60, 1060, 180)),
                },
            }
        return {
            "reasoning": "stage result already visible",
            "action": "done",
            "parameters": {"status": "success"},
        }

    decide.calls = calls
    return decide


def _legacy_goal_micro_action_decider(intent, test_case, current_frame):
    del test_case, current_frame
    assert intent.step_mode == "GOAL"
    return {
        "reasoning": "fake goal planner expanded the user step into internal micro-actions",
        "action": "gui_task",
        "parameters": {
            "micro_actions": [
                {
                    "action": "click",
                    "parameters": {
                        "target_element": "post creation entry",
                        "bbox": _runner_bbox((400, 2100, 680, 2400)),
                    },
                },
                {
                    "action": "click_input",
                    "parameters": {
                        "target_element": "editor body",
                        "bbox": _runner_bbox((80, 300, 1000, 700)),
                    },
                },
                {
                    "action": "click",
                    "parameters": {
                        "target_element": "submit post button",
                        "bbox": _runner_bbox((820, 60, 1060, 180)),
                    },
                },
                {"action": "done", "parameters": {"status": "success"}},
            ],
        },
    }


def _done_step_decider(intent, test_case, current_frame):
    del intent, test_case, current_frame
    return {
        "reasoning": "fake model stopped early",
        "action": "done",
        "parameters": {"status": "success"},
    }


def test_stage6_mobiagent_step_executor_emits_real_style_trace_artifacts(tmp_path):
    payload = _case_payload()
    payload["test_case_id"] = "real-step-adapter-001"
    payload["app_under_test"]["package"] = "com.example.demoforum"
    spec = AppTestCaseSpec.from_json(payload).with_runtime_context(run_id="real-step")
    executor = MobiAgentStepExecutor(
        output_dir=tmp_path,
        device_instance=_FakeMobiAgentDevice(),
        step_decider=_fake_step_decider,
    )
    record = executor.execute(spec)
    assert record.executor == "mobiagent_real_step"
    assert [item.status for item in record.step_results] == ["STEP_COMPLETED"] * 3
    assert record.raw_trace_dir is not None
    trace_dir = Path(record.raw_trace_dir)
    assert (trace_dir / "0.jpg").is_file()
    assert (trace_dir / "0.xml").is_file()
    assert (trace_dir / "actions.json").is_file()
    assert "hello test 123" in record.final_state.visible_texts
    assert record.metadata["frames"][-1]["visible_texts"] == ["Feed", "hello test 123"]
    assert record.metadata["primary_locator"] == "runner.mobiagent.decider_grounder_step_bound"
    assert record.step_results[0].evidence["target_source"] == "injected_step_decider"
    assert record.step_results[0].evidence["step_execution_intent"]["step_id"] == "open_post_editor"


def test_stage6_mobiagent_step_executor_parses_harmony_json_hierarchy_and_step_decider(tmp_path):
    payload = _case_payload()
    payload["test_case_id"] = "real-step-json-adapter-001"
    payload["app_under_test"]["package"] = "com.example.demoforum"
    spec = AppTestCaseSpec.from_json(payload).with_runtime_context(run_id="real-step-json")

    executor = MobiAgentStepExecutor(
        output_dir=tmp_path,
        device_instance=_FakeJsonMobiAgentDevice(),
        step_decider=_fake_step_decider,
    )
    record = executor.execute(spec)
    assert [item.status for item in record.step_results] == ["STEP_COMPLETED"] * 3
    trace_dir = Path(record.raw_trace_dir or "")
    assert (trace_dir / "0.json").is_file()
    assert "Feed" in record.metadata["frames"][0]["visible_texts"]
    assert record.step_results[0].evidence["target_source"] == "injected_step_decider"
    assert "hello test 123" in record.final_state.visible_texts


def test_stage6_mobiagent_step_executor_does_not_prefer_declared_coordinates(tmp_path):
    payload = _case_payload()
    payload["test_case_id"] = "real-step-coordinates-001"
    payload["app_under_test"]["package"] = "com.example.demoforum"
    payload["steps"][0]["target"] = {
        "label": "bottom composer",
        "coordinates": [1, 1],
    }
    payload["steps"][1]["target"] = {
        "label": "editor body",
        "coordinates": [1, 1],
    }
    payload["steps"][2]["target"] = {
        "label": "submit",
        "coordinates": [1, 1],
    }
    spec = AppTestCaseSpec.from_json(payload).with_runtime_context(run_id="real-step-coords")
    executor = MobiAgentStepExecutor(
        output_dir=tmp_path,
        device_instance=_FakeJsonMobiAgentDevice(),
        step_decider=_fake_step_decider,
    )
    record = executor.execute(spec)
    assert [item.status for item in record.step_results] == ["STEP_COMPLETED"] * 3
    assert record.step_results[0].evidence["target_source"] == "injected_step_decider"
    assert record.step_results[1].evidence["target_source"] == "injected_step_decider"
    assert record.step_results[2].evidence["click_point"] != [1, 1]
    assert "hello test 123" in record.final_state.visible_texts


def test_mobiagent_decider_receives_persistent_runner_history(tmp_path, monkeypatch):
    payload = _case_payload()
    spec = AppTestCaseSpec.from_json(payload).with_runtime_context(run_id="history-001")
    seen_history: list[list[str]] = []

    def decide(intent, test_case, current_frame, *, wants_text_input, history):
        del test_case, current_frame, wants_text_input
        seen_history.append(list(history))
        if intent.action_family == "INPUT":
            return {
                "reasoning": "input using the current step value",
                "action": "click_input",
                "parameters": {
                    "target_element": "editor body",
                    "bbox": _runner_bbox((80, 300, 1000, 700)),
                },
            }
        bounds = (400, 2100, 680, 2400) if intent.step_id == "open_post_editor" else (820, 60, 1060, 180)
        return {
            "reasoning": "continue the current declared step",
            "action": "click",
            "parameters": {
                "target_element": intent.step_id,
                "bbox": _runner_bbox(bounds),
            },
        }

    executor = MobiAgentStepExecutor(
        output_dir=tmp_path,
        device_instance=_FakeMobiAgentDevice(),
    )
    monkeypatch.setattr(executor, "_decide_with_mobiagent", decide)
    record = executor.execute(spec)
    assert [item.status for item in record.step_results] == ["STEP_COMPLETED"] * 3
    assert seen_history[0] == []
    assert len(seen_history[1]) >= 1
    assert len(seen_history[2]) >= 2
    assert record.metadata["decider_history_count"] >= 3


def test_stage7_mobiagent_goal_step_allows_internal_micro_actions_and_generated_data(tmp_path):
    payload = _case_payload()
    payload["test_case_id"] = "real-goal-step-001"
    payload["app_under_test"]["package"] = "com.example.demoforum"
    payload["test_data"] = {}
    payload["steps"] = [
        {
            "step_id": "create_text_post",
            "instruction": "完成一次文字发帖",
            "action_type": "GUI_TASK",
            "step_mode": "GOAL",
        }
    ]
    payload["expected_results"] = ["可以在个人主页看到刚才发布内容"]
    spec = AppTestCaseSpec.from_json(payload).with_runtime_context(run_id="goal-micro")
    decider = _goal_micro_action_decider()
    executor = MobiAgentStepExecutor(
        output_dir=tmp_path,
        device_instance=_FakeMobiAgentDevice(),
        step_decider=decider,
    )
    report = run_app_test(
        spec,
        executor,
        output_dir=tmp_path / "bundle",
        run_id="goal-micro",
    )
    result = report["step_results"][0]
    assert result["status"] == "STEP_COMPLETED"
    assert result["action_type"] == "GUI_TASK"
    assert result["resolved_value"] == "app_test_goal-micro_post_content"
    assert result["evidence"]["micro_action_count"] == 3
    assert decider.calls["count"] == 3
    assert len(result["evidence"]["micro_actions"]) == 3
    assert len(result["evidence"]["micro_action_observations"]) == 3
    assert len(result["evidence"]["micro_gates"]) == 3
    for index, micro in enumerate(result["evidence"]["micro_action_observations"], 1):
        assert micro["micro_action_index"] == index
        assert micro["action_evidence"]["action_index"] == micro["action_id"]
        assert micro["post_frame"] in result["evidence"]["goal_observation_frame_ids"]
        assert len(micro["post_frame_ids"]) == 3
        assert micro["post_frame"] == micro["post_frame_ids"][-1]
        assert micro["post_observation_burst"]["relative_to_action_ms"] == [0, 500, 1000]
        if micro["action_type"] == "click_input":
            assert micro["target_evidence"] == "CONFORMANT"
            assert micro["action_conformance"] == "CONFORMANT"
            assert micro["progress_status"] == "INPUT_DISPATCH_CONFIRMED"
        else:
            assert micro["target_evidence"] == "CONFORMANT"
            assert micro["action_conformance"] == "CONFORMANT"
        assert micro["micro_gate_decision"] == "CONTINUE"
        assert micro["micro_gate"]["attempt"] == index
    assert result["evidence"]["goal_completed"] is True
    assert result["evidence"]["goal_state"]["status"] == "COMPLETED"
    assert result["evidence"]["goal_state"]["micro_action_count"] == 3
    assert result["evidence"]["progress_status"] == "GOAL_RESULT_CONFIRMED"
    assert result["evidence"]["goal_completion_evidence"]["confirmed"] is True
    assert result["evidence"]["generated_runtime_data"] == {
        "__generated_post_content": "app_test_goal-micro_post_content"
    }
    assert report["overall_result"] == OverallResult.APP_PASS
    envelope = load_run_envelope(tmp_path / "bundle" / "run_envelope.json")
    assert envelope["runtime_generated_data"] == {
        "__generated_post_content": "app_test_goal-micro_post_content"
    }
    assert envelope["business_execution"]["runtime_generated_data"] == {
        "__generated_post_content": "app_test_goal-micro_post_content"
    }
    envelope_step = envelope["business_execution"]["steps"][0]
    assert envelope_step["micro_gate_count"] == 3
    assert envelope_step["micro_gates_sha256"]
    assert envelope_step["micro_action_observations_sha256"]
    assert envelope_step["goal_state"]["status"] == "COMPLETED"
    assert envelope_step["goal_state_sha256"]


def test_goal_runner_can_use_original_swipe_action_to_find_contact(tmp_path):
    payload = _case_payload()
    payload["test_case_id"] = "goal-contact-scroll-001"
    payload["steps"] = [
        {
            "step_id": "find_contact",
            "instruction": "在消息列表中下滑查找联系人张三",
            "action_type": "GUI_TASK",
            "step_mode": "GOAL",
            "target": {
                "stage_result_text_candidates": ["张三"],
                "max_micro_actions": 3,
            },
        }
    ]
    payload["expected_results"] = [
        {
            "assertion_id": "contact_visible",
            "type": "TEXT_VISIBLE",
            "expected_value": "张三",
            "surface": "消息联系人列表",
            "after_step": "find_contact",
        }
    ]
    spec = AppTestCaseSpec.from_json(payload).with_runtime_context(run_id="contact-scroll")

    def decider(intent, test_case, current_frame):
        del intent, test_case, current_frame
        return {
            "reasoning": "联系人不在当前消息列表，向上滑动查找",
            "action": "swipe",
            "parameters": {"direction": "UP"},
        }

    device = _ScrollableMessageListDevice()
    report = run_app_test(
        spec,
        MobiAgentStepExecutor(
            output_dir=tmp_path,
            device_instance=device,
            step_decider=decider,
        ),
        run_id="contact-scroll",
    )
    step = report["step_results"][0]
    assert report["overall_result"] == OverallResult.APP_PASS
    assert device.swipes
    assert step["evidence"]["micro_actions"][0]["type"] == "swipe"
    assert step["evidence"]["micro_actions"][0]["direction"] == "up"
    assert step["evidence"]["micro_gates"][0]["progress_status"] == "GOAL_RESULT_CONFIRMED"


class _ActionFreedomDevice(_FakeMobiAgentDevice):
    def __init__(self):
        super().__init__()
        self.freedom_state = "initial"

    def dump_hierarchy(self):
        if self.freedom_state == "home":
            return '<hierarchy><node text="Feed" /><node text="control complete" /></hierarchy>'
        return '<hierarchy><node text="Feed" /><node text="Long press target" clickable="true" bounds="[100,500][500,800]" /></hierarchy>'

    def long_press(self, x, y):
        super().long_press(x, y)
        self.freedom_state = "long_pressed"

    def keyevent(self, key):
        super().keyevent(key)
        if str(key).casefold() == "home":
            self.freedom_state = "home"


def test_goal_runner_preserves_long_press_and_press_home_actions(tmp_path):
    payload = _case_payload()
    payload["test_case_id"] = "goal-action-freedom-001"
    payload["steps"] = [
        {
            "step_id": "recover_and_confirm",
            "instruction": "长按目标后回到主屏幕确认状态",
            "action_type": "GUI_TASK",
            "step_mode": "GOAL",
            "target": {
                "stage_result_text_candidates": ["control complete"],
                "max_micro_actions": 3,
            },
        }
    ]
    payload["expected_results"] = [
        {
            "assertion_id": "control_result_visible",
                "type": "TEXT_VISIBLE",
                "expected_value": "control complete",
                "surface": "control complete",
                "after_step": "recover_and_confirm",
        }
    ]
    spec = AppTestCaseSpec.from_json(payload).with_runtime_context(run_id="action-freedom")
    calls = {"count": 0}

    def decider(intent, test_case, current_frame):
        del intent, test_case, current_frame
        calls["count"] += 1
        if calls["count"] == 1:
            return {
                "action": "long_press",
                "parameters": {"coords": [300, 650]},
            }
        return {"action": "press_home", "parameters": {}}

    device = _ActionFreedomDevice()
    report = run_app_test(
        spec,
        MobiAgentStepExecutor(
            output_dir=tmp_path,
            device_instance=device,
            device="Android",
            step_decider=decider,
        ),
        run_id="action-freedom",
    )
    micro_actions = report["step_results"][0]["evidence"]["micro_actions"]
    assert report["overall_result"] == OverallResult.APP_PASS
    assert [item["type"] for item in micro_actions] == ["long_press", "press_home"]
    assert device.long_presses
    assert "home" in [str(item).casefold() for item in device.keyevents]


def test_runner_abort_is_preserved_as_inconclusive_control_evidence(tmp_path):
    payload = _case_payload()
    payload["test_case_id"] = "goal-abort-control-001"
    payload["steps"] = [
        {
            "step_id": "abort_current_step",
            "instruction": "如果无法确认联系人则终止当前步骤",
            "action_type": "GUI_TASK",
            "step_mode": "GOAL",
            "target": {"stage_result_text_candidates": ["联系人已找到"]},
        }
    ]
    payload["expected_results"] = [
        {"assertion_id": "contact_visible", "type": "TEXT_VISIBLE", "expected_value": "联系人已找到"}
    ]
    spec = AppTestCaseSpec.from_json(payload).with_runtime_context(run_id="abort-control")
    report = run_app_test(
        spec,
        MobiAgentStepExecutor(
            output_dir=tmp_path,
            device_instance=_FakeMobiAgentDevice(),
            step_decider=lambda intent, test_case, current_frame: {
                "action": "abort",
                "parameters": {"reason": "contact is not safely identifiable"},
            },
        ),
        run_id="abort-control",
    )
    step = report["step_results"][0]
    assert report["overall_result"] == OverallResult.INCONCLUSIVE
    assert step["status"] == "INCONCLUSIVE"
    assert step["evidence"]["runner_control_events"][0]["runner_control_action"] == "abort"


def test_stage_c_step_gate_inconclusive_when_intermediate_progress_unknown(tmp_path):
    payload = _case_payload()
    payload["test_case_id"] = "step-gate-no-progress-001"
    payload["app_under_test"]["package"] = "com.example.demoforum"
    spec = AppTestCaseSpec.from_json(payload).with_runtime_context(run_id="gate-no-progress")
    executor = MobiAgentStepExecutor(
        output_dir=tmp_path,
        device_instance=_NoProgressMobiAgentDevice(),
        step_decider=_fake_step_decider,
    )
    record = executor.execute(spec)
    assert record.step_results[0].status == "INCONCLUSIVE"
    assert record.step_results[0].evidence["gate_decision"] == "INCONCLUSIVE"
    assert record.step_results[0].evidence["progress_status"] == "UNKNOWN"
    report = run_app_test(
        spec,
        ScriptedStepExecutor(record),
        run_id="gate-no-progress-report",
    )
    assert report["overall_result"] == OverallResult.INCONCLUSIVE
    assert report["app_behavior_result"]["status"] == "NOT_EVALUATED"


def test_stage_c_step_gate_env_blocked_after_action(tmp_path):
    payload = _case_payload()
    payload["test_case_id"] = "step-gate-env-blocked-001"
    payload["app_under_test"]["package"] = "com.example.demoforum"
    spec = AppTestCaseSpec.from_json(payload).with_runtime_context(run_id="gate-env")
    executor = MobiAgentStepExecutor(
        output_dir=tmp_path,
        device_instance=_PostActionEnvBlockedDevice(),
        step_decider=_fake_step_decider,
    )
    record = executor.execute(spec)
    assert record.step_results[0].status == "ENV_BLOCKED"
    assert record.step_results[0].evidence["gate_decision"] == "ENV_BLOCKED"
    assert record.step_results[0].blocker == "log in"
    report = run_app_test(spec, ScriptedStepExecutor(record), run_id="gate-env-report")
    assert report["overall_result"] == OverallResult.ENV_BLOCKED


def test_stage_c_step_gate_done_before_action_is_execution_failure(tmp_path):
    payload = _case_payload()
    payload["test_case_id"] = "step-gate-done-before-action-001"
    payload["app_under_test"]["package"] = "com.example.demoforum"
    spec = AppTestCaseSpec.from_json(payload).with_runtime_context(run_id="gate-done")
    executor = MobiAgentStepExecutor(
        output_dir=tmp_path,
        device_instance=_FakeMobiAgentDevice(),
        step_decider=_done_step_decider,
    )
    record = executor.execute(spec)
    assert record.step_results[0].status == "STEP_FAILED"
    assert "done before dispatching required action" in str(record.step_results[0].error)


def test_stage_c_step_gate_missing_target_evidence_is_unknown_inconclusive():
    spec = load_test_case(CASE)
    gate = evaluate_step_gate(
        test_case=spec,
        step=spec.steps[0],
        action_record={"type": "click", "action_index": 1},
        attempt=1,
        pre_frame=_frame(0, ("Feed", "Post"), 0),
        post_frames=(_frame(1, ("Feed", "Post"), 500),),
        next_step=spec.steps[1],
    )
    assert gate.target_evidence == "UNKNOWN"
    assert gate.action_conformance == "UNKNOWN"
    assert gate.gate_decision == "INCONCLUSIVE"


def test_stage_c_step_gate_missing_target_evidence_is_not_recovered_by_generic_page_change():
    spec = load_test_case(CASE)
    gate = evaluate_step_gate(
        test_case=spec,
        step=spec.steps[0],
        action_record={"type": "click", "action_index": 1},
        attempt=1,
        pre_frame=_frame(0, ("Feed", "Post"), 0),
        post_frames=(_frame(1, ("Editor",), 500),),
        next_step=spec.steps[1],
    )
    assert gate.target_evidence == "UNKNOWN"
    assert gate.progress_status == "PAGE_CHANGED"
    assert gate.gate_decision == "INCONCLUSIVE"


def test_stage_c_target_match_without_dispatch_geometry_cannot_continue_on_page_change():
    spec = load_test_case(CASE)
    gate = evaluate_step_gate(
        test_case=spec,
        step=spec.steps[0],
        action_record={
            "type": "click",
            "action_index": 1,
            "target_match": True,
        },
        attempt=1,
        pre_frame=_frame(0, ("Feed", "Post"), 0),
        post_frames=(_frame(1, ("Unrelated page",), 500),),
        next_step=spec.steps[1],
    )

    assert gate.target_evidence == "UNKNOWN"
    assert gate.action_conformance == "UNKNOWN"
    assert gate.progress_status == "PAGE_CHANGED"
    assert gate.gate_decision == "INCONCLUSIVE"


def test_stage_c_step_gate_wrong_click_target_is_non_conformant():
    spec = load_test_case(CASE)
    gate = evaluate_step_gate(
        test_case=spec,
        step=spec.steps[0],
        action_record={
            "type": "click",
            "action_index": 1,
            "click_point": [500, 500],
            "runtime_bounds": [0, 0, 100, 100],
        },
        attempt=1,
        pre_frame=_frame(0, ("Feed", "Post"), 0),
        post_frames=(_frame(1, ("Editor",), 500),),
        next_step=spec.steps[1],
    )
    assert gate.target_evidence == "NON_CONFORMANT"
    assert gate.action_conformance == "NON_CONFORMANT"
    assert gate.gate_decision == "TEST_EXECUTION_FAIL"


def test_stage7_target_conformance_prefers_xml_wrong_target_over_model_bounds():
    spec = load_test_case(CASE)
    gate = evaluate_step_gate(
        test_case=spec,
        step=spec.steps[0],
        action_record={
            "type": "click",
            "action_index": 1,
            "click_point": [540, 2250],
            "bounds": [400, 2100, 680, 2400],
            "xml_hit_test_result": {"rejection_reason": "wrong_target"},
        },
        attempt=1,
        pre_frame=_frame(0, ("Feed", "Post"), 0),
        post_frames=(_frame(1, ("Editor",), 500),),
        next_step=spec.steps[1],
    )
    assert gate.target_evidence == "NON_CONFORMANT"
    assert gate.gate_decision == "TEST_EXECUTION_FAIL"


def test_stage7_weak_xml_hit_with_direct_node_is_not_conformant():
    """A raw hit on an unrelated card must not satisfy a semantic tab target."""
    spec = load_test_case(CASE)
    gate = evaluate_step_gate(
        test_case=spec,
        step=spec.steps[0],
        action_record={
            "type": "click",
            "action_index": 1,
            "click_point": [751, 1902],
            "xml_hit_test_result": {
                "candidate_center_in_model_bounds": False,
                "direct_hits": [
                    {
                        "tag": "__Common__",
                        "text": "曾经北京的西城第一，考上北大元培后",
                        "bounds": [548, 1254, 1065, 2213],
                    }
                ],
                "intersection_ratio": 0.0615,
                "rejection_reason": "weak_geometry_without_semantics",
                "semantic_score": 0,
                "snapped": False,
            },
        },
        attempt=1,
        pre_frame=_frame(0, ("Feed",), 0),
        post_frames=(_frame(1, ("Feed", "Article"), 500),),
        next_step=spec.steps[1],
    )
    assert gate.target_evidence == "NON_CONFORMANT"
    assert gate.action_conformance == "NON_CONFORMANT"
    assert gate.gate_decision == "TEST_EXECUTION_FAIL"


def test_stage7_malformed_grounder_response_enters_target_retry_path(tmp_path, monkeypatch):
    image_path = tmp_path / "frame.jpg"
    Image.new("RGB", (1080, 2444), "white").save(image_path)

    class _MalformedGrounderRunner:
        factor = 0.5

        @staticmethod
        def load_prompt(_name):
            return "{reasoning} {description}"

        @staticmethod
        def handle_click_action(*_args, **_kwargs):
            raise ValueError("Grounder response must contain 'coordinates' or 'bbox' field")

    monkeypatch.setattr(
        mobiagent_executor_module,
        "_import_original_mobiagent",
        lambda: _MalformedGrounderRunner,
    )
    executor = MobiAgentStepExecutor(output_dir=tmp_path)
    with pytest.raises(_TargetNotFound, match="no usable target geometry"):
        executor._dispatch_runner_decision(
            _FakeMobiAgentDevice(),
            {"action": "click", "parameters": {"target_element": "消息"}},
            action_index=1,
            raw_trace_dir=tmp_path,
            current_frame={
                "screenshot_abs": str(image_path),
                "screenshot": image_path.name,
            },
            history=[],
        )


def test_stage7_alignment_rejection_uses_remaining_pre_dispatch_retry_budget():
    spec = load_test_case(CASE)
    error = (
        "runner grounder returned no usable target geometry: "
        "target alignment rejected before dispatch: weak_geometry_without_semantics"
    )
    gate = evaluate_dispatch_failure_gate(
        test_case=spec,
        step=spec.steps[0],
        attempt=1,
        pre_frame=_frame(0, ("Feed",), 0),
        error=error,
        max_retries=1,
    )
    assert gate.gate_decision == "RETRY"
    assert gate.reason == "target was not located before dispatch; retrying within budget"

    exhausted = evaluate_dispatch_failure_gate(
        test_case=spec,
        step=spec.steps[0],
        attempt=2,
        pre_frame=_frame(0, ("Feed",), 0),
        error=error,
        max_retries=1,
    )
    assert exhausted.gate_decision == "TEST_EXECUTION_FAIL"


def test_stage7_external_window_hit_is_retryable_overlay_block():
    spec = load_test_case(CASE)
    gate = evaluate_step_gate(
        test_case=spec,
        step=spec.steps[0],
        action_record={
            "type": "click",
            "action_index": 1,
            "click_point": [540, 500],
            "xml_hit_test_result": {
                "alignment_basis": "direct_supported_hit",
                "direct_hits": [
                    {
                        "tag": "Text",
                        "text": "Assistant suggestion",
                        "semantic_text": "",
                        "window_bundle_name": "com.huawei.hmos.vassistant",
                        "window_page_path": "pages/float/HalfPage60",
                        "bounds": [0, 0, 1080, 1363],
                    }
                ],
            },
        },
        attempt=1,
        pre_frame=_frame(0, ("Feed",), 0),
        post_frames=(_frame(1, ("Feed", "Assistant suggestion"), 500),),
        next_step=spec.steps[1],
    )
    assert gate.target_evidence == "OVERLAY_BLOCKED"
    assert gate.action_conformance == "NON_CONFORMANT"
    assert gate.gate_decision == "RETRY"


def test_stage7_harmony_json_hierarchy_is_passed_to_runner_as_mapping(tmp_path):
    hierarchy = {
        "attributes": {"bounds": "[0,0][1080,2444]"},
        "children": [
            {
                "attributes": {
                    "bundleName": "com.example.app",
                    "pagePath": "pages/Main",
                    "type": "root",
                    "bounds": "[0,122][1080,2444]",
                },
                "children": [
                    {
                        "attributes": {
                            "type": "Button",
                            "text": "消息",
                            "clickable": "true",
                            "enabled": "true",
                            "bounds": "[706,2251][806,2326]",
                        }
                    }
                ],
            }
        ],
    }
    (tmp_path / "4.json").write_text(json.dumps(hierarchy), encoding="utf-8")
    executor = MobiAgentStepExecutor(output_dir=tmp_path)
    parsed = executor._frame_hierarchy_text({"hierarchy": "4.json"}, tmp_path)
    assert isinstance(parsed, dict)
    assert parsed["children"][0]["attributes"]["bundleName"] == "com.example.app"


def test_stage7_runner_alignment_uses_descendant_text_for_clickable_container():
    from app_test_agent.mobiagent_executor import _import_original_mobiagent

    runner = _import_original_mobiagent()
    hierarchy = {
        "attributes": {},
        "children": [
            {
                "attributes": {
                    "type": "Row",
                    "clickable": "true",
                    "enabled": "true",
                    "bounds": "[669,2220][843,2358]",
                },
                "children": [
                    {
                        "attributes": {
                            "type": "Stack",
                            "clickable": "true",
                            "enabled": "true",
                            "bounds": "[706,2251][806,2326]",
                        },
                        "children": [
                            {
                                "attributes": {
                                    "type": "Text",
                                    "text": "消息",
                                    "bounds": "[706,2251][806,2326]",
                                }
                            }
                        ],
                    }
                ],
            }
        ],
    }
    point, audit = runner.align_click_to_xml_node(
        (730, 2378),
        [638, 2314, 822, 2443],
        "bottom navigation button 消息",
        hierarchy,
        1080,
        2444,
        action_type="click",
    )
    assert point == (756, 2288)
    assert audit["snapped"] is True
    assert audit["selected_node"]["bounds"] == [706, 2251, 806, 2326]


def test_stage7_runner_alignment_does_not_hijack_fab_with_adjacent_unlabeled_control():
    """A partial overlap cannot move a correct FAB click onto a nearby icon."""
    from app_test_agent.mobiagent_executor import _import_original_mobiagent

    runner = _import_original_mobiagent()
    hierarchy = {
        "attributes": {},
        "children": [
            {
                "attributes": {
                    "type": "android.view.ViewGroup",
                    "clickable": "true",
                    "enabled": "true",
                    "bounds": "[905,1403][1030,1528]",
                }
            }
        ],
    }
    point, audit = runner.align_click_to_xml_node(
        (941, 1605),
        [848, 1510, 1034, 1700],
        "red floating action button with white plus for 新建笔记",
        hierarchy,
        1080,
        2444,
        action_type="click",
    )
    assert point == (941, 1605)
    assert audit["snapped"] is False
    assert audit["candidate_center_in_model_bounds"] is False
    assert audit["rejection_reason"] == "geometry_only_rejects_low_information_container"
    assert runner._alignment_rejection_blocks_click(audit) is True


def test_stage7_runner_does_not_dispatch_weak_xml_alignment(tmp_path, monkeypatch):
    from app_test_agent.mobiagent_executor import _import_original_mobiagent

    runner = _import_original_mobiagent()
    image_path = tmp_path / "frame.jpg"
    Image.new("RGB", (1080, 2444), "white").save(image_path)

    class Recorder:
        def __init__(self):
            self.clicks = []

        def click(self, x, y):
            self.clicks.append((x, y))

    monkeypatch.setattr(
        runner,
        "call_model_with_validation_retry",
        lambda *_args, **_kwargs: {"bbox": [100, 100, 200, 200]},
    )
    device = Recorder()
    actions = []
    hierarchy = {
        "attributes": {},
        "children": [
            {
                "attributes": {
                    "type": "__Common__",
                    "clickable": "true",
                    "enabled": "true",
                    "bounds": "[548,1254][1065,2213]",
                }
            },
            {
                "attributes": {
                    "type": "Text",
                        "text": "消息",
                    "clickable": "true",
                    "enabled": "true",
                    "bounds": "[0,0][100,100]",
                }
            },
        ],
    }
    with pytest.raises(ValueError, match="target alignment rejected before dispatch"):
        runner.handle_click_action(
            {
                "reasoning": "navigate to the Messages tab",
                "parameters": {
                    "bbox": [324, 914, 427, 988],
                    "target_element": "底部导航栏的消息标签",
                },
            },
            device,
            Image.open(image_path),
            "",
            "{reasoning} {description}",
            "{reasoning} {description}",
            True,
            False,
            False,
            str(tmp_path),
            {"current_dir": str(tmp_path), "screenshot_name": image_path.name},
            image_path.name,
            1,
            actions,
            [],
            hierarchy,
        )
    assert device.clicks == []
    assert actions == []


def test_stage7_runner_visual_fab_resolver_uses_red_compact_lower_right_control():
    from app_test_agent.mobiagent_executor import _import_original_mobiagent
    from PIL import ImageDraw

    runner = _import_original_mobiagent()
    image = Image.new("RGB", (1080, 2444), "white")
    ImageDraw.Draw(image).ellipse((855, 1819, 1011, 1975), fill=(240, 45, 45))
    hierarchy = {
        "attributes": {},
        "children": [
            {
                "attributes": {
                    "type": "android.view.ViewGroup",
                    "clickable": "true",
                    "enabled": "true",
                    "bounds": "[855,1819][1011,1975]",
                }
            }
        ],
    }
    result = runner.find_visual_floating_action_button(
        "red floating action button with white plus for 新建笔记",
        hierarchy,
        image,
        1080,
        2444,
    )
    assert result is not None
    assert result["click_point"] == [933, 1897]
    assert result["red_pixel_ratio"] > 0.5


def test_stage7_runner_visual_fab_resolver_finds_unlabeled_bottom_navigation_add_control():
    from app_test_agent.mobiagent_executor import _import_original_mobiagent
    from PIL import ImageDraw

    runner = _import_original_mobiagent()
    image = Image.new("RGB", (1080, 2444), "white")
    ImageDraw.Draw(image).rectangle((432, 2220, 648, 2358), fill=(240, 45, 45))
    hierarchy = {
        "attributes": {},
        "children": [
            {
                "attributes": {
                    "type": "Row",
                    "clickable": "true",
                    "enabled": "true",
                    "bounds": "[432,2220][648,2358]",
                }
            },
            {
                "attributes": {
                    "type": "Row",
                    "clickable": "true",
                    "enabled": "true",
                    "bounds": "[669,2220][843,2358]",
                }
            },
        ],
    }
    result = runner.find_visual_floating_action_button(
        "底部导航中间的发布加号按钮",
        hierarchy,
        image,
        1080,
        2444,
    )
    assert result is not None
    assert result["click_point"] == [540, 2289]


def test_stage7_runner_input_reactivates_only_the_latest_click_input_target():
    from app_test_agent.mobiagent_executor import _import_original_mobiagent

    class Recorder:
        def __init__(self):
            self.clicks = []
            self.inputs = []

        def click(self, x, y):
            self.clicks.append((x, y))

        def input(self, text):
            self.inputs.append(text)

    runner = _import_original_mobiagent()
    device = Recorder()
    actions = [{"type": "click_input", "click_point": [300, 400]}]
    runner.handle_input_action(
        {"parameters": {"text": "评测智能体"}}, device, 2, actions, []
    )
    assert device.clicks == [(300, 400)]
    assert device.inputs == ["评测智能体"]
    assert actions[-1]["focus_reactivated"] is True
    assert actions[-1]["focus_point"] == [300, 400]
    # The app-test executor dispatches each micro-action through a fresh local
    # action list, so the focus must also survive on the device session.
    runner.handle_input_action(
        {"parameters": {"text": "下一段"}}, device, 3, [], []
    )
    assert device.clicks[-1] == (300, 400)
    assert device.inputs[-1] == "下一段"


def test_stage7_runner_input_rejects_unknown_focus_and_unsafe_swipe_falls_back(tmp_path):
    from app_test_agent.mobiagent_executor import _import_original_mobiagent

    class Recorder:
        def __init__(self):
            self.swipes = []

        def click(self, *_args):
            raise AssertionError("input without focus must not click")

        def input(self, _text):
            raise AssertionError("input without focus must not type")

        def swipe_with_coords(self, *coords):
            self.swipes.append(coords)

    runner = _import_original_mobiagent()
    device = Recorder()
    try:
        runner.handle_input_action({"parameters": {"text": "unsafe"}}, device, 1, [], [])
    except ValueError as exc:
        assert "unknown focus" in str(exc)
    else:
        raise AssertionError("input without a prior click_input target must be rejected")
    runner.handle_swipe_action(
        {"parameters": {"direction": "UP", "start_coords": [540, 400], "end_coords": [540, 1800]}},
        device,
        Image.new("RGB", (1080, 2444), "white"),
        False,
        False,
        str(tmp_path),
        2,
        [],
        [],
    )
    assert device.swipes == [(540.0, 1710.8, 540.0, 733.1999999999999)]


def test_stage7_semantically_snapped_button_is_conformant_without_direct_hits():
    spec = load_test_case(CASE)
    gate = evaluate_step_gate(
        test_case=spec,
        step=spec.steps[2],
        action_record={
            "type": "click",
            "action_index": 1,
            "click_point": [949, 1344],
            "xml_hit_test_result": {
                "snapped": True,
                "alignment_basis": "target_semantic_match",
                "direct_hits": [],
                "selected_node": {
                    "tag": "__Common__",
                    "text": "",
                    "semantic_text": "",
                    "semantic_context": "Button Row Send Send Text",
                    "bounds": [887, 1326, 1012, 1363],
                    "attributes": {"type": "__Common__"},
                },
            },
        },
        attempt=1,
        pre_frame=_frame(0, ("Editor",), 0),
        post_frames=(_frame(1, ("Editor", "hello test 123"), 500),),
        next_step=None,
    )
    assert gate.target_evidence == "CONFORMANT"
    assert gate.action_conformance == "CONFORMANT"


def test_stage7_open_app_first_step_is_not_started_before_dispatch(tmp_path):
    payload = _case_payload()
    payload["steps"] = [
        {
            "step_id": "open_app",
            "instruction": "Open the App",
            "action_type": "OPEN_APP",
            "timeout_seconds": 5,
            "max_retries": 0,
        }
    ]
    payload["expected_results"] = [
        {
            "assertion_id": "app_opened",
            "type": "STATE_CHANGED",
            "after_step": "open_app",
        }
    ]
    spec = AppTestCaseSpec.from_json(payload)
    device = _FakeMobiAgentDevice()
    device.app_start_calls = 0
    original_app_start = device.app_start

    def counted_app_start(package):
        device.app_start_calls += 1
        original_app_start(package)

    device.app_start = counted_app_start
    record = MobiAgentStepExecutor(
        output_dir=tmp_path / "trace",
        device_instance=device,
        observation_sleep_scale=0,
    ).execute(spec)
    assert record.step_results[0].status == "STEP_COMPLETED"
    assert device.app_start_calls == 1


def test_stage7_target_conformance_does_not_accept_model_bounds_alone():
    spec = load_test_case(CASE)
    gate = evaluate_step_gate(
        test_case=spec,
        step=spec.steps[0],
        action_record={
            "type": "click",
            "action_index": 1,
            "click_point": [540, 2250],
            "bounds": [400, 2100, 680, 2400],
        },
        attempt=1,
        pre_frame=_frame(0, ("Feed", "Post"), 0),
        post_frames=(_frame(1, ("Editor",), 500),),
        next_step=spec.steps[1],
    )
    assert gate.target_evidence == "UNKNOWN"
    assert gate.gate_decision == "INCONCLUSIVE"


def test_stage7_xml_runtime_bounds_override_stale_model_bounds():
    spec = load_test_case(CASE)
    gate = evaluate_step_gate(
        test_case=spec,
        step=spec.steps[0],
        action_record={
            "type": "click",
            "action_index": 1,
            "click_point": [933, 1897],
            "bounds": [788, 1604, 986, 1800],
            "xml_hit_test_result": {
                "snapped": True,
                "alignment_basis": "visual_fab_hierarchy_match",
                "direct_hits": [],
                "selected_node": {
                    "tag": "ViewGroup",
                    "bounds": [855, 1819, 1011, 1975],
                    "attributes": {"type": "ViewGroup"},
                },
            },
        },
        attempt=1,
        pre_frame=_frame(0, ("Feed", "Post"), 0),
        post_frames=(_frame(1, ("Editor",), 500),),
        next_step=spec.steps[1],
    )

    assert gate.target_evidence == "CONFORMANT"
    assert gate.action_conformance == "CONFORMANT"
    assert gate.gate_decision == "CONTINUE"


def test_stage7_exact_hierarchy_text_target_uses_unique_text_node_without_model_grounding(tmp_path):
    spec = load_test_case(CASE)
    step = replace(
        spec.steps[0],
        step_id="open_alice_conversation",
        instruction="Open Alice's conversation",
        target={"role": "conversation", "text_candidates": ["Alice"]},
    )
    frame = {
        **_frame(0, ("Feed", "Alice"), 0),
        "xml_nodes": [
            {
                "tag": "Text",
                "text": "Alice",
                "semantic_text": "Alice Text",
                "bounds": [400, 2100, 680, 2400],
                "clickable": False,
                "enabled": True,
                "visible": True,
                "attributes": {"type": "Text", "text": "Alice"},
            }
        ],
    }
    resolved = _resolve_exact_text_target(frame, step.target, wants_text_input=False)
    assert resolved is not None
    assert resolved["center"] == (540, 2250)

    device = _FakeMobiAgentDevice()
    executor = MobiAgentStepExecutor(
        output_dir=tmp_path,
        device_instance=device,
    )
    executor._decide_with_mobiagent = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("model must not be called")
    )
    action = executor._execute_mobiagent_decided_step(
        device,
        step,
        spec,
        action_index=1,
        raw_trace_dir=tmp_path,
        current_frame=frame,
        expected_runner_actions={"click"},
        wants_text_input=False,
        history=[],
    )
    assert device.clicks == [(540, 2250)]
    assert action["target_source"] == "hierarchy_exact_text"
    assert action["target_match"] is True


def test_stage7_unique_navigation_text_target_uses_hierarchy_without_model(tmp_path):
    spec = load_test_case(CASE)
    step = replace(
        spec.steps[0],
        step_id="open_messages",
        instruction="Open the Messages navigation tab",
        target={"role": "navigation", "text_candidates": ["消息"]},
    )
    frame = {
        **_frame(0, ("Feed", "消息"), 0),
        "xml_nodes": [
            {
                "tag": "Text",
                "text": "消息",
                "bounds": [706, 2251, 806, 2326],
                "clickable": False,
                "enabled": True,
                "visible": True,
                "attributes": {"type": "Text", "text": "消息"},
            }
        ],
    }
    resolved = _resolve_hierarchy_control_target(frame, step, wants_text_input=False)
    assert resolved is not None
    assert resolved["center"] == (756, 2288)
    assert resolved["source"] == "hierarchy_exact_text"


def test_stage7_destination_context_accepts_transient_matching_observation():
    spec = load_test_case(CASE)
    step = replace(
        spec.steps[0],
        step_id="open_chat",
        target={"role": "conversation", "text_candidates": ["青文"]},
    )
    context = _evaluate_post_action_context(
        step,
        (
            _frame(7, ("你好呀我是评测智能体", "青文"), 0),
            _frame(8, ("你好呀我是评测智能体",), 500),
        ),
    )
    assert context is not None
    assert context["status"] == "CONFORMANT"
    assert context["matched_candidates"] == ["青文"]
    assert context["frame_id"] == 7
    assert context["observed_frame_ids"] == [7, 8]


def test_stage7_real_default_keeps_grounder_refinement_for_decider_bbox(tmp_path):
    assert MobiAgentStepExecutor(output_dir=tmp_path)._use_direct_decider_geometry() is False
    assert MobiAgentStepExecutor(output_dir=tmp_path, use_e2e=True)._use_direct_decider_geometry() is True
    assert MobiAgentStepExecutor(
        output_dir=tmp_path,
        step_decider=lambda *_: {},
    )._use_direct_decider_geometry() is True


def test_stage7_hierarchy_control_selector_prefers_editable_leaf_and_exact_button():
    spec = load_test_case(ROOT / "examples" / "qq_send_hello_zhexi_app_test.json")
    frame = {
        **_frame(0, ("辄息", "发送"), 0),
        "xml_nodes": [
            {
                "tag": "Row",
                "text": "",
                "bounds": [0, 2084, 1080, 2233],
                "clickable": False,
                "enabled": True,
                "visible": True,
                "attributes": {"id": "inputBarRow", "type": "Row"},
            },
            {
                "tag": "RichEditor",
                "text": "",
                "bounds": [50, 2102, 806, 2215],
                "clickable": True,
                "enabled": True,
                "visible": True,
                "attributes": {"id": "inputBar1", "type": "RichEditor"},
            },
            {
                "tag": "Button",
                "text": "发送",
                "bounds": [843, 2102, 1031, 2215],
                "clickable": False,
                "enabled": True,
                "visible": True,
                "attributes": {"type": "Button"},
            },
        ],
    }
    input_target = _resolve_hierarchy_control_target(
        frame, spec.steps[2], wants_text_input=True
    )
    send_target = _resolve_hierarchy_control_target(
        frame, spec.steps[3], wants_text_input=False
    )
    assert input_target is not None
    assert input_target["bounds"] == [50, 2102, 806, 2215]
    assert input_target["source"] == "hierarchy_text_input"
    assert send_target is not None
    assert send_target["bounds"] == (843, 2102, 1031, 2215)
    assert send_target["source"] == "hierarchy_exact_button_text"


def test_stage7_decider_bbox_disambiguates_repeated_accessible_labels():
    frame = {
        **_frame(0, ("Notes",), 0),
        "xml_nodes": [
            {
                "tag": "Text",
                "text": "Notes",
                "bounds": [720, 2300, 800, 2380],
                "clickable": False,
                "enabled": True,
                "visible": True,
                "attributes": {"type": "Text"},
            },
            {
                "tag": "Text",
                "text": "Notes",
                "bounds": [790, 1250, 900, 1330],
                "clickable": False,
                "enabled": True,
                "visible": True,
                "attributes": {"type": "Text"},
            },
        ],
    }
    target = _resolve_decider_aligned_text_target(
        frame,
        {"role": "section", "text_candidates": ["Notes"]},
        {"parameters": {"bbox": [359, 598, 456, 664]}},
    )
    assert target is not None
    assert target["bounds"] == (790, 1250, 900, 1330)
    assert target["source"] == "hierarchy_decider_aligned_text"


def test_stage7_required_destination_context_retries_recoverable_navigation_before_later_actions():
    payload = _case_payload()
    payload["steps"] = [
        {
            "step_id": "open_target_conversation",
            "instruction": "Open Alice's conversation",
            "action_type": "CLICK",
            "target": {
                "role": "conversation",
                "text_candidates": ["Alice"],
                "post_action_context": {
                    "text_candidates": ["Alice"],
                    "required": True,
                },
            },
        },
        {
            "step_id": "input_message",
            "instruction": "Input the configured message",
            "action_type": "INPUT",
            "target": {"role": "text_input"},
            "value_ref": "post_content",
        },
    ]
    payload["expected_results"] = [
        {
            "assertion_id": "message_visible",
            "type": "TEXT_VISIBLE",
            "expected_value_ref": "post_content",
            "after_step": "input_message",
        }
    ]
    spec = AppTestCaseSpec.from_json(payload)
    gate = evaluate_step_gate(
        test_case=spec,
        step=spec.steps[0],
        action_record={
            "type": "click",
            "action_index": 1,
            "target_match": True,
            "post_action_context": {
                "status": "NON_CONFORMANT",
                "required": True,
                "text_candidates": ["Alice"],
                "visible_texts": ["Bob", "Send"],
            },
        },
        attempt=1,
        pre_frame=_frame(0, ("Alice",), 0),
        post_frames=(_frame(1, ("Bob", "Send"), 500),),
        next_step=spec.steps[1],
        next_step_target_evidence="CONFORMANT",
    )
    assert gate.target_evidence == "NON_CONFORMANT"
    assert gate.action_conformance == "NON_CONFORMANT"
    assert gate.gate_decision == "RETRY"
    assert "returning" in gate.reason


def test_stage7_button_step_rejects_a_rich_editor_hit_even_when_xml_hit_exists():
    spec = load_test_case(CASE)
    step = spec.steps[2]
    rich_editor_hit = {
        "alignment_basis": "direct_supported_hit",
        "direct_hits": [
            {
                "tag": "RichEditor",
                "text": "",
                "semantic_text": "发消息... rich_editor_social_use RichEditor",
                "bounds": [175, 2220, 756, 2308],
                "clickable": True,
                "enabled": True,
                "attributes": {
                    "id": "rich_editor_social_use",
                    "type": "RichEditor",
                    "clickable": "true",
                    "enabled": "true",
                },
            }
        ],
    }
    gate = evaluate_step_gate(
        test_case=spec,
        step=step,
        action_record={
            "type": "click",
            "action_index": 1,
            "xml_hit_test_result": rich_editor_hit,
        },
        attempt=1,
        pre_frame=_frame(0, ("Editor",), 0),
        post_frames=(_frame(1, ("Editor",), 500),),
        next_step=None,
    )
    assert gate.target_evidence == "NON_CONFORMANT"
    assert gate.action_conformance == "NON_CONFORMANT"
    assert gate.gate_decision == "TEST_EXECUTION_FAIL"


def test_stage7_input_step_accepts_a_semantic_chat_input_container():
    spec = load_test_case(CASE)
    step = spec.steps[1]
    gate = evaluate_step_gate(
        test_case=spec,
        step=step,
        action_record={
            "type": "click_input",
            "action_index": 1,
            "text": "hello test 123",
            "click_point": [540, 2200],
            "xml_hit_test_result": {
                "alignment_basis": "direct_supported_hit",
                "direct_hits": [
                    {
                        "tag": "Column",
                        "text": "",
                        "semantic_text": "ChatInputArea 1 Column",
                        "bounds": [0, 2000, 1080, 2350],
                        "clickable": False,
                        "enabled": True,
                        "attributes": {
                            "id": "ChatInputArea",
                            "type": "Column",
                        },
                    }
                ],
            },
        },
        attempt=1,
        pre_frame=_frame(0, ("Feed",), 0),
        post_frames=(_frame(1, ("Feed",), 500),),
        next_step=spec.steps[2],
    )
    assert gate.target_evidence == "CONFORMANT"
    assert gate.action_conformance == "CONFORMANT"
    assert gate.gate_decision == "CONTINUE"


def test_stage7_goal_micro_gate_wrong_target_blocks_goal_completion():
    payload = _case_payload()
    payload["steps"] = [
        {
            "step_id": "create_text_post",
            "instruction": "完成一次文字发帖",
            "action_type": "GUI_TASK",
            "step_mode": "GOAL",
            "target": {"stage_result_text_candidates": ["发布完成"]},
        }
    ]
    payload["expected_results"] = ["可以看到刚才发布内容"]
    spec = AppTestCaseSpec.from_json(payload).with_runtime_context(run_id="micro-wrong")
    step = spec.steps[0]
    action = {
        "type": "click",
        "action_index": 101,
        "target_match": False,
        "target_element": "wrong tab",
    }
    micro_gate = evaluate_micro_action_gate(
        test_case=spec,
        step=step,
        micro_action_index=1,
        action_record=action,
        pre_frame=_frame(0, ("Feed", "Post"), 0),
        post_frame=_frame(1, ("发布完成",), 250),
    )
    assert micro_gate.target_evidence == "NON_CONFORMANT"
    assert micro_gate.gate_decision == "TEST_EXECUTION_FAIL"
    goal_gate = evaluate_step_gate(
        test_case=spec,
        step=step,
        action_record={
            "type": "gui_task",
            "action_index": 1,
            "action_ids": [101],
            "micro_gates": [micro_gate.as_dict()],
            "goal_state": {
                "status": "BLOCKED",
                "last_micro_gate_decision": "TEST_EXECUTION_FAIL",
            },
        },
        attempt=1,
        pre_frame=_frame(0, ("Feed", "Post"), 0),
        post_frames=(_frame(1, ("发布完成",), 250),),
        next_step=None,
    )
    assert goal_gate.action_conformance == "NON_CONFORMANT"
    assert goal_gate.gate_decision == "TEST_EXECUTION_FAIL"


def test_stage7_goal_micro_env_blocked_is_not_hidden_by_stage_marker():
    payload = _case_payload()
    payload["steps"] = [
        {
            "step_id": "create_text_post",
            "instruction": "完成一次文字发帖",
            "action_type": "GUI_TASK",
            "step_mode": "GOAL",
            "target": {"stage_result_text_candidates": ["发布完成"]},
        }
    ]
    payload["expected_results"] = ["可以看到刚才发布内容"]
    spec = AppTestCaseSpec.from_json(payload).with_runtime_context(run_id="micro-env")
    step = spec.steps[0]
    micro_gate = {
        "step_id": step.step_id,
        "attempt": 1,
        "pre_frame": 0,
        "post_frames": [1],
        "action_ids": [101],
        "target_evidence": "UNKNOWN",
        "action_conformance": "UNKNOWN",
        "progress_status": "UNKNOWN",
        "environment_signal": "login",
        "gate_decision": "ENV_BLOCKED",
        "reason": "environment blocker observed after micro-action: login",
        "runtime_intent": {},
    }
    goal_gate = evaluate_step_gate(
        test_case=spec,
        step=step,
        action_record={
            "type": "gui_task",
            "action_index": 1,
            "action_ids": [101],
            "micro_gates": [micro_gate],
        },
        attempt=1,
        pre_frame=_frame(0, ("Feed", "Post"), 0),
        post_frames=(_frame(1, ("发布完成", "login"), 250),),
        next_step=None,
    )
    assert goal_gate.gate_decision == "ENV_BLOCKED"
    assert goal_gate.environment_signal == "login"


def test_stage7_goal_micro_inconclusive_is_not_hidden_by_stage_marker():
    payload = _case_payload()
    payload["steps"] = [
        {
            "step_id": "create_text_post",
            "instruction": "完成一次文字发帖",
            "action_type": "GUI_TASK",
            "step_mode": "GOAL",
            "target": {"stage_result_text_candidates": ["发布完成"]},
        }
    ]
    payload["expected_results"] = ["可以看到刚才发布内容"]
    spec = AppTestCaseSpec.from_json(payload).with_runtime_context(run_id="micro-inconclusive")
    step = spec.steps[0]
    micro_gate = {
        "step_id": step.step_id,
        "attempt": 1,
        "pre_frame": 0,
        "post_frames": [1],
        "action_ids": [101],
        "target_evidence": "UNKNOWN",
        "action_conformance": "UNKNOWN",
        "progress_status": "UNKNOWN",
        "environment_signal": None,
        "gate_decision": "INCONCLUSIVE",
        "reason": "micro-action target evidence insufficient",
        "runtime_intent": {},
    }
    goal_gate = evaluate_step_gate(
        test_case=spec,
        step=step,
        action_record={
            "type": "gui_task",
            "action_index": 1,
            "action_ids": [101],
            "micro_gates": [micro_gate],
        },
        attempt=1,
        pre_frame=_frame(0, ("Feed", "Post"), 0),
        post_frames=(_frame(1, ("发布完成",), 250),),
        next_step=None,
    )
    assert goal_gate.gate_decision == "INCONCLUSIVE"
    assert "micro-action was inconclusive" in goal_gate.reason


def test_stage_c_step_gate_retries_pre_dispatch_target_failure(tmp_path):
    payload = _case_payload()
    payload["test_case_id"] = "step-gate-retry-target-001"
    payload["steps"] = [payload["steps"][0]]
    payload["expected_results"] = [{"assertion_id": "state_changed", "type": "STATE_CHANGED"}]
    spec = AppTestCaseSpec.from_json(payload).with_runtime_context(run_id="gate-retry")
    attempts = {"count": 0}

    def flaky_locator(step, test_case, current_frame, wants_text_input):
        del step, test_case, current_frame, wants_text_input
        attempts["count"] += 1
        if attempts["count"] == 1:
            return None
        return {
            "x": 540,
            "y": 2250,
            "bounds": [400, 2100, 680, 2400],
            "target_element": "post creation entry",
            "reason": "second attempt located target",
        }

    executor = MobiAgentStepExecutor(
        output_dir=tmp_path,
        device_instance=_FakeMobiAgentDevice(),
        target_locator=flaky_locator,
    )
    record = executor.execute(spec)
    assert record.step_results[0].status == "STEP_COMPLETED"
    assert record.step_results[0].attempts == 2
    gates = record.step_results[0].evidence["step_gate_attempts"]
    assert gates[0]["gate_decision"] == "RETRY"
    assert gates[-1]["gate_decision"] == "CONTINUE"
    attempts = record.step_results[0].evidence["attempt_evidence"]
    assert len(attempts) == 2
    assert attempts[0]["action_dispatched"] is False
    assert attempts[0]["retry_class"] == "PRE_DISPATCH_RETRY"
    assert attempts[0]["action_ids"] == []
    assert attempts[1]["action_dispatched"] is True
    assert attempts[1]["gate_decision"] == "CONTINUE"
    json.dumps(attempts, ensure_ascii=False)


def test_stage7_dispatched_business_actions_are_never_classified_as_safe_retries():
    spec = load_test_case(CASE)
    submit = spec.steps[2]
    input_step = spec.steps[1]
    dispatched_submit = {
        "type": "click",
        "action_index": 41,
        "action_ids": [41],
        "target_match": True,
        "click_point": [540, 2200],
        "runtime_bounds": [400, 2100, 680, 2300],
        "post_action_context": {
            "required": True,
            "status": "NON_CONFORMANT",
            "text_candidates": ["Feed"],
        },
    }
    assert _retry_is_safe(submit, dispatched_submit) is False
    assert _needs_navigation_context_recovery(submit, dispatched_submit) is False
    assert _retry_is_safe(input_step, {"type": "click_input", "action_index": 42}) is False

    goal_payload = _case_payload()
    goal_payload["steps"] = [
        {
            "step_id": "goal",
            "instruction": "完成一次文字发帖",
            "action_type": "GUI_TASK",
            "step_mode": "GOAL",
            "target": {"stage_result_text_candidates": ["发布完成"]},
        }
    ]
    goal_payload["expected_results"] = ["发布完成"]
    goal = AppTestCaseSpec.from_json(goal_payload).steps[0]
    assert _retry_is_safe(goal, {"type": "gui_task", "action_ids": [101]}) is False


def test_stage7_navigation_recovery_requires_runtime_target_proof_and_read_only_role():
    payload = _case_payload()
    payload["steps"] = [
        {
            "step_id": "open_chat",
            "instruction": "Open Alice's conversation",
            "action_type": "CLICK",
            "target": {
                "role": "conversation",
                "text_candidates": ["Alice"],
                "post_action_context": {"required": True, "text_candidates": ["Alice"]},
            },
        }
    ]
    payload["expected_results"] = ["Alice"]
    spec = AppTestCaseSpec.from_json(payload)
    step = spec.steps[0]
    base = {
        "type": "click",
        "action_index": 7,
        "target_match": True,
        "post_action_context": {"status": "NON_CONFORMANT", "required": True},
    }
    assert _needs_navigation_context_recovery(step, {**base, "bounds": [0, 0, 100, 100]}) is False
    assert _needs_navigation_context_recovery(
        step,
        {
            **base,
            "click_point": [50, 50],
            "runtime_bounds": [0, 0, 100, 100],
        },
    ) is True

    write_step = replace(
        step,
        instruction="Send the message",
        target={"role": "conversation", "text_candidates": ["Alice"]},
    )
    assert _needs_navigation_context_recovery(write_step, {
        **base,
        "click_point": [50, 50],
        "runtime_bounds": [0, 0, 100, 100],
    }) is False


def test_stage_c_observation_burst_captures_async_progress_without_app_verdict(tmp_path):
    payload = _case_payload()
    payload["test_case_id"] = "step-gate-async-burst-001"
    payload["steps"] = [payload["steps"][0]]
    payload["expected_results"] = [{"assertion_id": "state_changed", "type": "STATE_CHANGED"}]
    spec = AppTestCaseSpec.from_json(payload).with_runtime_context(run_id="gate-async")
    executor = MobiAgentStepExecutor(
        output_dir=tmp_path,
        device_instance=_AsyncEditorMobiAgentDevice(),
        step_decider=_fake_step_decider,
    )
    record = executor.execute(spec)
    result = record.step_results[0]
    assert result.status == "STEP_COMPLETED"
    assert len(result.post_frames) == 3
    burst = result.evidence["post_observation_burst"]
    assert burst["relative_to_action_ms"] == [0, 500, 1000]
    assert burst["stability_sequence"] == ["STABLE", "CHANGED", "STABLE"]
    assert result.evidence["progress_status"] == "ASYNC_PAGE_CHANGED"
    assert result.evidence["target_evidence"] == "CONFORMANT"


def test_stage7_observation_burst_stops_after_stability_but_preserves_terminal_window(tmp_path):
    executor = MobiAgentStepExecutor(output_dir=tmp_path)

    class StableDevice:
        def screenshot(self, path):
            Image.new("RGB", (8, 8), "white").save(path)

        def dump_hierarchy(self):
            return '<hierarchy><node text="stable result" bounds="[0,0][8,8]" /></hierarchy>'

    device = StableDevice()
    policy = {"immediate": True, "delays_ms": [50, 100], "max_wait_ms": 100, "stop_when_stable": True, "adaptive_capture": True}
    pre = {"visible_texts": ["stable result"]}
    adaptive = executor._capture_observation_burst(
        device, tmp_path, next_frame_id=1, pre_frame=pre, policy=policy
    )
    terminal = executor._capture_observation_burst(
        device, tmp_path, next_frame_id=10, pre_frame=pre, policy=policy, force_full_schedule=True
    )
    assert [frame["relative_to_action_ms"] for frame in adaptive] == [0]
    assert adaptive[-1]["observation_stop_reason"] == "consecutive_stable_frames"
    assert [frame["relative_to_action_ms"] for frame in terminal] == [0, 50, 100]


def test_stage6_mobiagent_step_executor_rejects_placeholder_model_config(tmp_path, monkeypatch):
    payload = _case_payload()
    payload["test_case_id"] = "real-step-placeholder-model-001"
    payload["app_under_test"]["package"] = "com.example.demoforum"
    spec = AppTestCaseSpec.from_json(payload).with_runtime_context(run_id="placeholder-model")
    monkeypatch.setenv("MOBIAGENT_BASE_URL", "https://YOUR_MODEL_ENDPOINT/v1")
    monkeypatch.setenv("MOBIAGENT_MODEL", "YOUR_MODEL_NAME")
    monkeypatch.setenv("MOBIAGENT_API_KEY", "YOUR_KEY")

    executor = MobiAgentStepExecutor(
        output_dir=tmp_path,
        device_instance=_FakeJsonMobiAgentDevice(),
    )
    record = executor.execute(spec)
    assert record.step_results[0].status == "STEP_FAILED"
    assert "placeholder value" in str(record.step_results[0].error)
    actions = json.loads((Path(record.raw_trace_dir or "") / "actions.json").read_text(encoding="utf-8"))
    assert actions["action_count"] == 0


def test_stage7_model_service_403_fails_once_and_is_classified_as_environment_blocked(tmp_path, monkeypatch):
    from runner.mobiagent import mobiagent as runner_mobiagent

    calls = {"count": 0}

    def rejected_request(*args, **kwargs):
        del args, kwargs
        calls["count"] += 1

        class ForbiddenError(RuntimeError):
            status_code = 403

        raise ForbiddenError("Your request was blocked.")

    monkeypatch.setattr(runner_mobiagent, "_requests_chat_completion", rejected_request)
    with pytest.raises(runner_mobiagent.ModelServiceConfigurationError) as caught:
        runner_mobiagent.call_model_with_validation_retry(
            client=None,
            model="test-model",
            messages=[],
            validator_func=lambda payload: payload,
            max_retries=5,
            context="Decider",
        )
    assert caught.value.status_code == 403
    assert calls["count"] == 1

    payload = _case_payload()
    spec = AppTestCaseSpec.from_json(payload).with_runtime_context(run_id="model-service-blocked")
    blocked = runner_mobiagent.ModelServiceConfigurationError("Decider", 403, RuntimeError("blocked"))
    executor = MobiAgentStepExecutor(
        output_dir=tmp_path,
        device_instance=_FakeMobiAgentDevice(),
        step_decider=lambda *args, **kwargs: (_ for _ in ()).throw(blocked),
    )
    record = executor.execute(spec)
    assert record.step_results[0].status == "ENV_BLOCKED"
    assert record.step_results[0].blocker == "model_service_access"


def test_stage7_remote_runner_requires_explicit_key_before_device_mutation(tmp_path, monkeypatch):
    payload = _case_payload()
    spec = AppTestCaseSpec.from_json(payload).with_runtime_context(run_id="missing-model-key")
    monkeypatch.setenv("MOBIAGENT_BASE_URL", "https://example.invalid/v1")
    monkeypatch.delenv("MOBIAGENT_API_KEY", raising=False)
    monkeypatch.delenv("MOBIAGENT_API_KEY_FILE", raising=False)

    executor = MobiAgentStepExecutor(
        output_dir=tmp_path,
        device_instance=_FakeMobiAgentDevice(),
    )
    record = executor.execute(spec)
    assert record.step_results[0].status == "ENV_BLOCKED"
    assert "without MOBIAGENT_API_KEY" in str(record.step_results[0].error)
    assert not (Path(record.raw_trace_dir or "") / "0.jpg").exists()

    key_file = tmp_path / "runner-api-key.txt"
    key_file.write_text("test-only-key\n", encoding="utf-8")
    monkeypatch.setenv("MOBIAGENT_API_KEY_FILE", str(key_file))
    from runner.mobiagent import mobiagent as runner_mobiagent

    assert runner_mobiagent._configured_api_key() == "test-only-key"


def test_stage7_remote_model_endpoint_uses_raw_http_transport_by_default(monkeypatch):
    from runner.mobiagent import mobiagent as runner_mobiagent

    monkeypatch.setenv("MOBIAGENT_DECIDER_BASE_URL", "https://api.example.test/v1")
    monkeypatch.delenv("MOBIAGENT_LLM_TRANSPORT", raising=False)
    assert runner_mobiagent._use_raw_http_transport() is True

    monkeypatch.setenv("MOBIAGENT_LLM_TRANSPORT", "openai_sdk")
    assert runner_mobiagent._use_raw_http_transport() is False

    monkeypatch.setenv("MOBIAGENT_LLM_TRANSPORT", "raw_http")
    assert runner_mobiagent._use_raw_http_transport() is True


def test_stage6_app_verifier_can_use_visual_post_action_evidence(tmp_path, monkeypatch):
    payload = _case_payload()
    payload["test_data"]["post_content"] = "app_test_visual_001"
    payload["forbidden_effects"] = []
    spec = AppTestCaseSpec.from_json(payload)
    steps = tuple(completed_step(step, spec, index) for index, step in enumerate(spec.steps))
    screenshot = tmp_path / "3.jpg"
    screenshot.write_bytes(b"visual screenshot bytes")
    frames = [
        _frame(0, ("Feed",), 0),
        {
            **_frame(3, ("Feed",), 500),
            "screenshot": "3.jpg",
            "screenshot_abs": str(screenshot),
        },
    ]
    record = ExecutionRecord(
        test_case_id=spec.test_case_id,
        executor="script-fixture",
        step_results=steps,
        final_state=EvidenceState(
            visible_texts=("Feed",),
            state_changed=True,
            evidence_sufficient=True,
        ),
        raw_trace_dir=str(tmp_path),
        metadata={
            "initial_visible_texts": ["Feed"],
            "frame_visible_texts": {"0": ["Feed"], "3": ["Feed"]},
            "frames": frames,
        },
    )

    import app_test_agent.app_verifier as app_verifier

    monkeypatch.setenv("APP_TEST_ENABLE_VLM_VERIFIER", "1")
    monkeypatch.setattr(
        app_verifier,
        "_model_visual_assertion",
        lambda expected_value, screenshot_paths: {
            "visible": True,
            "confidence": 0.91,
            "reason": f"{expected_value} visible",
            "matched_text": expected_value,
            "screenshot_count": len(screenshot_paths),
        },
    )
    report = run_app_test(spec, ScriptedStepExecutor(record), run_id="visual-001")
    assert report["overall_result"] == OverallResult.APP_PASS
    assertion = report["app_behavior_result"]["assertion_results"][0]
    assert assertion["evidence"]["visual_verifier"]["status"] == "VISIBLE"


class _FakeReadOnlyVerificationDevice:
    def __init__(self, expected_text: str):
        self.expected_text = expected_text
        self.state = "post_publish_page"
        self.clicks: list[tuple[int, int]] = []
        self.swipes: list[str] = []

    def screenshot(self, path):
        Path(path).write_bytes(b"\xff\xd8\xff\xd9")

    def dump_hierarchy(self):
        if self.state == "profile":
            return (
                """<hierarchy>"""
                """<node text="我" bounds="[900,2200][1040,2380]" />"""
                """<node text="笔记" clickable="true" bounds="[60,620][200,700]" />"""
                f"""<node text="{self.expected_text}" bounds="[60,820][1000,920]" />"""
                """</hierarchy>"""
            )
        return (
            """<hierarchy>"""
            """<node text="发布完成" bounds="[40,100][500,180]" />"""
            """<node text="我" clickable="true" bounds="[900,2200][1040,2380]" />"""
            """</hierarchy>"""
        )

    def click(self, x, y):
        self.clicks.append((x, y))
        self.state = "profile"

    def swipe(self, direction):
        self.swipes.append(direction)

    def keyevent(self, key):
        pass


class _RetryingReadOnlyVerificationDevice(_FakeReadOnlyVerificationDevice):
    def __init__(self, expected_text: str):
        super().__init__(expected_text)
        self.click_attempts = 0

    def click(self, x, y):
        self.click_attempts += 1
        if self.click_attempts == 1:
            raise RuntimeError("temporary navigation failure")
        super().click(x, y)


def test_stage6_real_verification_runner_collects_read_only_observations(tmp_path):
    payload = _payload_with_verification_steps()
    payload["test_data"]["post_content"] = "app_test_real_verify"
    payload["expected_results"][0]["requires_verification_runner"] = True
    payload["verification_steps"] = [
        {
            "verification_step_id": "navigate_to_profile",
            "instruction": "Navigate to the profile page",
            "action_type": "NAVIGATE",
            "target": {
                "label": "我",
                "coordinates": [970, 2280],
                "surface_text_candidates": ["笔记"],
            },
        },
        {
            "verification_step_id": "observe_profile_notes",
            "instruction": "Observe own note list",
            "action_type": "OBSERVE",
            "target": {
                "surface": "own_note_list",
                "surface_text_candidates": ["笔记"],
            },
        },
    ]
    payload["verification_policy"] = {
        "max_steps": 3,
        "timeout_seconds": 10,
        "max_retries": 0,
    }
    spec = AppTestCaseSpec.from_json(payload)
    record = _direct_unknown_record(spec)
    report = run_app_test(
        spec,
        ScriptedStepExecutor(record),
        run_id="real-verification-runner",
        verification_runner=MobiAgentVerificationRunner(
            output_dir=tmp_path,
            device_instance=_FakeReadOnlyVerificationDevice("app_test_real_verify"),
        ),
    )
    assert report["direct_app_behavior_result"]["status"] == "UNKNOWN_EVIDENCE"
    assert report["verification_runner_result"]["status"] == "COMPLETED"
    assert report["verification_runner_result"]["used_runner"] is True
    assert report["verification_runner_result"]["observation_record"]["executor"] == (
        "mobiagent_real_verification"
    )
    assert report["overall_result"] == OverallResult.APP_PASS


def test_verification_danger_filter_allows_result_reference_but_blocks_write_semantics():
    assert not _dangerous_step_text(
        {
            "action_type": "WAIT",
            "instruction": "Wait briefly for the publish result page or feed transition to settle",
            "target": {},
        }
    )
    assert not _dangerous_step_text(
        {
            "action_type": "OBSERVE",
            "instruction": "Observe the 发布结果页面 for the unique content",
            "target": {"surface_text_candidates": ["发布结果"]},
        }
    )
    assert _dangerous_step_text(
        {
            "action_type": "WAIT",
            "instruction": "Wait, then publish the note",
            "target": {},
        }
    )
    assert _dangerous_step_text(
        {
            "action_type": "NAVIGATE",
            "instruction": "Navigate to the verification surface",
            "target": {"text_candidates": ["发布笔记"]},
        }
    )


def test_real_verification_runner_assigns_unique_frame_ids_across_retries(tmp_path):
    payload = _payload_with_verification_steps()
    payload["test_data"]["post_content"] = "app_test_editor_text"
    payload["verification_steps"][0]["max_retries"] = 1
    payload["verification_steps"][0]["target"]["surface_text_candidates"] = ["我"]
    payload["verification_steps"][2]["target"]["surface_text_candidates"] = ["笔记"]
    payload["verification_policy"] = {
        "max_steps": 3,
        "timeout_seconds": 10,
        "max_retries": 1,
    }
    spec = AppTestCaseSpec.from_json(payload)
    report = run_app_test(
        spec,
        ScriptedStepExecutor(_direct_unknown_record(spec)),
        run_id="verification-unique-frame-ids",
        verification_runner=MobiAgentVerificationRunner(
            output_dir=tmp_path,
            device_instance=_RetryingReadOnlyVerificationDevice("app_test_editor_text"),
        ),
    )
    observation = report["verification_runner_result"]["observation_record"]
    frame_ids = [frame["frame_id"] for frame in observation["metadata"]["frames"]]
    assert frame_ids == [0, 1, 2, 3]
    assert len(frame_ids) == len(set(frame_ids))
    assert report["overall_result"] == OverallResult.APP_PASS


def test_stage7_goal_runner_completes_stage_then_read_only_verifier_confirms_result(tmp_path):
    payload = _case_payload()
    payload["test_case_id"] = "goal-readonly-verification-001"
    payload["app_under_test"]["package"] = "com.example.demoforum"
    payload["test_data"] = {}
    payload["steps"] = [
        {
            "step_id": "create_text_post",
            "instruction": "完成一次文字发帖",
            "action_type": "GUI_TASK",
            "step_mode": "GOAL",
            "target": {
                "stage_result_text_candidates": ["发布完成"],
                "max_micro_actions": 6,
            },
        }
    ]
    payload["expected_results"] = [
        {
            "assertion_id": "generated_post_visible_in_profile",
            "type": "TEXT_VISIBLE",
            "expected_value_ref": "__generated_post_content",
            "surface": "个人主页我的笔记",
            "after_step": "create_text_post",
            "requires_verification_runner": True,
            "historical_match_not_sufficient": True,
        }
    ]
    payload["verification_steps"] = [
        {
            "verification_step_id": "navigate_to_profile",
            "instruction": "Navigate to profile page",
            "action_type": "NAVIGATE",
            "target": {
                "label": "我",
                "coordinates": [970, 2280],
                "surface_text_candidates": ["笔记"],
            },
        },
        {
            "verification_step_id": "observe_profile_notes",
            "instruction": "Observe profile notes",
            "action_type": "OBSERVE",
            "target": {
                "surface": "profile_notes",
                "surface_text_candidates": ["笔记"],
            },
        },
    ]
    spec = AppTestCaseSpec.from_json(payload).with_runtime_context(run_id="xhs-goal")
    decider = _goal_publish_completion_decider()
    report = run_app_test(
        spec,
        MobiAgentStepExecutor(
            output_dir=tmp_path / "business",
            device_instance=_PublishCompletionOnlyDevice(),
            step_decider=decider,
        ),
        output_dir=tmp_path / "bundle",
        run_id="xhs-goal",
        verification_runner=MobiAgentVerificationRunner(
            output_dir=tmp_path / "verification",
            device_instance=_FakeReadOnlyVerificationDevice("app_test_xhs-goal_post_content"),
        ),
    )
    step = report["step_results"][0]
    assert step["status"] == "STEP_COMPLETED"
    assert step["evidence"]["goal_completed"] is True
    assert len(step["evidence"]["micro_action_observations"]) == 3
    assert len(step["evidence"]["micro_gates"]) == 3
    assert step["evidence"]["goal_state"]["status"] == "COMPLETED"
    assert step["evidence"]["progress_status"] == "GOAL_RESULT_CONFIRMED"
    assert step["evidence"]["goal_completion_evidence"]["matched_stage_markers"] == ["发布完成"]
    assert "app_test_xhs-goal_post_content" not in report["final_evidence_state"]["visible_texts"]
    assert report["direct_app_behavior_result"]["status"] == "UNKNOWN_EVIDENCE"
    assert report["verification_runner_result"]["status"] == "COMPLETED"
    assert report["verification_runner_result"]["used_runner"] is True
    assert report["overall_result"] == OverallResult.APP_PASS
    envelope = load_run_envelope(tmp_path / "bundle" / "run_envelope.json")
    assert envelope["verification"]["used_runner"] is True
    assert envelope["verification"]["verification_trace_sha256"]
    assert envelope["business_execution"]["steps"][0]["micro_gate_count"] == 3
    temporal = envelope["temporal_boundaries"]
    assert temporal["schema_version"] == "app-test-temporal-boundaries-v1"
    assert temporal["runner_done_frame"]["known"] is True
    assert temporal["runner_done_frame"]["frame"]["frame_id"] == 9
    assert temporal["runner_done_frame"]["source"] == "goal_state.completion_evidence.frame_id"
    assert temporal["verification_runner_surface_reached_frame"]["known"] is True
    assert temporal["verification_runner_surface_reached_frame"]["frame"]["frame_id"]
    assert temporal["result_observation_window"]["business_execution"]
    assert temporal["result_observation_window"]["verification_observation"]


def test_stage6_assertion_can_require_verification_runner_evidence():
    payload = _payload_with_verification_steps()
    payload["test_data"]["post_content"] = "app_test_editor_text"
    payload["expected_results"][0]["requires_verification_runner"] = True
    spec = AppTestCaseSpec.from_json(payload)
    record = _scripted_record(
        spec,
        visible_texts=("Editor", "app_test_editor_text"),
        initial_texts=("Feed",),
        after_submit_texts=("Editor", "app_test_editor_text"),
    )
    report = run_app_test(
        spec,
        ScriptedStepExecutor(record),
        run_id="requires-verification",
        verification_runner=ScriptedVerificationRunner(
            scenario="found",
            visible_texts=("笔记", "app_test_editor_text"),
        ),
    )
    assert report["direct_app_behavior_result"]["status"] == "UNKNOWN_EVIDENCE"
    assert "requires verification runner evidence" in (
        report["direct_app_behavior_result"]["assertion_results"][0]["reason"]
    )
    assert report["overall_result"] == OverallResult.APP_PASS


def test_stage5_contract_is_compiled_only_from_test_case():
    spec = load_test_case(CASE)
    contract = compile_app_test_contract(spec)
    assert contract.test_case_id == spec.test_case_id
    assert contract.test_case_sha256 == spec.sha256
    assert contract.execution_contract["step_order"] == [
        step.step_id for step in spec.steps
    ]
    assert contract.app_oracle_contract["expected_results"][0][
        "resolved_expected_value"
    ] == "hello test 123"
    rendered = json.dumps(contract.as_dict(), ensure_ascii=False)
    assert "task_family" not in rendered


def test_stage5_report_writes_split_verifier_outputs(tmp_path):
    spec = load_test_case(CASE)
    report = run_app_test(
        spec,
        MockStepExecutor(scenario="pass"),
        output_dir=tmp_path,
        run_id="stage5-report",
    )
    contract_path = tmp_path / "app_test_contract.json"
    execution_path = tmp_path / "execution_result.json"
    behavior_path = tmp_path / "app_behavior_result.json"
    envelope_path = tmp_path / "run_envelope.json"
    assert contract_path.is_file()
    assert execution_path.is_file()
    assert behavior_path.is_file()
    assert envelope_path.is_file()
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    execution = json.loads(execution_path.read_text(encoding="utf-8"))
    behavior = json.loads(behavior_path.read_text(encoding="utf-8"))
    envelope = load_run_envelope(envelope_path)
    assert report["contract_sha256"] == contract["contract_sha256"]
    assert report["run_envelope_sha256"] == envelope["run_envelope_sha256"]
    assert execution["contract_sha256"] == contract["contract_sha256"]
    assert behavior["contract_sha256"] == contract["contract_sha256"]
    assert execution["status"] == "COMPLETED"
    assert behavior["status"] == "SATISFIED"
    manifest = json.loads((tmp_path / "test_execution_manifest.json").read_text(encoding="utf-8"))
    assert manifest["contract_sha256"] == contract["contract_sha256"]
    registry = {item["relative_ref"]: item for item in envelope["artifact_registry"]}
    assert registry["app_test_contract.json"]["payload_sha256"] == contract["contract_sha256"]
    assert registry["test_execution_manifest.json"]["payload_sha256"] == canonical_sha256(manifest)
    assert (tmp_path / "attribution_result.json").is_file()
    assert registry["attribution_result.json"]["payload_sha256"] == canonical_sha256(
        report["attribution"]
    )
    assert envelope["result_summary"]["overall_result"] == OverallResult.APP_PASS


def test_stage7_run_envelope_records_step_gate_and_observation_burst(tmp_path):
    payload = _case_payload()
    payload["test_case_id"] = "run-envelope-step-gate-001"
    payload["app_under_test"]["package"] = "com.example.demoforum"
    spec = AppTestCaseSpec.from_json(payload).with_runtime_context(run_id="run-envelope-gate")
    executor = MobiAgentStepExecutor(
        output_dir=tmp_path / "trace",
        device_instance=_AsyncEditorMobiAgentDevice(),
        step_decider=_fake_step_decider,
    )
    report = run_app_test(
        spec,
        executor,
        output_dir=tmp_path / "bundle",
        run_id="run-envelope-gate",
    )
    envelope = load_run_envelope(tmp_path / "bundle" / "run_envelope.json")
    first_step = envelope["business_execution"]["steps"][0]
    assert report["run_envelope_sha256"] == envelope["run_envelope_sha256"]
    assert first_step["gate_decision"] == "CONTINUE"
    assert first_step["target_evidence"] == "CONFORMANT"
    assert first_step["progress_status"] == "NEXT_STEP_TARGET_AVAILABLE"
    assert first_step["next_step_target_evidence"] == "CONFORMANT"
    assert first_step["step_gate_sha256"]
    assert first_step["step_gate_attempts_sha256"]
    assert first_step["post_observation_burst_sha256"]
    assert first_step["post_observation_burst"]["relative_to_action_ms"] == [0, 500, 1000]


def test_stage7_run_envelope_records_auto_verification_intent_and_runner_result(tmp_path):
    payload = json.loads(MINIMAL_USER_CASE.read_text(encoding="utf-8"))
    payload["metadata"]["verification_runner"] = {
        "scenario": "found",
        "observation_sufficient": True,
    }
    spec = AppTestCaseSpec.from_json(payload).with_runtime_context(run_id="envelope-auto-verify")
    report = run_app_test(
        spec,
        ScriptedStepExecutor(_direct_unknown_record(spec)),
        output_dir=tmp_path,
        run_id="envelope-auto-verify",
    )
    envelope = load_run_envelope(tmp_path / "run_envelope.json")
    verification = envelope["verification"]
    runner_result = json.loads((tmp_path / "verification_runner_result.json").read_text(encoding="utf-8"))
    assert report["overall_result"] == OverallResult.APP_PASS
    assert verification["used_runner"] is True
    assert verification["generated_from_verification_intent"] is True
    assert verification["verification_runner_result_sha256"] == canonical_sha256(runner_result)
    assert verification["verification_intent_sha256"] == (
        runner_result["metadata"]["verification_intent_sha256"]
    )
    assert verification["verification_intent"]["expected_texts"] == ["app_test_envelope-auto-verify"]


def test_run_envelope_records_business_action_boundaries_without_explicit_done(tmp_path):
    spec = load_test_case(CASE)
    run_app_test(
        spec,
        MockStepExecutor(scenario="pass"),
        output_dir=tmp_path,
        run_id="temporal-business-boundaries",
    )
    envelope = load_run_envelope(tmp_path / "run_envelope.json")
    temporal = envelope["temporal_boundaries"]
    boundaries = temporal["business_action_boundaries"]
    assert len(boundaries) == len(spec.steps)
    assert boundaries[0]["boundary"]["pre_frame"]["frame_id"] == 0
    assert boundaries[0]["boundary"]["first_post_frame"]["frame_id"] == 1
    assert boundaries[-1]["boundary"]["last_post_frame"]["frame_id"]
    assert temporal["runner_done_frame"]["known"] is False
    assert temporal["runner_done_frame"]["inferred_terminal_frame"]["frame_id"]


def test_stage5_runtime_run_id_template_is_resolved_into_contract():
    payload = _case_payload()
    payload["test_data"]["post_content"] = "app_test_${run_id}"
    spec = AppTestCaseSpec.from_json(payload)
    report = run_app_test(
        spec,
        MockStepExecutor(scenario="pass"),
        run_id="fresh-001",
    )
    assert report["overall_result"] == OverallResult.APP_PASS
    assert report["contract"]["app_oracle_contract"]["expected_results"][0][
        "resolved_expected_value"
    ] == "app_test_fresh-001"


def test_stage5_old_text_without_post_action_freshness_is_inconclusive():
    payload = _case_payload()
    payload["test_data"]["post_content"] = "app_test_old"
    spec = AppTestCaseSpec.from_json(payload)
    record = _scripted_record(
        spec,
        visible_texts=("Feed", "app_test_old"),
        initial_texts=("Feed", "app_test_old"),
        after_submit_texts=("Feed",),
    )
    report = run_app_test(spec, ScriptedStepExecutor(record), run_id="fresh-old")
    assert report["overall_result"] == OverallResult.INCONCLUSIVE
    assert report["app_behavior_result"]["status"] == "UNKNOWN_EVIDENCE"


def test_stage5_persistent_historical_text_without_new_occurrence_is_inconclusive():
    payload = _case_payload()
    payload["test_data"]["post_content"] = "app_test_persistent_old"
    spec = AppTestCaseSpec.from_json(payload)
    record = _scripted_record(
        spec,
        visible_texts=("Feed", "app_test_persistent_old"),
        initial_texts=("Feed", "app_test_persistent_old"),
        after_submit_texts=("Feed", "app_test_persistent_old"),
    )

    report = run_app_test(
        spec,
        ScriptedStepExecutor(record),
        run_id="fresh-persistent-old",
    )

    assertion = report["app_behavior_result"]["assertion_results"][0]
    assert report["overall_result"] == OverallResult.INCONCLUSIVE
    assert assertion["status"] == "UNKNOWN_EVIDENCE"
    assert assertion["evidence"]["freshness"]["proven"] is False


def test_stage5_additional_historical_text_occurrence_proves_freshness():
    payload = _case_payload()
    payload["test_data"]["post_content"] = "app_test_repeated"
    spec = AppTestCaseSpec.from_json(payload)
    record = _scripted_record(
        spec,
        visible_texts=("Feed", "app_test_repeated", "app_test_repeated"),
        initial_texts=("Feed", "app_test_repeated"),
        after_submit_texts=("Feed", "app_test_repeated", "app_test_repeated"),
    )

    report = run_app_test(
        spec,
        ScriptedStepExecutor(record),
        run_id="fresh-repeated-new-occurrence",
    )

    assertion = report["app_behavior_result"]["assertion_results"][0]
    freshness = assertion["evidence"]["freshness"]
    assert report["overall_result"] == OverallResult.APP_PASS
    assert assertion["status"] == "SATISFIED"
    assert freshness["proven"] is True
    assert freshness["initial_count"] == 1
    assert freshness["max_post_count"] == 2
    assert freshness["proof_frame_ids"]


def test_stage5_final_state_text_without_frame_text_cannot_pass_freshness():
    spec = load_test_case(CASE)
    steps = tuple(completed_step(step, spec, index) for index, step in enumerate(spec.steps))
    record = ExecutionRecord(
        test_case_id=spec.test_case_id,
        executor="script-fixture",
        step_results=steps,
        final_state=EvidenceState(
            visible_texts=("Feed", "hello test 123"),
            state_changed=True,
            evidence_sufficient=True,
        ),
        metadata={
            "initial_visible_texts": ["Feed"],
            "frames": [
                {
                    "frame_id": 0,
                    "timestamp_ms": 0,
                    "relative_to_action_ms": 0,
                    "screenshot": "mock://test/0.png",
                    "screenshot_sha256": "0" * 64,
                    "hierarchy": "mock://test/0.xml",
                    "hierarchy_sha256": "1" * 64,
                    "stability": "STABLE",
                },
                {
                    "frame_id": 3,
                    "timestamp_ms": 3000,
                    "relative_to_action_ms": 500,
                    "screenshot": "mock://test/3.png",
                    "screenshot_sha256": "3" * 64,
                    "hierarchy": "mock://test/3.xml",
                    "hierarchy_sha256": "4" * 64,
                    "stability": "STABLE",
                },
            ],
        },
    )
    report = run_app_test(spec, ScriptedStepExecutor(record), run_id="final-only")
    assert report["overall_result"] == OverallResult.INCONCLUSIVE


def test_stage5_post_action_new_text_satisfies_freshness():
    payload = _case_payload()
    payload["test_data"]["post_content"] = "app_test_new"
    spec = AppTestCaseSpec.from_json(payload)
    record = _scripted_record(
        spec,
        visible_texts=("Feed", "app_test_new"),
        initial_texts=("Feed",),
        after_submit_texts=("Feed", "app_test_new"),
    )
    report = run_app_test(spec, ScriptedStepExecutor(record), run_id="fresh-new")
    assert report["overall_result"] == OverallResult.APP_PASS


def test_stage5_direct_evidence_pass_does_not_start_verification_runner():
    payload = _payload_with_verification_steps()
    spec = AppTestCaseSpec.from_json(payload)
    report = run_app_test(
        spec,
        MockStepExecutor(scenario="pass"),
        run_id="verification-direct-pass",
        verification_runner=ScriptedVerificationRunner(scenario="found"),
    )
    assert report["overall_result"] == OverallResult.APP_PASS
    assert report["verification_runner_result"]["status"] == "NOT_RUN"
    assert report["verification_runner_result"]["used_runner"] is False


def test_verification_runner_policy_is_frozen_in_contract_and_report():
    payload = _payload_with_verification_steps()
    payload["verification_runner_policy"] = "REQUIRED_FOR_RESULT"
    spec = AppTestCaseSpec.from_json(payload)
    assert spec.verification_runner_policy == "REQUIRED_FOR_RESULT"
    contract = compile_app_test_contract(spec)
    assert contract.verification_contract["runner_policy"] == "REQUIRED_FOR_RESULT"


def test_verification_runner_policy_rejects_unknown_value():
    payload = _payload_with_verification_steps()
    payload["verification_runner_policy"] = "SOMETIMES"
    try:
        AppTestCaseSpec.from_json(payload)
    except AppTestCaseError as exc:
        assert "verification_runner_policy is unsupported" in str(exc)
    else:
        raise AssertionError("unknown verification runner policy should be rejected")


def test_never_verification_runner_policy_conflicts_with_required_assertion():
    payload = _payload_with_verification_steps()
    payload["verification_runner_policy"] = "NEVER"
    payload["expected_results"][0]["requires_verification_runner"] = True
    try:
        AppTestCaseSpec.from_json(payload)
    except AppTestCaseError as exc:
        assert "conflicts" in str(exc)
    else:
        raise AssertionError("NEVER policy should reject required runner assertions")


def test_required_verification_runner_runs_even_when_direct_evidence_is_decisive():
    payload = _payload_with_verification_steps()
    payload["verification_runner_policy"] = "REQUIRED_FOR_RESULT"
    spec = AppTestCaseSpec.from_json(payload)
    runner = _CountingVerificationRunner()
    report = run_app_test(
        spec,
        MockStepExecutor(scenario="pass"),
        run_id="required-verification",
        verification_runner=runner,
    )
    assert runner.calls == 1
    assert report["verification_runner_policy"] == "REQUIRED_FOR_RESULT"
    assert report["verification_runner_result"]["status"] == "COMPLETED"
    assert report["verification_runner_result"]["used_runner"] is True


def test_never_verification_runner_policy_blocks_runner_call():
    payload = _payload_with_verification_steps()
    payload["verification_runner_policy"] = "NEVER"
    spec = AppTestCaseSpec.from_json(payload)
    runner = _CountingVerificationRunner()
    report = run_app_test(
        spec,
        ScriptedStepExecutor(_direct_unknown_record(spec)),
        run_id="never-verification",
        verification_runner=runner,
    )
    assert runner.calls == 0
    assert report["verification_runner_result"]["status"] == "NOT_RUN"
    assert report["verification_runner_result"]["used_runner"] is False
    assert report["overall_result"] == OverallResult.INCONCLUSIVE


def test_required_verification_runner_without_observable_intent_is_unsupported():
    payload = _case_payload()
    payload.pop("verification_steps", None)
    payload["verification_runner_policy"] = "REQUIRED_FOR_RESULT"
    payload["expected_results"] = [
        {
            "assertion_id": "state_changed",
            "type": "STATE_CHANGED",
        }
    ]
    spec = AppTestCaseSpec.from_json(payload)
    report = run_app_test(
        spec,
        MockStepExecutor(scenario="pass"),
        run_id="required-without-intent",
    )
    assert report["verification_runner_result"]["status"] == "UNSUPPORTED"
    assert report["overall_result"] == OverallResult.UNSUPPORTED


def test_terminal_flow_verifier_rejects_completed_step_action_type_mismatch():
    spec = load_test_case(CASE)
    record = _scripted_record(spec)
    mismatched = replace(record.step_results[0], action_type="INPUT")
    record = replace(record, step_results=(mismatched, *record.step_results[1:]))
    report = run_app_test(spec, ScriptedStepExecutor(record), run_id="flow-action-type")
    assert report["overall_result"] == OverallResult.TEST_EXECUTION_FAIL
    assert report["execution_result"]["failed_step"] == spec.steps[0].step_id
    assert "action_type" in report["execution_result"]["reason"]


def test_terminal_flow_verifier_rejects_completed_step_without_post_frame():
    spec = load_test_case(CASE)
    record = _scripted_record(spec)
    missing_observation = replace(record.step_results[0], post_frames=())
    record = replace(record, step_results=(missing_observation, *record.step_results[1:]))
    report = run_app_test(spec, ScriptedStepExecutor(record), run_id="flow-no-post-frame")
    assert report["overall_result"] == OverallResult.TEST_EXECUTION_FAIL
    assert report["execution_result"]["failed_step"] == spec.steps[0].step_id
    assert "post-observation" in report["execution_result"]["reason"]


def test_terminal_flow_verifier_rejects_atomic_done_signal():
    spec = load_test_case(CASE)
    record = _scripted_record(spec)
    evidence = {
        **dict(record.step_results[0].evidence),
        "model_decision": {"action": "done"},
    }
    premature_done = replace(record.step_results[0], evidence=evidence)
    record = replace(record, step_results=(premature_done, *record.step_results[1:]))
    report = run_app_test(spec, ScriptedStepExecutor(record), run_id="flow-atomic-done")
    assert report["overall_result"] == OverallResult.TEST_EXECUTION_FAIL
    assert "atomic step" in report["execution_result"]["reason"]


def test_terminal_flow_verifier_rejects_unfinished_goal_state():
    payload = _case_payload()
    payload["preconditions"] = []
    payload["steps"] = [
        {
            "step_id": "create_text_post",
            "instruction": "完成一次文字发帖",
            "action_type": "GUI_TASK",
            "step_mode": "GOAL",
        }
    ]
    payload["expected_results"] = ["可以看到刚才发布内容"]
    spec = AppTestCaseSpec.from_json(payload).with_runtime_context(run_id="flow-goal-incomplete")
    result = completed_step(spec.steps[0], spec, 0)
    result = replace(
        result,
        evidence={
            "goal_completed": False,
            "goal_state": {
                "status": "IN_PROGRESS",
                "completed": False,
            },
            "model_decisions": [{"action": "done"}],
        },
    )
    record = ExecutionRecord(
        test_case_id=spec.test_case_id,
        executor="scripted-flow-fixture",
        step_results=(result,),
        final_state=EvidenceState(
            visible_texts=("Feed",),
            state_changed=True,
            evidence_sufficient=True,
        ),
    )
    report = run_app_test(
        spec,
        ScriptedStepExecutor(record),
        run_id="flow-goal-incomplete",
    )
    assert report["overall_result"] == OverallResult.TEST_EXECUTION_FAIL
    assert report["execution_result"]["failed_step"] == "create_text_post"
    assert "terminal state" in report["execution_result"]["reason"]


def _record_with_uncertain_terminal_gate(spec: AppTestCaseSpec) -> ExecutionRecord:
    record = _scripted_record(spec)
    uncertain = replace(
        record.step_results[0],
        evidence={
            **dict(record.step_results[0].evidence),
            "gate_decision": "INCONCLUSIVE",
            "target_evidence": "UNKNOWN",
        },
    )
    return replace(record, step_results=(uncertain, *record.step_results[1:]))


def test_flow_vlm_fallback_accepts_inconclusive_terminal_flow(monkeypatch):
    spec = load_test_case(CASE)
    monkeypatch.setenv("APP_TEST_ENABLE_FLOW_VLM", "1")
    monkeypatch.setattr(
        execution_verifier,
        "_model_flow_verification",
        lambda test_case, execution, contract: {
            "decision": "CONFORMANT",
            "confidence": 0.92,
            "reason": "post-action frames are consistent with the declared flow",
        },
    )
    report = run_app_test(
        spec,
        ScriptedStepExecutor(_record_with_uncertain_terminal_gate(spec)),
        run_id="flow-vlm-pass",
    )
    assert report["execution_result"]["status"] == "COMPLETED"
    assert report["overall_result"] == OverallResult.APP_PASS
    assert report["execution_result"]["evidence"]["flow_vlm"]["status"] == "CONFORMANT"


def test_flow_vlm_fallback_can_reject_inconclusive_terminal_flow(monkeypatch):
    spec = load_test_case(CASE)
    monkeypatch.setenv("APP_TEST_ENABLE_FLOW_VLM", "1")
    monkeypatch.setattr(
        execution_verifier,
        "_model_flow_verification",
        lambda test_case, execution, contract: {
            "decision": "NONCONFORMANT",
            "confidence": 0.95,
            "failed_step": spec.steps[0].step_id,
            "reason": "the observed page does not support the declared first action",
        },
    )
    report = run_app_test(
        spec,
        ScriptedStepExecutor(_record_with_uncertain_terminal_gate(spec)),
        run_id="flow-vlm-fail",
    )
    assert report["execution_result"]["status"] == "TEST_EXECUTION_FAILED"
    assert report["execution_result"]["failed_step"] == spec.steps[0].step_id
    assert report["app_behavior_result"]["status"] == "NOT_EVALUATED"
    assert report["overall_result"] == OverallResult.TEST_EXECUTION_FAIL


def test_flow_vlm_fallback_is_conservative_on_model_error(monkeypatch):
    spec = load_test_case(CASE)
    monkeypatch.setenv("APP_TEST_ENABLE_FLOW_VLM", "1")

    def fail_flow_vlm(test_case, execution, contract):
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(execution_verifier, "_model_flow_verification", fail_flow_vlm)
    report = run_app_test(
        spec,
        ScriptedStepExecutor(_record_with_uncertain_terminal_gate(spec)),
        run_id="flow-vlm-error",
    )
    assert report["execution_result"]["status"] == "INCONCLUSIVE"
    assert report["overall_result"] == OverallResult.INCONCLUSIVE
    flow_vlm = report["execution_result"]["evidence"]["flow_vlm"]
    assert flow_vlm["status"] == "ERROR"
    assert "model unavailable" in flow_vlm["error"]


def test_flow_vlm_fallback_does_not_override_deterministic_flow_failure(monkeypatch):
    spec = load_test_case(CASE)
    record = _scripted_record(spec)
    mismatched = replace(record.step_results[0], action_type="INPUT")
    record = replace(record, step_results=(mismatched, *record.step_results[1:]))
    calls = {"count": 0}
    monkeypatch.setenv("APP_TEST_ENABLE_FLOW_VLM", "1")

    def unexpected_flow_vlm(test_case, execution, contract):
        calls["count"] += 1
        return {"decision": "CONFORMANT", "confidence": 1.0}

    monkeypatch.setattr(execution_verifier, "_model_flow_verification", unexpected_flow_vlm)
    report = run_app_test(
        spec,
        ScriptedStepExecutor(record),
        run_id="flow-vlm-hard-failure",
    )
    assert calls["count"] == 0
    assert report["overall_result"] == OverallResult.TEST_EXECUTION_FAIL


def test_legacy_visual_checker_can_resolve_unknown_app_text_evidence(tmp_path):
    payload = _case_payload()
    payload["test_data"]["post_content"] = "app_test_legacy_visual"
    payload["expected_results"] = [payload["expected_results"][0]]
    payload["expected_results"][0]["surface"] = "own_note_list"
    spec = AppTestCaseSpec.from_json(payload)
    trace_dir = tmp_path / "legacy_trace"
    trace_dir.mkdir()
    for frame_id in (0, 1):
        Image.new("RGB", (1080, 800), "white").save(
            trace_dir / f"{frame_id}.jpg", format="JPEG"
        )
    (trace_dir / "0.xml").write_text(
        '<hierarchy><node text="Feed" /></hierarchy>', encoding="utf-8"
    )
    (trace_dir / "1.xml").write_text(
        '<hierarchy><node text="我" /><node text="笔记" />'
        '<node text="app_test_legacy_visual" /></hierarchy>',
        encoding="utf-8",
    )
    (trace_dir / "actions.json").write_text(
        json.dumps(
            {
                "actions": [
                    {
                        "action_index": 1,
                        "type": "click",
                        "target_element": "publish",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    record = _scripted_record(
        spec,
        visible_texts=("Feed",),
        initial_texts=("Feed",),
        after_submit_texts=("Feed",),
        evidence_sufficient=False,
    )
    record = replace(record, raw_trace_dir=str(trace_dir))
    report = run_app_test(
        spec,
        ScriptedStepExecutor(record),
        run_id="legacy-visual-resolve",
    )
    assertion = report["business_offline_review"]["assertion_reviews"][0]
    legacy = assertion["evidence"]["verification_benchmark_legacy_checker"]
    assert legacy["status"] == "SATISFIED"
    assert assertion["status"] == "UNKNOWN_EVIDENCE"
    assert report["app_behavior_result"]["status"] == "UNKNOWN_EVIDENCE"
    assert report["overall_result"] == OverallResult.INCONCLUSIVE


def test_legacy_state_checker_is_recorded_as_advisory_state_evidence(tmp_path):
    payload = _case_payload()
    payload["expected_results"] = [
        {
            "assertion_id": "posting_changes_state",
            "type": "STATE_CHANGED",
            "surface": "profile",
        }
    ]
    spec = AppTestCaseSpec.from_json(payload)
    trace_dir = tmp_path / "legacy_state_trace"
    trace_dir.mkdir()
    for frame_id in (0, 1):
        Image.new("RGB", (1080, 800), "white").save(
            trace_dir / f"{frame_id}.jpg", format="JPEG"
        )
    (trace_dir / "0.xml").write_text(
        '<hierarchy><node text="我" selected="false" bounds="[0,0][100,100]" /></hierarchy>',
        encoding="utf-8",
    )
    (trace_dir / "1.xml").write_text(
        '<hierarchy><node text="我" selected="true" bounds="[0,0][100,100]" /></hierarchy>',
        encoding="utf-8",
    )
    (trace_dir / "actions.json").write_text(
        json.dumps({"actions": [{"action_index": 1, "type": "click", "bounds": [0, 0, 100, 100]}]}),
        encoding="utf-8",
    )
    record = replace(
        _scripted_record(spec, visible_texts=("我",), initial_texts=("我",)),
        raw_trace_dir=str(trace_dir),
    )
    review = review_app_test_trace(
        test_case=spec,
        execution=record,
        contract=compile_app_test_contract(spec),
        role=OfflineTraceRole.BUSINESS_EXECUTION,
    )
    legacy = review.assertion_reviews[0].evidence[
        "verification_benchmark_legacy_checker"
    ]
    checker_criteria = legacy["evidence"]["checker_result"]["criteria"]
    state = checker_criteria["state.posting_changes_state"]
    assert state["status"] == "SATISFIED"
    assert legacy["evidence"]["state_evidence"]["status"] == "SATISFIED"


def test_legacy_visual_checker_cannot_bypass_unreached_surface(tmp_path):
    payload = _payload_with_verification_steps()
    payload["test_data"]["post_content"] = "app_test_legacy_surface_guard"
    payload["expected_results"][0]["surface"] = "own_note_list"
    spec = AppTestCaseSpec.from_json(payload)
    trace_dir = tmp_path / "legacy_surface_guard"
    trace_dir.mkdir()
    Image.new("RGB", (1080, 800), "white").save(
        trace_dir / "0.jpg", format="JPEG"
    )
    (trace_dir / "0.xml").write_text(
        '<hierarchy><node text="Feed" /><node text="笔记" />'
        '<node text="app_test_legacy_surface_guard" /></hierarchy>',
        encoding="utf-8",
    )
    (trace_dir / "actions.json").write_text(
        json.dumps({"actions": [{"action_index": 1, "type": "click"}]}),
        encoding="utf-8",
    )
    record = replace(_direct_unknown_record(spec), raw_trace_dir=str(trace_dir))
    review = review_app_test_trace(
        test_case=spec,
        execution=record,
        contract=compile_app_test_contract(spec),
        role=OfflineTraceRole.VERIFICATION_OBSERVATION,
        verification_context={
            "step_results": [
                {
                    "verification_step_id": "navigate_to_surface",
                    "reached_surface": False,
                    "observation_frames": [0],
                }
            ]
        },
    )
    assertion = review.assertion_reviews[0].as_dict()
    assert assertion["evidence"]["source"].startswith("surface_not_reached:")
    assert assertion["status"] == "UNKNOWN_EVIDENCE"


def test_stage5_verification_runner_finds_unique_text_after_direct_unknown():
    payload = _payload_with_verification_steps()
    payload["test_data"]["post_content"] = "app_test_verify_found"
    spec = AppTestCaseSpec.from_json(payload)
    report = run_app_test(
        spec,
        ScriptedStepExecutor(_direct_unknown_record(spec)),
        run_id="verify-found",
        verification_runner=ScriptedVerificationRunner(scenario="found"),
    )
    assert report["direct_app_behavior_result"]["status"] == "UNKNOWN_EVIDENCE"
    assert report["verification_runner_result"]["status"] == "COMPLETED"
    assert report["verification_runner_result"]["used_runner"] is True
    assert report["overall_result"] == OverallResult.APP_PASS


def test_stage8_verification_text_before_surface_does_not_satisfy_surface_assertion():
    payload = _payload_with_verification_steps()
    payload["test_data"]["post_content"] = "app_test_before_surface"
    payload["expected_results"][0]["surface"] = "own_note_list"
    payload["expected_results"][0]["requires_verification_runner"] = True
    spec = AppTestCaseSpec.from_json(payload)
    report = run_app_test(
        spec,
        ScriptedStepExecutor(_direct_unknown_record(spec)),
        run_id="surface-before-only",
        verification_runner=_SurfaceScopedVerificationRunner(
            expected_text="app_test_before_surface",
            text_before_surface=True,
            text_after_surface=False,
        ),
    )
    assert report["verification_runner_result"]["status"] == "COMPLETED"
    assert report["verification_offline_review"]["assertion_reviews"][0]["evidence"][
        "source"
    ] == "surface:own_note_list:from_frame:2"
    replay = report["verification_offline_review"]["assertion_reviews"][0]["evidence"][
        "verification_benchmark_replay_mirror"
    ]
    assert replay["engine"] == "harmony-eval-replay-v1"
    assert replay["matches"] is True
    assert replay["comparisons"] == {
        "criterion_status": True,
        "temporal_semantics": True,
        "first_satisfied_frame": True,
        "last_evaluated_frame": True,
        "evidence_pointers": True,
    }
    assert replay["criterion"]["status"] == "UNKNOWN_EVIDENCE"
    assert isinstance(replay["contract_sha256"], str)
    assert isinstance(replay["event_trace_sha256"], str)
    assert report["overall_result"] == OverallResult.INCONCLUSIVE


def test_stage8_runner_surface_self_report_without_frame_evidence_is_inconclusive():
    payload = _payload_with_verification_steps()
    payload["test_data"]["post_content"] = "app_test_surface_self_report"
    payload["expected_results"][0]["surface"] = "own_note_list"
    payload["expected_results"][0]["requires_verification_runner"] = True
    spec = AppTestCaseSpec.from_json(payload)
    report = run_app_test(
        spec,
        ScriptedStepExecutor(_direct_unknown_record(spec)),
        run_id="surface-self-report",
        verification_runner=_SurfaceScopedVerificationRunner(
            expected_text="app_test_surface_self_report",
            text_before_surface=True,
            text_after_surface=True,
            report_surface_frame=False,
        ),
    )
    assert report["verification_runner_result"]["reached_surface"] is True
    assert report["verification_offline_review"]["assertion_reviews"][0]["evidence"][
        "source"
    ] == "surface_not_reached:own_note_list"
    assert report["overall_result"] == OverallResult.INCONCLUSIVE


def test_stage8_loading_surface_frame_cannot_confirm_expected_text():
    payload = _payload_with_verification_steps()
    payload["test_data"]["post_content"] = "app_test_loading_surface"
    payload["expected_results"][0]["surface"] = "own_note_list"
    payload["expected_results"][0]["requires_verification_runner"] = True
    spec = AppTestCaseSpec.from_json(payload)
    report = run_app_test(
        spec,
        ScriptedStepExecutor(_direct_unknown_record(spec)),
        run_id="surface-loading",
        verification_runner=_SurfaceScopedVerificationRunner(
            expected_text="app_test_loading_surface",
            text_before_surface=False,
            text_after_surface=True,
            after_surface_extra_texts=("loading",),
        ),
    )
    review = report["verification_offline_review"]["assertion_reviews"][0]
    assert review["evidence"]["source"] == "surface_not_reached:own_note_list"
    assert report["overall_result"] == OverallResult.INCONCLUSIVE


def test_stage8_surface_shape_contract_rejects_marker_only_page():
    payload = _payload_with_verification_steps()
    payload["test_data"]["post_content"] = "app_test_shape_marker_only"
    payload["expected_results"][0]["surface"] = "own_note_list"
    payload["expected_results"][0]["requires_verification_runner"] = True
    spec = AppTestCaseSpec.from_json(payload)
    report = run_app_test(
        spec,
        ScriptedStepExecutor(_direct_unknown_record(spec)),
        run_id="surface-shape-marker-only",
        verification_runner=_SurfaceScopedVerificationRunner(
            expected_text="app_test_shape_marker_only",
            text_before_surface=False,
            text_after_surface=True,
            verification_target={
                "surface": "own_note_list",
                "surface_text_candidates": ["笔记"],
                "surface_shape_required": True,
                "surface_shape_text_groups": [["我的", "个人主页"], ["笔记"]],
            },
        ),
    )
    review = report["verification_offline_review"]["assertion_reviews"][0]
    page_state = review["evidence"]["surface_page_states"][1]
    assert review["evidence"]["source"] == "surface_not_reached:own_note_list"
    assert "笔记" in page_state["surface_marker_hits"]
    assert page_state["surface_shape_matched"] is False
    assert report["overall_result"] == OverallResult.INCONCLUSIVE


def test_stage8_surface_shape_contract_accepts_marker_and_page_shape():
    payload = _payload_with_verification_steps()
    payload["test_data"]["post_content"] = "app_test_shape_complete"
    payload["expected_results"][0]["surface"] = "own_note_list"
    payload["expected_results"][0]["requires_verification_runner"] = True
    spec = AppTestCaseSpec.from_json(payload)
    report = run_app_test(
        spec,
        ScriptedStepExecutor(_direct_unknown_record(spec)),
        run_id="surface-shape-complete",
        verification_runner=_SurfaceScopedVerificationRunner(
            expected_text="app_test_shape_complete",
            text_before_surface=False,
            text_after_surface=True,
            after_surface_extra_texts=("我的",),
            verification_target={
                "surface": "own_note_list",
                "surface_text_candidates": ["笔记"],
                "surface_shape_required": True,
                "surface_shape_text_groups": [["我的", "个人主页"], ["笔记"]],
            },
        ),
    )
    review = report["verification_offline_review"]["assertion_reviews"][0]
    assert review["evidence"]["source"] == "surface:own_note_list:from_frame:2"
    assert review["evidence"]["surface_page_states"][1]["surface_shape_matched"] is True
    assert report["overall_result"] == OverallResult.APP_PASS


def test_stage8_conversation_surface_requires_target_contact_context():
    payload = _payload_with_verification_steps()
    payload["test_data"]["post_content"] = "app_test_chat_wrong_contact"
    payload["expected_results"][0]["surface"] = "conversation_with_contact"
    payload["expected_results"][0]["requires_verification_runner"] = True
    spec = AppTestCaseSpec.from_json(payload)
    report = run_app_test(
        spec,
        ScriptedStepExecutor(_direct_unknown_record(spec)),
        run_id="conversation-wrong-contact",
        verification_runner=_SurfaceScopedVerificationRunner(
            expected_text="app_test_chat_wrong_contact",
            text_before_surface=False,
            text_after_surface=True,
            surface_base_texts=("消息", "Bob"),
            verification_target={
                "surface": "conversation_with_contact",
                "surface_text_candidates": ["消息"],
                "surface_shape_required": True,
                "surface_context": {"contact_name": "Alice"},
            },
        ),
    )
    review = report["verification_offline_review"]["assertion_reviews"][0]
    page_state = review["evidence"]["surface_page_states"][1]
    assert review["evidence"]["source"] == "surface_not_reached:conversation_with_contact"
    assert page_state["context_candidates"] == ["Alice"]
    assert page_state["context_matched"] is False
    assert report["overall_result"] == OverallResult.INCONCLUSIVE


def test_stage8_conversation_surface_accepts_target_contact_context():
    payload = _payload_with_verification_steps()
    payload["test_data"]["post_content"] = "app_test_chat_right_contact"
    payload["expected_results"][0]["surface"] = "conversation_with_contact"
    payload["expected_results"][0]["requires_verification_runner"] = True
    spec = AppTestCaseSpec.from_json(payload)
    report = run_app_test(
        spec,
        ScriptedStepExecutor(_direct_unknown_record(spec)),
        run_id="conversation-right-contact",
        verification_runner=_SurfaceScopedVerificationRunner(
            expected_text="app_test_chat_right_contact",
            text_before_surface=False,
            text_after_surface=True,
            surface_base_texts=("消息", "Alice"),
            verification_target={
                "surface": "conversation_with_contact",
                "surface_text_candidates": ["消息"],
                "surface_shape_required": True,
                "surface_context": {"contact_name": "Alice"},
            },
        ),
    )
    review = report["verification_offline_review"]["assertion_reviews"][0]
    assert review["evidence"]["source"] == "surface:conversation_with_contact:from_frame:2"
    assert review["evidence"]["surface_page_states"][1]["context_matched"] is True
    assert report["overall_result"] == OverallResult.APP_PASS


def test_stage8_business_conversation_surface_accepts_fresh_message_result():
    payload = _case_payload()
    expected_text = "hello from the verifier"
    payload["test_data"]["post_content"] = expected_text
    payload["expected_results"][0]["surface"] = "conversation"
    spec = AppTestCaseSpec.from_json(payload)
    record = _scripted_record(
        spec,
        visible_texts=("消息", "Alice", expected_text),
        initial_texts=("Feed", "消息", "Alice"),
        after_submit_texts=("消息", "Alice", expected_text),
    )

    report = run_app_test(
        spec,
        ScriptedStepExecutor(record),
        run_id="business-conversation-fresh-message",
    )

    direct = report["direct_app_behavior_result"]
    assertion = direct["assertion_results"][0]
    assert assertion["status"] == "SATISFIED"
    offline_assertion = report["business_offline_review"]["assertion_reviews"][0]
    assert offline_assertion["evidence"]["source"].startswith(
        "business_surface:conversation:"
    )
    assert report["verification_runner_result"]["status"] == "NOT_RUN"
    assert report["overall_result"] == OverallResult.APP_PASS


def test_stage7_minimal_user_view_auto_verification_intent_finds_result():
    payload = json.loads(MINIMAL_USER_CASE.read_text(encoding="utf-8"))
    payload["metadata"]["verification_runner"] = {
        "scenario": "found",
        "observation_sufficient": True,
    }
    spec = AppTestCaseSpec.from_json(payload).with_runtime_context(run_id="auto-verify")
    record = _direct_unknown_record(spec)
    report = run_app_test(
        spec,
        ScriptedStepExecutor(record),
        run_id="auto-verify",
    )
    assert spec.verification_steps == ()
    assert report["direct_app_behavior_result"]["status"] == "UNKNOWN_EVIDENCE"
    assert report["verification_runner_result"]["status"] == "COMPLETED"
    assert report["verification_runner_result"]["used_runner"] is True
    assert report["verification_runner_result"]["metadata"]["generated_verification_steps"] is True
    intent = report["verification_runner_result"]["metadata"]["verification_intent"]
    assert intent["target_surface"] == "可以在个人主页看到本轮发布的测试内容"
    assert intent["expected_texts"] == ["app_test_auto-verify"]
    assert report["overall_result"] == OverallResult.APP_PASS


def test_stage7_minimal_user_view_auto_verification_absence_can_fail_app():
    payload = json.loads(MINIMAL_USER_CASE.read_text(encoding="utf-8"))
    payload["metadata"]["verification_runner"] = {
        "scenario": "not_found",
        "visible_texts": ["Feed", "Other post"],
        "observation_sufficient": True,
    }
    spec = AppTestCaseSpec.from_json(payload).with_runtime_context(run_id="auto-verify-missing")
    report = run_app_test(
        spec,
        ScriptedStepExecutor(_direct_unknown_record(spec)),
        run_id="auto-verify-missing",
    )
    assert report["verification_runner_result"]["metadata"]["generated_verification_steps"] is True
    assert report["overall_result"] == OverallResult.APP_FAIL


def test_stage7_auto_verification_intent_requires_observable_goal():
    payload = _case_payload()
    payload.pop("verification_steps", None)
    payload["expected_results"] = [
        {
            "assertion_id": "state_changed",
            "type": "STATE_CHANGED",
        }
    ]
    spec = AppTestCaseSpec.from_json(payload)
    intent = compile_verification_intent(spec)
    assert intent.has_observable_goal is False
    assert intent.generated_steps == ()
    assert effective_verification_steps(spec) == ()


def test_stage5_verification_runner_sufficient_absence_can_fail_app():
    payload = _payload_with_verification_steps()
    payload["test_data"]["post_content"] = "app_test_verify_missing"
    spec = AppTestCaseSpec.from_json(payload)
    report = run_app_test(
        spec,
        ScriptedStepExecutor(_direct_unknown_record(spec)),
        run_id="verify-missing",
        verification_runner=ScriptedVerificationRunner(
            scenario="not_found",
            visible_texts=("Feed", "Other post"),
            observation_sufficient=True,
        ),
    )
    assert report["verification_runner_result"]["reached_surface"] is True
    assert report["verification_runner_result"]["observation_sufficient"] is True
    assert report["overall_result"] == OverallResult.APP_FAIL


def test_stage5_verification_runner_route_failure_is_inconclusive_not_app_fail():
    payload = _payload_with_verification_steps()
    spec = AppTestCaseSpec.from_json(payload)
    report = run_app_test(
        spec,
        ScriptedStepExecutor(_direct_unknown_record(spec)),
        run_id="verify-route-failed",
        verification_runner=ScriptedVerificationRunner(scenario="route_failed"),
    )
    assert report["verification_runner_result"]["status"] == "ROUTE_FAILED"
    assert report["overall_result"] == OverallResult.INCONCLUSIVE


def test_stage5_verification_runner_env_blocked_maps_to_env_blocked():
    payload = _payload_with_verification_steps()
    spec = AppTestCaseSpec.from_json(payload)
    report = run_app_test(
        spec,
        ScriptedStepExecutor(_direct_unknown_record(spec)),
        run_id="verify-env-blocked",
        verification_runner=ScriptedVerificationRunner(scenario="env_blocked"),
    )
    assert report["verification_runner_result"]["status"] == "ENV_BLOCKED"
    assert report["overall_result"] == OverallResult.ENV_BLOCKED


def test_stage5_verification_runner_rejects_write_action():
    payload = _payload_with_verification_steps()
    payload["verification_steps"][0]["action_type"] = "INPUT"
    spec = AppTestCaseSpec.from_json(payload)
    report = run_app_test(
        spec,
        ScriptedStepExecutor(_direct_unknown_record(spec)),
        run_id="verify-write-action",
    )
    assert report["verification_runner_result"]["status"] == "UNSUPPORTED"
    assert report["overall_result"] == OverallResult.UNSUPPORTED


def test_stage5_execution_fail_does_not_start_verification_runner():
    payload = _payload_with_verification_steps()
    spec = AppTestCaseSpec.from_json(payload)
    report = run_app_test(
        spec,
        MockStepExecutor(scenario="execution_fail"),
        run_id="verify-exec-fail",
        verification_runner=ScriptedVerificationRunner(scenario="found"),
    )
    assert report["overall_result"] == OverallResult.TEST_EXECUTION_FAIL
    assert report["verification_runner_result"]["status"] == "NOT_RUN"
    assert report["verification_runner_result"]["used_runner"] is False


def test_stage5_execution_failure_with_old_text_does_not_run_app_oracle():
    spec = load_test_case(CASE)
    first = completed_step(spec.steps[0], spec, 0)
    failed = completed_step(spec.steps[1], spec, 1)
    failed = type(failed)(
        **{**failed.as_dict(), "status": "STEP_FAILED", "error": "target missing"}
    )
    record = ExecutionRecord(
        test_case_id=spec.test_case_id,
        executor="script-fixture",
        step_results=(first, failed),
        final_state=EvidenceState(
            visible_texts=("Feed", "hello test 123"),
            state_changed=True,
            evidence_sufficient=True,
        ),
        metadata={
            "initial_visible_texts": ["Feed", "hello test 123"],
            "frames": [_frame(0, ("Feed", "hello test 123"), 0)],
            "frame_visible_texts": {"0": ["Feed", "hello test 123"]},
        },
    )
    report = run_app_test(spec, ScriptedStepExecutor(record), run_id="exec-old")
    assert report["overall_result"] == OverallResult.TEST_EXECUTION_FAIL
    assert report["app_behavior_result"]["status"] == "NOT_EVALUATED"


def test_stage5_observation_policy_can_make_after_step_evidence_insufficient():
    payload = _case_payload()
    payload["observation_policy"] = {
        "immediate": True,
        "delays_ms": [50],
        "max_wait_ms": 100,
        "stop_when_stable": True,
    }
    spec = AppTestCaseSpec.from_json(payload)
    report = run_app_test(spec, MockStepExecutor(scenario="pass"), run_id="policy-short")
    assert report["overall_result"] == OverallResult.INCONCLUSIVE


def test_text_visible_absence_at_immediate_frame_is_not_app_failure():
    spec = load_test_case(CASE)
    record = _scripted_record(
        spec,
        visible_texts=("Feed",),
        after_submit_texts=("Feed",),
    )
    terminal = replace(record.step_results[-1], post_frames=(3,))
    frames = [
        {**frame, "relative_to_action_ms": 0}
        if frame.get("frame_id") == 3
        else frame
        for frame in record.metadata["frames"]
        if frame.get("frame_id") in {0, 1, 2, 3}
    ]
    record = replace(
        record,
        step_results=(*record.step_results[:-1], terminal),
        metadata={
            **dict(record.metadata),
            "frames": frames,
            "frame_visible_texts": {
                key: value
                for key, value in record.metadata["frame_visible_texts"].items()
                if int(key) <= 3
            },
        },
    )

    report = run_app_test(
        spec,
        ScriptedStepExecutor(record),
        run_id="negative-window-immediate-only",
    )

    assertion = report["app_behavior_result"]["assertion_results"][0]
    sufficiency = assertion["evidence"]["negative_observation_sufficiency"]
    assert report["overall_result"] == OverallResult.INCONCLUSIVE
    assert assertion["status"] == "UNKNOWN_EVIDENCE"
    assert sufficiency["sufficient"] is False
    assert sufficiency["observed_offsets_ms"] == [0]
    assert sufficiency["missing_offsets_ms"] == [500, 1000]


def test_text_visible_async_result_at_terminal_delay_is_app_pass():
    spec = load_test_case(CASE)
    expected = str(spec.test_data["post_content"])
    record = _scripted_record(
        spec,
        visible_texts=("Feed", expected),
        after_submit_texts=("Feed",),
    )
    terminal = replace(record.step_results[-1], post_frames=(3, 4, 5))
    frames = [
        frame
        for frame in record.metadata["frames"]
        if frame.get("frame_id") not in {3, 4, 5}
    ] + [
        _frame(3, ("Feed",), 0),
        _frame(4, ("Feed",), 500),
        _frame(5, ("Feed", expected), 1000),
    ]
    frame_texts = {
        **dict(record.metadata["frame_visible_texts"]),
        "3": ["Feed"],
        "4": ["Feed"],
        "5": ["Feed", expected],
    }
    record = replace(
        record,
        step_results=(*record.step_results[:-1], terminal),
        metadata={
            **dict(record.metadata),
            "frames": frames,
            "frame_visible_texts": frame_texts,
        },
    )

    report = run_app_test(
        spec,
        ScriptedStepExecutor(record),
        run_id="async-result-terminal-delay",
    )

    assertion = report["app_behavior_result"]["assertion_results"][0]
    assert report["overall_result"] == OverallResult.APP_PASS
    assert assertion["status"] == "SATISFIED"
    assert assertion["evidence"]["negative_observation_sufficiency"]["sufficient"] is True


@pytest.mark.parametrize(
    ("terminal_texts", "terminal_stability"),
    [
        (("Feed", "loading"), "STABLE_LOADING"),
        (("Feed", "permission_dialog"), "DEGRADED"),
    ],
)
def test_text_visible_absence_with_terminal_blocker_is_inconclusive(
    terminal_texts: tuple[str, ...],
    terminal_stability: str,
):
    spec = load_test_case(CASE)
    record = _scripted_record(
        spec,
        visible_texts=terminal_texts,
        after_submit_texts=("Feed",),
    )
    terminal = replace(record.step_results[-1], post_frames=(3, 4, 5))
    frames = [
        frame
        for frame in record.metadata["frames"]
        if frame.get("frame_id") not in {3, 4, 5}
    ] + [
        _frame(3, ("Feed",), 0),
        _frame(4, ("Feed",), 500),
        {
            **_frame(5, terminal_texts, 1000),
            "stability": terminal_stability,
        },
    ]
    frame_texts = {
        **dict(record.metadata["frame_visible_texts"]),
        "3": ["Feed"],
        "4": ["Feed"],
        "5": list(terminal_texts),
    }
    record = replace(
        record,
        step_results=(*record.step_results[:-1], terminal),
        metadata={
            **dict(record.metadata),
            "frames": frames,
            "frame_visible_texts": frame_texts,
        },
    )

    report = run_app_test(
        spec,
        ScriptedStepExecutor(record),
        run_id=f"terminal-blocker-{terminal_stability.casefold()}",
    )

    review = report["business_offline_review"]["assertion_reviews"][0]
    assertion = report["app_behavior_result"]["assertion_results"][0]
    assert report["overall_result"] == OverallResult.INCONCLUSIVE
    assert review["status"] == "UNKNOWN_EVIDENCE"
    assert assertion["status"] == "UNKNOWN_EVIDENCE"
    assert assertion["evidence"]["negative_observation_sufficiency"][
        "sufficient"
    ] is False


def test_text_absent_requires_complete_delayed_observation_window():
    payload = _case_payload()
    payload["expected_results"] = [
        {
            "assertion_id": "error_absent",
            "type": "TEXT_ABSENT",
            "expected_value": "发布失败",
            "after_step": "submit_post",
        }
    ]
    payload["forbidden_effects"] = []
    spec = AppTestCaseSpec.from_json(payload)
    record = _scripted_record(
        spec,
        visible_texts=("Feed",),
        after_submit_texts=("Feed",),
    )
    terminal = replace(record.step_results[-1], post_frames=(3,))
    frames = [
        {**frame, "relative_to_action_ms": 0}
        if frame.get("frame_id") == 3
        else frame
        for frame in record.metadata["frames"]
        if frame.get("frame_id") in {0, 1, 2, 3}
    ]
    record = replace(
        record,
        step_results=(*record.step_results[:-1], terminal),
        metadata={
            **dict(record.metadata),
            "frames": frames,
            "frame_visible_texts": {
                key: value
                for key, value in record.metadata["frame_visible_texts"].items()
                if int(key) <= 3
            },
        },
    )

    report = run_app_test(
        spec,
        ScriptedStepExecutor(record),
        run_id="text-absent-immediate-only",
    )

    assertion = report["app_behavior_result"]["assertion_results"][0]
    assert report["overall_result"] == OverallResult.INCONCLUSIVE
    assert assertion["status"] == "UNKNOWN_EVIDENCE"
    assert assertion["evidence"]["negative_observation_sufficiency"]["sufficient"] is False


def test_forbidden_text_presence_is_decisive_even_before_window_completion():
    spec = load_test_case(CASE)
    record = _scripted_record(
        spec,
        visible_texts=("Feed", "Pay now"),
        after_submit_texts=("Feed", "Pay now"),
    )
    terminal = replace(record.step_results[-1], post_frames=(3,))
    frames = [
        {**frame, "relative_to_action_ms": 0}
        if frame.get("frame_id") == 3
        else frame
        for frame in record.metadata["frames"]
        if frame.get("frame_id") in {0, 1, 2, 3}
    ]
    record = replace(
        record,
        step_results=(*record.step_results[:-1], terminal),
        metadata={
            **dict(record.metadata),
            "frames": frames,
            "frame_visible_texts": {
                key: value
                for key, value in record.metadata["frame_visible_texts"].items()
                if int(key) <= 3
            },
        },
    )

    report = run_app_test(
        spec,
        ScriptedStepExecutor(record),
        run_id="forbidden-present-immediate",
    )

    forbidden = next(
        item
        for item in report["app_behavior_result"]["assertion_results"]
        if item["assertion_id"] == "no_payment_flow"
    )
    assert forbidden["status"] == "VIOLATED"
    assert report["overall_result"] == OverallResult.APP_FAIL


def test_business_text_on_editor_does_not_satisfy_declared_result_surface():
    payload = _case_payload()
    payload["expected_results"] = [payload["expected_results"][0]]
    payload["expected_results"][0]["surface"] = "result_list"
    payload["forbidden_effects"] = []
    spec = AppTestCaseSpec.from_json(payload)
    expected = str(spec.test_data["post_content"])
    record = _scripted_record(
        spec,
        visible_texts=("Editor", "EditText", expected, "Publish"),
        after_submit_texts=("Editor", "EditText", expected, "Publish"),
    )

    report = run_app_test(
        spec,
        ScriptedStepExecutor(record),
        run_id="business-wrong-editor-surface",
    )

    review = report["business_offline_review"]["assertion_reviews"][0]
    assert report["overall_result"] == OverallResult.INCONCLUSIVE
    assert review["status"] == "UNKNOWN_EVIDENCE"
    assert review["evidence"]["source"] == "surface_not_reached:result_list"


def test_business_text_on_declared_feed_surface_remains_app_pass():
    payload = _case_payload()
    payload["expected_results"] = [payload["expected_results"][0]]
    payload["forbidden_effects"] = []
    spec = AppTestCaseSpec.from_json(payload)
    expected = str(spec.test_data["post_content"])
    record = _scripted_record(
        spec,
        visible_texts=("Feed", expected),
        after_submit_texts=("Feed", expected),
    )

    report = run_app_test(
        spec,
        ScriptedStepExecutor(record),
        run_id="business-declared-feed-surface",
    )

    review = report["business_offline_review"]["assertion_reviews"][0]
    assert report["overall_result"] == OverallResult.APP_PASS
    assert review["status"] == "SATISFIED"
    assert review["evidence"]["source"].startswith(
        "business_surface:feed_or_post_detail:"
    )


def test_stage5_observation_policy_accepts_matching_after_step_frame():
    payload = _case_payload()
    payload["observation_policy"] = {
        "immediate": True,
        "delays_ms": [500],
        "max_wait_ms": 600,
        "stop_when_stable": True,
    }
    spec = AppTestCaseSpec.from_json(payload)
    report = run_app_test(spec, MockStepExecutor(scenario="pass"), run_id="policy-ok")
    assert report["overall_result"] == OverallResult.APP_PASS


def test_stage5_precondition_login_failure_is_env_blocked():
    spec = load_test_case(CASE)
    record = _scripted_record(spec, initial_texts=("Please log in",), after_submit_texts=("Feed", "hello test 123"))
    report = run_app_test(spec, ScriptedStepExecutor(record), run_id="pre-login")
    assert report["overall_result"] == OverallResult.ENV_BLOCKED
    assert report["app_behavior_result"]["status"] == "NOT_EVALUATED"


def test_stage5_precondition_wrong_page_is_execution_failure():
    spec = load_test_case(CASE)
    record = _scripted_record(spec, initial_texts=("Home",), after_submit_texts=("Feed", "hello test 123"))
    report = run_app_test(spec, ScriptedStepExecutor(record), run_id="pre-page")
    assert report["overall_result"] == OverallResult.TEST_EXECUTION_FAIL
    assert report["app_behavior_result"]["status"] == "NOT_EVALUATED"


def test_stage5_precondition_satisfied_allows_app_oracle():
    spec = load_test_case(CASE)
    report = run_app_test(spec, ScriptedStepExecutor(_scripted_record(spec)), run_id="pre-ok")
    assert report["overall_result"] == OverallResult.APP_PASS


def test_stage5_optional_unsupported_assertion_does_not_block_app_pass():
    payload = _case_payload()
    payload["expected_results"].append(
        {
            "assertion_id": "optional_image_match",
            "type": "IMAGE_MATCH",
            "expected_value": "whatever",
            "required": False,
        }
    )
    spec = AppTestCaseSpec.from_json(payload)
    report = run_app_test(spec, MockStepExecutor(scenario="pass"), run_id="optional-unsupported")
    assert report["overall_result"] == OverallResult.APP_PASS
    statuses = {
        item["assertion_id"]: item["status"]
        for item in report["app_behavior_result"]["assertion_results"]
    }
    assert statuses["optional_image_match"] == "UNSUPPORTED"


def test_stage5_required_unsupported_assertion_blocks_result():
    payload = _case_payload()
    payload["expected_results"] = [
        {
            "assertion_id": "required_image_match",
            "type": "IMAGE_MATCH",
            "expected_value": "whatever",
            "required": True,
        }
    ]
    spec = AppTestCaseSpec.from_json(payload)
    report = run_app_test(spec, MockStepExecutor(scenario="pass"), run_id="required-unsupported")
    assert report["overall_result"] == OverallResult.UNSUPPORTED


def test_stage5_manifest_rejects_contract_hash_mismatch(tmp_path):
    spec = load_test_case(CASE)
    report = run_app_test(
        spec,
        MockStepExecutor(scenario="pass"),
        output_dir=tmp_path,
        run_id="contract-hash-ok",
    )
    manifest_path = tmp_path / "test_execution_manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["contract_sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    try:
        load_execution_manifest(manifest_path, spec, report["contract_sha256"])
    except ManifestIntakeError as exc:
        assert "contract_sha256" in str(exc)
    else:
        raise AssertionError("manifest with mismatched contract_sha256 should fail")


def test_stage5_verification_benchmark_manifest_intake_loads_app_test_evidence(tmp_path):
    spec = load_test_case(CASE)
    report = run_app_test(
        spec,
        MockStepExecutor(scenario="pass"),
        output_dir=tmp_path,
        run_id="benchmark-intake",
    )
    intake = load_app_test_manifest_evidence(
        test_case=spec,
        manifest_path=tmp_path / "test_execution_manifest.json",
    )
    assert intake.contract.sha256 == report["contract_sha256"]
    assert intake.execution_record.metadata["contract_sha256"] == report["contract_sha256"]
    assert intake.manifest.as_dict()["contract_sha256"] == report["contract_sha256"]


def _legacy_adaptive_capture_manifest_fixture(tmp_path: Path) -> tuple[Path, Path]:
    """Freeze the exact schema drift present in the retained XHS traces."""

    spec = load_test_case(CASE)
    run_app_test(
        spec,
        MockStepExecutor(scenario="pass"),
        output_dir=tmp_path,
        run_id="legacy-adaptive-capture",
    )
    test_case_path = tmp_path / "test_case.normalized.json"
    manifest_path = tmp_path / "test_execution_manifest.json"
    contract_path = tmp_path / "app_test_contract.json"

    test_case_payload = json.loads(test_case_path.read_text(encoding="utf-8"))
    test_case_payload["observation_policy"].pop("adaptive_capture")
    legacy_test_case_sha256 = canonical_sha256(test_case_payload)
    test_case_path.write_text(
        json.dumps(test_case_payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )

    contract_payload = json.loads(contract_path.read_text(encoding="utf-8"))
    contract_payload.pop("contract_sha256")
    contract_payload["test_case_sha256"] = legacy_test_case_sha256
    contract_payload["observation_policy"].pop("adaptive_capture")
    oracle = contract_payload["app_oracle_contract"]
    oracle["forbidden_effects_ignored_v1"] = oracle.pop("forbidden_effects")
    oracle.pop("forbidden_effects_are_required_absence_constraints")
    legacy_contract_sha256 = canonical_sha256(contract_payload)
    contract_payload["contract_sha256"] = legacy_contract_sha256
    contract_path.write_text(
        json.dumps(contract_payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )

    manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_payload["test_case_sha256"] = legacy_test_case_sha256
    manifest_payload["contract_sha256"] = legacy_contract_sha256
    manifest_path.write_text(
        json.dumps(manifest_payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return test_case_path, manifest_path


def test_manifest_intake_migrates_known_legacy_schema_drift_with_receipt(tmp_path):
    test_case_path, manifest_path = _legacy_adaptive_capture_manifest_fixture(tmp_path)
    current_spec = load_test_case(test_case_path)

    intake = load_app_test_manifest_evidence(
        test_case=current_spec,
        test_case_path=test_case_path,
        manifest_path=manifest_path,
    )

    migration = intake.compatibility_migration
    assert migration is not None
    assert migration["status"] == "MIGRATED"
    assert migration["migration_ids"] == [
        "test_case.observation_policy.adaptive_capture_default_false",
        "contract.app_oracle.forbidden_effects_v1",
    ]
    assert migration["legacy_test_case_sha256"] == intake.manifest.test_case_sha256
    assert migration["legacy_contract_sha256"] == intake.manifest.contract_sha256
    assert intake.execution_record.metadata["manifest_compatibility_migration"] == migration


def test_manifest_intake_rejects_legacy_binding_without_source_test_case(tmp_path):
    test_case_path, manifest_path = _legacy_adaptive_capture_manifest_fixture(tmp_path)
    current_spec = load_test_case(test_case_path)

    with pytest.raises(ManifestIntakeError, match="test_case_sha256"):
        load_app_test_manifest_evidence(
            test_case=current_spec,
            manifest_path=manifest_path,
        )


def test_manifest_intake_rejects_unregistered_legacy_contract_change(tmp_path):
    test_case_path, manifest_path = _legacy_adaptive_capture_manifest_fixture(tmp_path)
    current_spec = load_test_case(test_case_path)
    contract_path = tmp_path / "app_test_contract.json"
    contract_payload = json.loads(contract_path.read_text(encoding="utf-8"))
    contract_payload.pop("contract_sha256")
    contract_payload["execution_contract"]["runner_constraints"][
        "preserve_step_order"
    ] = False
    tampered_contract_sha256 = canonical_sha256(contract_payload)
    contract_payload["contract_sha256"] = tampered_contract_sha256
    contract_path.write_text(
        json.dumps(contract_payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_payload["contract_sha256"] = tampered_contract_sha256
    manifest_path.write_text(
        json.dumps(manifest_payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ManifestIntakeError, match="registered compatibility migration"):
        load_app_test_manifest_evidence(
            test_case=current_spec,
            test_case_path=test_case_path,
            manifest_path=manifest_path,
        )


def test_stage1_structured_preconditions_and_forbidden_effects_normalize():
    spec = load_test_case(CASE)
    assert spec.preconditions[0].condition_id == "logged_in"
    assert spec.preconditions[0].failure_class == "ENV_BLOCKED"
    assert spec.forbidden_effects[0].assertion_id == "no_payment_flow"
    assert spec.expected_results[0].after_step == "submit_post"
    normalized = spec.as_dict()
    assert normalized["preconditions"][0]["type"] == "TEXT_ABSENT"
    assert normalized["forbidden_effects"][0]["type"] == "TEXT_ABSENT"


def test_stage1_rejects_unsupported_action_type():
    payload = _case_payload()
    payload["steps"][0]["action_type"] = "SWIPE"
    try:
        AppTestCaseSpec.from_json(payload)
    except AppTestCaseError as exc:
        assert "unsupported" in str(exc)
    else:
        raise AssertionError("unsupported action type should be rejected")


def test_stage1_keeps_unsupported_expected_assertion_for_verifier():
    payload = _case_payload()
    payload["expected_results"][0]["type"] = "IMAGE_MATCH"
    spec = AppTestCaseSpec.from_json(payload)
    assert spec.expected_results[0].type == "IMAGE_MATCH"


def test_stage1_rejects_missing_test_data_reference():
    payload = _case_payload()
    payload["steps"][1]["value_ref"] = "missing_key"
    try:
        AppTestCaseSpec.from_json(payload)
    except AppTestCaseError as exc:
        assert "missing test_data key" in str(exc)
    else:
        raise AssertionError("missing test data reference should be rejected")


def test_stage1_rejects_unknown_after_step():
    payload = _case_payload()
    payload["expected_results"][0]["after_step"] = "missing_step"
    try:
        AppTestCaseSpec.from_json(payload)
    except AppTestCaseError as exc:
        assert "unknown after_step" in str(exc)
    else:
        raise AssertionError("unknown after_step should be rejected")


def test_stage7_input_without_value_generates_runtime_text():
    payload = _case_payload()
    payload["test_data"] = {}
    payload["steps"][1].pop("value_ref")
    payload["expected_results"] = ["可以看到本轮测试内容"]
    spec = AppTestCaseSpec.from_json(payload)
    assert spec.steps[1].action_type == "INPUT"
    assert spec.steps[1].value_ref == "__generated_post_content"
    runtime = spec.with_runtime_context(run_id="input-free")
    assert runtime.test_data["__generated_post_content"] == "app_test_input-free_post_content"
    contract = compile_app_test_contract(runtime)
    assert contract.execution_contract["steps"][1]["expected_value"] == "app_test_input-free_post_content"
