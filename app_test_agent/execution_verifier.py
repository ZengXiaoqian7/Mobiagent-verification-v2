"""Execution conformance verifier for App tests."""

from __future__ import annotations

import base64
import os
from pathlib import Path
from typing import Mapping

from .contract import AppTestContract
from .evidence import ExecutionEvidence, text_contains
from .executor import ExecutionRecord, StepStatus
from .model_client import (
    extract_json_object,
    model_config_from_env,
    post_chat_completion,
)
from .result_types import ExecutionConformanceResult, ExecutionStatus
from .schema import TestCaseSpec


FLOW_VLM_FALLBACK_MARKER = "flow evidence insufficient:"


def verify_execution_conformance(
    test_case: TestCaseSpec,
    execution: ExecutionRecord,
    contract: AppTestContract,
) -> ExecutionConformanceResult:
    precondition_result = _verify_preconditions(test_case, execution, contract)
    if precondition_result is not None:
        return precondition_result
    expected_ids = list(contract.execution_contract["step_order"])
    actual_ids = [item.step_id for item in execution.step_results]
    for result in execution.step_results:
        if result.status == StepStatus.ENV_BLOCKED:
            return ExecutionConformanceResult(
                status=ExecutionStatus.ENV_BLOCKED,
                failed_step=result.step_id,
                environment_blocker=result.blocker or result.error,
                reason="environment blocked the test before all steps could run",
                contract_sha256=contract.sha256,
            )
        if result.status == StepStatus.UNSUPPORTED:
            return ExecutionConformanceResult(
                status=ExecutionStatus.UNSUPPORTED,
                failed_step=result.step_id,
                reason=result.error or "executor does not support this test case",
                evidence_sufficient=False,
                contract_sha256=contract.sha256,
            )
        if result.status == StepStatus.INCONCLUSIVE:
            return ExecutionConformanceResult(
                status=ExecutionStatus.INCONCLUSIVE,
                failed_step=result.step_id,
                reason=result.error or "step gate evidence was insufficient to continue safely",
                evidence_sufficient=False,
                contract_sha256=contract.sha256,
            )
        if result.status != StepStatus.STEP_COMPLETED:
            return ExecutionConformanceResult(
                status=ExecutionStatus.TEST_EXECUTION_FAILED,
                failed_step=result.step_id,
                reason=result.error or "test step did not complete",
                contract_sha256=contract.sha256,
            )
    if actual_ids != expected_ids:
        return ExecutionConformanceResult(
            status=ExecutionStatus.TEST_EXECUTION_FAILED,
            failed_step=_first_mismatch(expected_ids, actual_ids),
            reason=f"step order/count mismatch; expected={expected_ids}, actual={actual_ids}",
            contract_sha256=contract.sha256,
        )
    by_id = {item.step_id: item for item in execution.step_results}
    for step_contract in contract.execution_contract["steps"]:
        result = by_id[step_contract["step_id"]]
        expected_value = step_contract.get("expected_value")
        if expected_value is not None and result.resolved_value != expected_value:
            return ExecutionConformanceResult(
                status=ExecutionStatus.TEST_EXECUTION_FAILED,
                failed_step=step_contract["step_id"],
                reason="input value used by executor does not match test data",
                contract_sha256=contract.sha256,
            )
    flow_failure = verify_execution_flow(test_case, execution, contract)
    if flow_failure is not None:
        if flow_failure.reason.startswith(FLOW_VLM_FALLBACK_MARKER):
            return _apply_flow_vlm_fallback(
                test_case,
                execution,
                contract,
                flow_failure,
            )
        return flow_failure
    return ExecutionConformanceResult(
        status=ExecutionStatus.COMPLETED,
        evidence_sufficient=execution.final_state.evidence_sufficient,
        reason="all declared steps completed in order and passed terminal flow checks",
        contract_sha256=contract.sha256,
    )


def verify_execution_flow(
    test_case: TestCaseSpec,
    execution: ExecutionRecord,
    contract: AppTestContract,
) -> ExecutionConformanceResult | None:
    """Check final process evidence after the step executor has stopped.

    This is deliberately deterministic and small: the flow verifier confirms
    that completed records still agree with the frozen step contract, that
    every completed step has an observation boundary, and that a goal step did
    not finish in an unfinished state.  It never evaluates App success.
    """

    del test_case
    by_id = {item.step_id: item for item in execution.step_results}
    for step_contract in contract.execution_contract["steps"]:
        step_id = step_contract["step_id"]
        result = by_id[step_id]
        if result.action_type != step_contract["action_type"]:
            return _flow_failure(
                contract,
                step_id,
                f"completed step action_type does not match contract: "
                f"{result.action_type} != {step_contract['action_type']}",
            )
        maximum_attempts = int(step_contract["max_retries"]) + 1
        if result.attempts < 1 or result.attempts > maximum_attempts:
            return _flow_failure(
                contract,
                step_id,
                f"attempt count is outside the contract budget: "
                f"{result.attempts} > {maximum_attempts}",
            )
        if not result.post_frames:
            return _flow_failure(
                contract,
                step_id,
                "completed step has no post-observation frame",
            )

        evidence = result.evidence
        if not isinstance(evidence, Mapping):
            evidence = {}
        gate_decision = evidence.get("gate_decision")
        if gate_decision in {"ENV_BLOCKED", "INCONCLUSIVE"}:
            return ExecutionConformanceResult(
                status=(
                    ExecutionStatus.ENV_BLOCKED
                    if gate_decision == "ENV_BLOCKED"
                    else ExecutionStatus.INCONCLUSIVE
                ),
                failed_step=step_id,
                environment_blocker=(
                    str(evidence.get("environment_signal"))
                    if gate_decision == "ENV_BLOCKED"
                    and evidence.get("environment_signal")
                    else None
                ),
                evidence_sufficient=False,
                reason=f"{FLOW_VLM_FALLBACK_MARKER} terminal Step Gate decision was {gate_decision}",
                contract_sha256=contract.sha256,
            )
        if gate_decision in {"TEST_EXECUTION_FAIL", "RETRY"}:
            return _flow_failure(
                contract,
                step_id,
                f"terminal Step Gate decision was {gate_decision}",
            )

        if step_contract["step_mode"] == "GOAL":
            goal_state = evidence.get("goal_state")
            goal_completed = evidence.get("goal_completed")
            model_decisions = evidence.get("model_decisions")
            done_emitted = _done_was_emitted(model_decisions)
            if not isinstance(goal_state, Mapping):
                return _flow_uncertain(
                    contract,
                    step_id,
                    "completed GOAL step is missing goal_state evidence",
                )
            if goal_state.get("status") != "COMPLETED" or goal_state.get("completed") is not True:
                return _flow_failure(
                    contract,
                    step_id,
                    "completed GOAL step ended without a confirmed goal terminal state",
                )
            if goal_completed is False:
                return _flow_failure(
                    contract,
                    step_id,
                    "completed GOAL step reports goal_completed=false",
                )
            if done_emitted and goal_state.get("completed") is not True:
                return _flow_failure(
                    contract,
                    step_id,
                    "Runner emitted done before the GOAL terminal state was confirmed",
                )
        elif _done_was_emitted(evidence.get("model_decision")):
            return _flow_failure(
                contract,
                step_id,
                "Runner emitted done for an atomic step instead of dispatching its action",
            )
    return None


def _apply_flow_vlm_fallback(
    test_case: TestCaseSpec,
    execution: ExecutionRecord,
    contract: AppTestContract,
    deterministic_result: ExecutionConformanceResult,
) -> ExecutionConformanceResult:
    if not _flow_vlm_enabled():
        return deterministic_result
    try:
        decision = _model_flow_verification(test_case, execution, contract)
        normalized = _normalize_flow_vlm_decision(decision)
    except Exception as exc:  # noqa: BLE001 - fallback must fail closed.
        return _flow_vlm_result(
            deterministic_result,
            status="ERROR",
            error=f"{type(exc).__name__}: {exc}",
        )
    confidence = _confidence(normalized)
    minimum = _flow_vlm_min_confidence()
    decision_name = str(normalized.get("decision") or "INCONCLUSIVE").upper()
    model_evidence = {
        "enabled": True,
        "status": decision_name,
        "decision": dict(normalized),
        "confidence_threshold": minimum,
    }
    if confidence < minimum:
        model_evidence["status"] = "LOW_CONFIDENCE"
        return _flow_vlm_result(
            deterministic_result,
            status="LOW_CONFIDENCE",
            evidence=model_evidence,
        )
    if decision_name == "CONFORMANT":
        return ExecutionConformanceResult(
            status=ExecutionStatus.COMPLETED,
            evidence_sufficient=True,
            reason="flow evidence was accepted by the VLM fallback",
            contract_sha256=contract.sha256,
            evidence={"flow_vlm": model_evidence},
        )
    if decision_name == "NONCONFORMANT":
        failed_step = normalized.get("failed_step")
        expected_ids = set(contract.execution_contract["step_order"])
        if not isinstance(failed_step, str) or failed_step not in expected_ids:
            failed_step = deterministic_result.failed_step
        return ExecutionConformanceResult(
            status=ExecutionStatus.TEST_EXECUTION_FAILED,
            failed_step=failed_step,
            evidence_sufficient=False,
            reason=str(normalized.get("reason") or "VLM found the business flow nonconformant"),
            contract_sha256=contract.sha256,
            evidence={"flow_vlm": model_evidence},
        )
    return _flow_vlm_result(
        deterministic_result,
        status="INCONCLUSIVE",
        evidence=model_evidence,
    )


def _flow_vlm_result(
    deterministic_result: ExecutionConformanceResult,
    *,
    status: str,
    error: str | None = None,
    evidence: Mapping[str, object] | None = None,
) -> ExecutionConformanceResult:
    flow_evidence = dict(evidence or {})
    if error is not None:
        flow_evidence = {
            "enabled": True,
            "status": status,
            "error": error,
        }
    return ExecutionConformanceResult(
        status=deterministic_result.status,
        failed_step=deterministic_result.failed_step,
        environment_blocker=deterministic_result.environment_blocker,
        evidence_sufficient=deterministic_result.evidence_sufficient,
        reason=(
            f"{deterministic_result.reason}; flow VLM fallback {status.lower()}"
        ),
        contract_sha256=deterministic_result.contract_sha256,
        evidence={"flow_vlm": flow_evidence},
    )


def _flow_uncertain(
    contract: AppTestContract,
    step_id: str,
    reason: str,
) -> ExecutionConformanceResult:
    return ExecutionConformanceResult(
        status=ExecutionStatus.INCONCLUSIVE,
        failed_step=step_id,
        reason=f"{FLOW_VLM_FALLBACK_MARKER} {reason}",
        evidence_sufficient=False,
        contract_sha256=contract.sha256,
    )


def _flow_vlm_enabled() -> bool:
    value = os.getenv("APP_TEST_ENABLE_FLOW_VLM", "").strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    return bool(os.getenv("APP_TEST_FLOW_VERIFIER_BASE_URL"))


def _flow_vlm_min_confidence() -> float:
    try:
        value = float(os.getenv("APP_TEST_FLOW_VLM_MIN_CONFIDENCE", "0.7"))
    except ValueError:
        return 0.7
    return max(0.0, min(1.0, value))


def _confidence(decision: Mapping[str, object]) -> float:
    try:
        return float(decision.get("confidence") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _normalize_flow_vlm_decision(value: Mapping[str, object]) -> dict[str, object]:
    decision = str(value.get("decision") or "INCONCLUSIVE").upper()
    if decision not in {"CONFORMANT", "NONCONFORMANT", "INCONCLUSIVE"}:
        decision = "INCONCLUSIVE"
    return {
        "decision": decision,
        "confidence": _confidence(value),
        "reason": str(value.get("reason") or ""),
        "failed_step": value.get("failed_step"),
    }


def _model_flow_verification(
    test_case: TestCaseSpec,
    execution: ExecutionRecord,
    contract: AppTestContract,
) -> Mapping[str, object]:
    config = model_config_from_env(
        base_url_names=(
            "APP_TEST_FLOW_VERIFIER_BASE_URL",
            "APP_TEST_VERIFIER_BASE_URL",
            "MOBIAGENT_VERIFIER_BASE_URL",
            "MOBIAGENT_BASE_URL",
        ),
        model_names=(
            "APP_TEST_FLOW_VERIFIER_MODEL",
            "APP_TEST_VERIFIER_MODEL",
            "MOBIAGENT_VERIFIER_MODEL",
            "MOBIAGENT_MODEL",
        ),
    )
    content: list[Mapping[str, object]] = [
        {
            "type": "text",
            "text": _flow_vlm_prompt(test_case, execution, contract),
        }
    ]
    for path in _flow_screenshot_paths(execution):
        suffix = path.suffix.lower()
        mime = "image/png" if suffix == ".png" else "image/jpeg"
        content.append(
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"
                },
            }
        )
    body = post_chat_completion(
        config,
        messages=[{"role": "user", "content": content}],
        max_tokens=256,
    )
    return extract_json_object(body)


def _flow_vlm_prompt(
    test_case: TestCaseSpec,
    execution: ExecutionRecord,
    contract: AppTestContract,
) -> str:
    steps = []
    for step_contract, result in zip(
        contract.execution_contract["steps"], execution.step_results
    ):
        evidence = result.evidence if isinstance(result.evidence, Mapping) else {}
        steps.append(
            {
                "step_id": step_contract["step_id"],
                "instruction": step_contract["instruction"],
                "action_type": step_contract["action_type"],
                "step_mode": step_contract["step_mode"],
                "status": result.status,
                "actual_action_type": result.action_type,
                "attempts": result.attempts,
                "post_frames": list(result.post_frames),
                "gate_decision": evidence.get("gate_decision"),
                "progress_status": evidence.get("progress_status"),
                "target_evidence": evidence.get("target_evidence"),
                "goal_state": evidence.get("goal_state"),
                "model_decision": evidence.get("model_decision"),
                "model_decisions": evidence.get("model_decisions"),
            }
        )
    return (
        "You are a conservative process verifier for a mobile App test. "
        "Judge only whether the Runner followed the declared business steps and "
        "whether any GOAL step ended in a confirmed terminal state. Do not judge "
        "whether the App feature succeeded, and do not use Runner done alone as "
        "success evidence. Use the supplied screenshots only as process evidence. "
        "Return JSON only with decision CONFORMANT, NONCONFORMANT, or INCONCLUSIVE; "
        "confidence 0..1; reason; and optional failed_step.\n"
        f"App: {test_case.app_under_test.name}\n"
        f"Declared steps: {steps}\n"
        "A high-confidence NONCONFORMANT result must identify a concrete mismatch."
    )


def _flow_screenshot_paths(execution: ExecutionRecord) -> tuple[Path, ...]:
    frames = execution.metadata.get("frames")
    if not isinstance(frames, list):
        return ()
    paths: list[Path] = []
    for frame in frames:
        if not isinstance(frame, Mapping):
            continue
        absolute = frame.get("screenshot_abs")
        relative = frame.get("screenshot")
        candidate = Path(absolute) if isinstance(absolute, str) and absolute else None
        if candidate is None and execution.raw_trace_dir and isinstance(relative, str):
            candidate = Path(execution.raw_trace_dir) / relative
        if candidate is not None and candidate.is_file():
            paths.append(candidate)
    return tuple(dict.fromkeys(paths[-6:]))


def _done_was_emitted(value: object) -> bool:
    if isinstance(value, Mapping):
        return str(value.get("action") or "").casefold() == "done"
    if isinstance(value, (list, tuple)):
        return any(_done_was_emitted(item) for item in value)
    return False


def _flow_failure(
    contract: AppTestContract,
    step_id: str,
    reason: str,
) -> ExecutionConformanceResult:
    return ExecutionConformanceResult(
        status=ExecutionStatus.TEST_EXECUTION_FAILED,
        failed_step=step_id,
        reason=reason,
        evidence_sufficient=False,
        contract_sha256=contract.sha256,
    )


def _verify_preconditions(
    test_case: TestCaseSpec,
    execution: ExecutionRecord,
    contract: AppTestContract,
) -> ExecutionConformanceResult | None:
    if not test_case.preconditions:
        return None
    evidence = ExecutionEvidence(execution)
    initial_texts = evidence.initial_texts()
    if not initial_texts:
        env_blocker = next(
            (item for item in execution.step_results if item.status == StepStatus.ENV_BLOCKED),
            None,
        )
        if env_blocker is not None:
            return ExecutionConformanceResult(
                status=ExecutionStatus.ENV_BLOCKED,
                failed_step=env_blocker.step_id,
                environment_blocker=env_blocker.blocker or env_blocker.error,
                evidence_sufficient=False,
                reason="environment blocked the test before initial preconditions could be observed",
                contract_sha256=contract.sha256,
            )
        return ExecutionConformanceResult(
            status=ExecutionStatus.TEST_EXECUTION_FAILED,
            failed_step=None,
            evidence_sufficient=False,
            reason="preconditions could not be evaluated because initial observation evidence is missing",
            contract_sha256=contract.sha256,
        )
    for precondition in test_case.preconditions:
        values = precondition.resolved_values(test_case.test_data)
        if precondition.type == "TEXT_VISIBLE":
            ok = any(text_contains(initial_texts, value) for value in values)
        elif precondition.type == "TEXT_ABSENT":
            ok = not any(text_contains(initial_texts, value) for value in values)
        else:
            return ExecutionConformanceResult(
                status=ExecutionStatus.UNSUPPORTED,
                failed_step=None,
                reason=f"unsupported precondition type: {precondition.type}",
                evidence_sufficient=False,
                contract_sha256=contract.sha256,
            )
        if ok:
            continue
        status = (
            ExecutionStatus.ENV_BLOCKED
            if precondition.failure_class == "ENV_BLOCKED"
            else ExecutionStatus.TEST_EXECUTION_FAILED
        )
        return ExecutionConformanceResult(
            status=status,
            failed_step=None,
            environment_blocker=(
                precondition.condition_id if status == ExecutionStatus.ENV_BLOCKED else None
            ),
            reason=(
                f"precondition {precondition.condition_id} failed: {precondition.type} "
                f"expected {list(values)} in initial evidence"
            ),
            contract_sha256=contract.sha256,
        )
    return None


def _first_mismatch(expected: list[str], actual: list[str]) -> str | None:
    for left, right in zip(expected, actual):
        if left != right:
            return right
    if len(actual) < len(expected):
        return expected[len(actual)]
    if len(actual) > len(expected):
        return actual[len(expected)]
    return None
