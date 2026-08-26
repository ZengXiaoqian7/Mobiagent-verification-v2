"""MobiAgent step-executor adapter preparation.

Stage 4 intentionally implements the preflight side first.  It produces the
step-bound payload and a not-yet-dispatched execution manifest that the real
MobiAgent runner must fill in during device execution.
"""

from __future__ import annotations

import ast
import base64
from dataclasses import dataclass, replace
import hashlib
import json
import logging
import os
from pathlib import Path
import sys
import time
import types
from typing import Any, Callable, Mapping
import xml.etree.ElementTree as ET
from uuid import uuid4

from PIL import Image

from .contract import compile_app_test_contract
from .executor import EvidenceState, ExecutionRecord, StepExecutionResult, StepStatus
from .manifest import (
    EXECUTION_MANIFEST_SCHEMA_VERSION,
    StepEvidenceRecord,
    TestExecutionManifest,
    write_execution_manifest,
)
from .model_client import (
    extract_json_object,
    model_config_from_env,
    post_chat_completion,
)
from .schema import TestCaseError, TestCaseSpec, TestStep, dump_json
from .step_gate import (
    StepGateDecision,
    evaluate_dispatch_failure_gate,
    evaluate_micro_action_gate,
    evaluate_step_gate,
)
from .step_intent import StepExecutionIntent, compile_step_execution_intent


MOBIAGENT_STEP_PAYLOAD_SCHEMA_VERSION = "app-test-mobiagent-step-payload-v1"
MOBIAGENT_STEP_PAYLOAD_FILE = "mobiagent_step_payload.json"
MOBIAGENT_PREFLIGHT_MANIFEST_FILE = "test_execution_manifest.json"
TEXT_ATTRIBUTE_KEYS = (
    "text",
    "originalText",
    "content-desc",
    "description",
    "value",
    "label",
    "hint",
)
RUNNER_CONTROL_ACTIONS = frozenset({"info", "call_user", "abort"})
SEMANTIC_ATTRIBUTE_KEYS = TEXT_ATTRIBUTE_KEYS + (
    "id",
    "key",
    "accessibilityId",
    "resource-id",
    "resourceId",
    "type",
    "class",
)
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class MobiAgentPreflightResult:
    run_id: str
    output_dir: Path
    payload_path: Path
    manifest_path: Path
    step_count: int
    device_mutation: bool = False
    paid_provider_call: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "output_dir": str(self.output_dir),
            "payload_path": str(self.payload_path),
            "manifest_path": str(self.manifest_path),
            "step_count": self.step_count,
            "device_mutation": self.device_mutation,
            "paid_provider_call": self.paid_provider_call,
        }


@dataclass
class MobiAgentStepExecutor:
    """Real, step-bound MobiAgent executor.

    This path uses the existing runner/mobiagent Device classes for real device
    mutation and evidence capture, but it does not use the old whole-task
    planner/decider loop and does not treat model `done` as an App verdict.
    """

    output_dir: Path
    device: str = "Harmony"
    device_serial: str | None = None
    runner_root: Path | None = None
    device_instance: Any | None = None
    step_decider: Callable[[StepExecutionIntent, TestCaseSpec, Mapping[str, Any]], Mapping[str, Any]] | None = None
    target_locator: Callable[[TestStep, TestCaseSpec, Mapping[str, Any], bool], Mapping[str, Any] | None] | None = None
    allow_legacy_target_hints: bool = False
    use_e2e: bool = False
    use_qwen3: bool = True
    bbox_flag: bool = True
    decider_protocol: str = "qwen_json"
    observation_sleep_scale: float | None = None
    name: str = "mobiagent_real_step"

    def execute(self, test_case: TestCaseSpec) -> ExecutionRecord:
        raw_trace_dir = self.output_dir.resolve() / "mobiagent_step_trace"
        raw_trace_dir.mkdir(parents=True, exist_ok=True)
        frames: list[dict[str, Any]] = []
        frame_visible_texts: dict[str, list[str]] = {}
        actions: list[dict[str, Any]] = []
        history: list[str] = []
        step_results: list[StepExecutionResult] = []
        action_index = 0
        started_package = test_case.app_under_test.package
        first_step_is_open_app = bool(
            test_case.steps and test_case.steps[0].action_type == "OPEN_APP"
        )

        # The default real runner will make model calls.  Validate remote
        # credentials before opening or navigating the app, so an absent key
        # never leaves a partially-mutated test session behind.  Injected
        # deterministic test locators deliberately bypass this requirement.
        if self.step_decider is None and self.target_locator is None and not self.allow_legacy_target_hints:
            try:
                _import_original_mobiagent().validate_model_service_environment()
            except Exception as exc:  # noqa: BLE001 - configuration is an environment blocker.
                if _is_model_service_blocker(exc):
                    return _environment_blocked_record(
                        test_case,
                        self.name,
                        raw_trace_dir,
                        f"mobiagent model service configuration blocked: {exc}",
                    )
                raise

        try:
            device = self.device_instance or self._connect_device()
            if not first_step_is_open_app:
                if started_package:
                    device.app_start(started_package)
                elif test_case.app_under_test.name:
                    device.start_app(test_case.app_under_test.name)
            initial = self._capture_frame(
                device,
                raw_trace_dir,
                frame_id=0,
                relative_to_action_ms=0,
            )
            frames.append(initial)
            frame_visible_texts["0"] = list(initial["visible_texts"])
            blocker = _environment_blocker_frame(initial)
            if blocker is not None:
                return _environment_blocked_record(
                    test_case,
                    self.name,
                    raw_trace_dir,
                    f"mobiagent initial environment blocked: {blocker}",
                )
        except Exception as exc:  # noqa: BLE001 - fail closed before mutation claims.
            return _environment_blocked_record(
                test_case,
                self.name,
                raw_trace_dir,
                f"mobiagent device setup failed: {type(exc).__name__}: {exc}",
            )

        next_frame_id = 1
        for step_index, step in enumerate(test_case.steps):
            next_step = (
                test_case.steps[step_index + 1]
                if step_index + 1 < len(test_case.steps)
                else None
            )
            attempts = 0
            gate_attempts: list[dict[str, Any]] = []
            attempt_evidence: list[dict[str, Any]] = []
            while attempts <= step.max_retries:
                attempts += 1
                action_index += 1
                attempt_started_ms = int(time.time() * 1000)
                current_frame = frames[-1] if frames else None
                pre_frame = current_frame["frame_id"] if current_frame else None
                try:
                    action_record = self._execute_one_step(
                        device,
                        step,
                        test_case,
                        action_index=action_index,
                        raw_trace_dir=raw_trace_dir,
                        current_frame=current_frame,
                        next_frame_id=next_frame_id,
                        history=history,
                    )
                    dispatch_finished_ms = int(time.time() * 1000)
                    action_record["dispatch_started_ms"] = attempt_started_ms
                    action_record["dispatch_finished_ms"] = dispatch_finished_ms
                    action_record["dispatch_duration_ms"] = max(
                        0, dispatch_finished_ms - attempt_started_ms
                    )
                    actions.append(action_record)
                except _TargetNotFound as exc:
                    dispatch_finished_ms = int(time.time() * 1000)
                    gate = evaluate_dispatch_failure_gate(
                        test_case=test_case,
                        step=step,
                        attempt=attempts,
                        pre_frame=current_frame,
                        error=str(exc),
                        max_retries=step.max_retries,
                    )
                    gate_attempts.append(gate.as_dict())
                    attempt_evidence.append(
                        _build_attempt_evidence(
                            attempt=attempts,
                            action_index=action_index,
                            pre_frame=current_frame,
                            action_record=None,
                            post_frames=(),
                            gate=gate,
                            dispatch_started_ms=attempt_started_ms,
                            dispatch_finished_ms=dispatch_finished_ms,
                            action_dispatched=False,
                            retry_class=(
                                "PRE_DISPATCH_RETRY"
                                if gate.gate_decision == StepGateDecision.RETRY
                                else "NO_REDISPATCH"
                            ),
                            retry_reason=gate.reason,
                            error=str(exc),
                        )
                    )
                    if gate.gate_decision == StepGateDecision.RETRY:
                        continue
                    step_results.append(
                        StepExecutionResult(
                            step_id=step.step_id,
                            status=_step_status_from_gate(gate.gate_decision),
                            action_type=step.action_type,
                            attempts=attempts,
                            resolved_value=step.resolved_value(test_case.test_data),
                            target=step.target,
                            pre_frame=pre_frame,
                            error=str(exc),
                            evidence={
                                "test_case_id": test_case.test_case_id,
                                "step_id": step.step_id,
                                "attempt": attempts,
                                "action_index": action_index,
                                "target_match": False,
                                "step_gate": gate.as_dict(),
                                "step_gate_attempts": list(gate_attempts),
                                "attempt_evidence": list(attempt_evidence),
                                "gate_decision": gate.gate_decision,
                                "target_evidence": gate.target_evidence,
                                "action_conformance": gate.action_conformance,
                            },
                        )
                    )
                    break
                except _UnsupportedRealAction as exc:
                    dispatch_finished_ms = int(time.time() * 1000)
                    attempt_evidence.append(
                        _build_attempt_evidence(
                            attempt=attempts,
                            action_index=action_index,
                            pre_frame=current_frame,
                            action_record=None,
                            post_frames=(),
                            gate=None,
                            dispatch_started_ms=attempt_started_ms,
                            dispatch_finished_ms=dispatch_finished_ms,
                            action_dispatched=False,
                            retry_class="NO_REDISPATCH",
                            retry_reason="unsupported action was not dispatched",
                            error=str(exc),
                        )
                    )
                    step_results.append(
                        StepExecutionResult(
                            step_id=step.step_id,
                            status=StepStatus.UNSUPPORTED,
                            action_type=step.action_type,
                            attempts=attempts - 1,
                            resolved_value=step.resolved_value(test_case.test_data),
                            target=step.target,
                            pre_frame=pre_frame,
                            error=str(exc),
                            evidence={"attempt_evidence": list(attempt_evidence)},
                        )
                    )
                    break
                except Exception as exc:  # noqa: BLE001
                    dispatch_finished_ms = int(time.time() * 1000)
                    attempt_evidence.append(
                        _build_attempt_evidence(
                            attempt=attempts,
                            action_index=action_index,
                            pre_frame=current_frame,
                            action_record=None,
                            post_frames=(),
                            gate=None,
                            dispatch_started_ms=attempt_started_ms,
                            dispatch_finished_ms=dispatch_finished_ms,
                            action_dispatched=False,
                            retry_class="NO_REDISPATCH",
                            retry_reason="step execution raised before a gate decision",
                            error=f"{type(exc).__name__}: {exc}",
                        )
                    )
                    if _is_model_service_blocker(exc):
                        step_results.append(
                            StepExecutionResult(
                                step_id=step.step_id,
                                status=StepStatus.ENV_BLOCKED,
                                action_type=step.action_type,
                                attempts=attempts,
                                resolved_value=step.resolved_value(test_case.test_data),
                                target=step.target,
                                pre_frame=pre_frame,
                                blocker="model_service_access",
                                error=f"mobiagent model service blocked: {exc}",
                                evidence={"attempt_evidence": list(attempt_evidence)},
                            )
                        )
                        break
                    step_results.append(
                        StepExecutionResult(
                            step_id=step.step_id,
                            status=StepStatus.STEP_FAILED,
                            action_type=step.action_type,
                            attempts=attempts,
                            resolved_value=step.resolved_value(test_case.test_data),
                            target=step.target,
                            pre_frame=pre_frame,
                            error=f"mobiagent step execution failed: {type(exc).__name__}: {exc}",
                            evidence={"attempt_evidence": list(attempt_evidence)},
                        )
                    )
                    break

                goal_frames = [
                    frame
                    for frame in action_record.get("goal_observation_frames", [])
                    if isinstance(frame, Mapping)
                ]
                if goal_frames:
                    frames.extend(dict(frame) for frame in goal_frames)
                    for frame in goal_frames:
                        frame_id = frame.get("frame_id")
                        if isinstance(frame_id, int):
                            frame_visible_texts[str(frame_id)] = list(frame.get("visible_texts", ()))
                    next_frame_id = max(
                        next_frame_id,
                        max(
                            int(frame["frame_id"])
                            for frame in goal_frames
                            if isinstance(frame.get("frame_id"), int)
                        )
                        + 1,
                    )
                post_frames = self._capture_observation_burst(
                    device,
                    raw_trace_dir,
                    next_frame_id=next_frame_id,
                    pre_frame=current_frame,
                    policy=test_case.observation_policy,
                    force_full_schedule=_step_requires_full_observation_window(
                        test_case,
                        step,
                    ),
                )
                next_frame_id += len(post_frames)
                frames.extend(post_frames)
                for post in post_frames:
                    frame_visible_texts[str(post["frame_id"])] = list(post["visible_texts"])
                gate_post_frames = tuple([*goal_frames, *post_frames])
                post_action_context = _evaluate_post_action_context(
                    step,
                    gate_post_frames,
                )
                if post_action_context is not None:
                    action_record["post_action_context"] = post_action_context
                input_effect = (
                    _evaluate_input_effect(step, action_record, gate_post_frames)
                    if str(action_record.get("target_source") or "").startswith("hierarchy_")
                    else None
                )
                if input_effect is not None:
                    action_record["input_effect"] = input_effect
                next_target_resolution = self._resolve_next_step_target(
                    next_step,
                    test_case,
                    post_frames[-1] if post_frames else current_frame,
                )
                if next_target_resolution is not None:
                    action_record["next_step_target_resolution"] = next_target_resolution
                gate = evaluate_step_gate(
                    test_case=test_case,
                    step=step,
                    action_record=action_record,
                    attempt=attempts,
                    pre_frame=current_frame,
                    post_frames=gate_post_frames,
                    next_step=next_step,
                    next_step_target_evidence=(
                        next_target_resolution.get("status")
                        if next_target_resolution is not None
                        else None
                    ),
                )
                reobserve_count = 0
                reobserve_started_ms: int | None = None
                if (
                    gate.gate_decision == StepGateDecision.INCONCLUSIVE
                    and _has_dispatched_action(action_record)
                ):
                    reobserve_started_ms = int(time.time() * 1000)
                    reobserve_frames = self._capture_observation_burst(
                        device,
                        raw_trace_dir,
                        next_frame_id=next_frame_id,
                        pre_frame=post_frames[-1] if post_frames else current_frame,
                        policy=test_case.observation_policy,
                        force_full_schedule=True,
                    )
                    reobserve_count = 1
                    next_frame_id += len(reobserve_frames)
                    for reobserve_frame in reobserve_frames:
                        reobserve_frame["observation_phase"] = "reobserve"
                        reobserve_frame["reobserve_attempt"] = attempts
                    frames.extend(reobserve_frames)
                    post_frames = [*post_frames, *reobserve_frames]
                    for post in reobserve_frames:
                        frame_visible_texts[str(post["frame_id"])] = list(post["visible_texts"])
                    gate_post_frames = tuple([*goal_frames, *post_frames])
                    post_action_context = _evaluate_post_action_context(step, gate_post_frames)
                    if post_action_context is not None:
                        action_record["post_action_context"] = post_action_context
                    next_target_resolution = self._resolve_next_step_target(
                        next_step,
                        test_case,
                        post_frames[-1] if post_frames else current_frame,
                    )
                    if next_target_resolution is not None:
                        action_record["next_step_target_resolution"] = next_target_resolution
                    gate = evaluate_step_gate(
                        test_case=test_case,
                        step=step,
                        action_record=action_record,
                        attempt=attempts,
                        pre_frame=current_frame,
                        post_frames=gate_post_frames,
                        next_step=next_step,
                        next_step_target_evidence=(
                            next_target_resolution.get("status")
                            if next_target_resolution is not None
                            else None
                        ),
                    )
                post_frame_ids = tuple(frame["frame_id"] for frame in gate_post_frames)
                evidence = {
                    **action_record,
                    "test_case_id": test_case.test_case_id,
                    "step_id": step.step_id,
                    "attempt": attempts,
                    "action_ids": list(action_record.get("action_ids") or [action_index]),
                    "post_observation_burst": _observation_burst_summary(
                        post_frames,
                        policy=test_case.observation_policy,
                    ),
                    "goal_observation_frame_ids": [
                        frame["frame_id"] for frame in goal_frames if isinstance(frame.get("frame_id"), int)
                    ],
                    "target_match": action_record.get("target_match"),
                    "step_gate": gate.as_dict(),
                    "gate_decision": gate.gate_decision,
                    "progress_status": gate.progress_status,
                    "target_evidence": gate.target_evidence,
                    "action_conformance": gate.action_conformance,
                    "environment_signal": gate.environment_signal,
                    "reobserve_count": reobserve_count,
                    "reobserve_started_ms": reobserve_started_ms,
                }
                if gate_attempts:
                    evidence["step_gate_attempts"] = [*gate_attempts, gate.as_dict()]
                if gate.gate_decision == StepGateDecision.CONTINUE:
                    attempt_evidence.append(
                        _build_attempt_evidence(
                            attempt=attempts,
                            action_index=action_index,
                            pre_frame=current_frame,
                            action_record=action_record,
                            post_frames=gate_post_frames,
                            gate=gate,
                            dispatch_started_ms=attempt_started_ms,
                            dispatch_finished_ms=int(time.time() * 1000),
                            action_dispatched=True,
                            retry_class=(
                                "REOBSERVE_THEN_CONTINUE"
                                if reobserve_count
                                else "CONTINUE"
                            ),
                            retry_reason=(
                                "additional read-only observation after insufficient evidence"
                                if reobserve_count
                                else None
                            ),
                        )
                    )
                    evidence["attempt_evidence"] = list(attempt_evidence)
                    step_results.append(
                        StepExecutionResult(
                            step_id=step.step_id,
                            status=StepStatus.STEP_COMPLETED,
                            action_type=step.action_type,
                            attempts=attempts,
                            resolved_value=step.resolved_value(test_case.test_data),
                            target=step.target,
                            pre_frame=pre_frame,
                            post_frames=post_frame_ids,
                            evidence=evidence,
                        )
                    )
                    break
                if (
                    gate.gate_decision == StepGateDecision.RETRY
                    and attempts <= step.max_retries
                    and (
                        (
                            gate.target_evidence == "OVERLAY_BLOCKED"
                            and _action_has_external_overlay(
                                action_record,
                                test_case.app_under_test.package,
                            )
                        )
                        or _needs_navigation_context_recovery(step, action_record)
                    )
                ):
                    recovery_kind = None
                    if (
                        gate.target_evidence == "OVERLAY_BLOCKED"
                        and _action_has_external_overlay(
                            action_record,
                            test_case.app_under_test.package,
                        )
                    ):
                        recovery_kind = "SAFE_OVERLAY_RECOVERY"
                        recovery = self._dismiss_external_overlay(
                            device,
                            frames[-1] if frames else current_frame,
                            test_case.app_under_test.package,
                            raw_trace_dir=raw_trace_dir,
                            action_index=action_index,
                            next_frame_id=next_frame_id,
                            policy=test_case.observation_policy,
                            step_id=step.step_id,
                        )
                        if recovery is not None:
                            recovery_action, recovery_frames = recovery
                            actions.append(recovery_action)
                            frames.extend(recovery_frames)
                            for recovery_frame in recovery_frames:
                                frame_visible_texts[str(recovery_frame["frame_id"])] = list(
                                    recovery_frame["visible_texts"]
                                )
                            next_frame_id += len(recovery_frames)
                            attempt_audit = _build_attempt_evidence(
                                attempt=attempts,
                                action_index=action_index,
                                pre_frame=current_frame,
                                action_record=action_record,
                                post_frames=gate_post_frames,
                                gate=gate,
                                dispatch_started_ms=attempt_started_ms,
                                dispatch_finished_ms=int(time.time() * 1000),
                                action_dispatched=True,
                                retry_class=recovery_kind,
                                retry_reason=gate.reason,
                            )
                            attempt_audit["recovery_action"] = dict(recovery_action)
                            attempt_audit["recovery_frame_ids"] = [
                                frame["frame_id"] for frame in recovery_frames
                            ]
                            attempt_evidence.append(attempt_audit)
                            evidence["retry_class"] = recovery_kind
                            evidence["retry_reason"] = gate.reason
                            evidence["attempt_evidence"] = list(attempt_evidence)
                            continue
                    elif _needs_navigation_context_recovery(step, action_record):
                        recovery_kind = "SAFE_NAVIGATION_RECOVERY"
                        recovery_action, recovery_frames = self._recover_navigation_context(
                            device,
                            frames[-1] if frames else current_frame,
                            raw_trace_dir=raw_trace_dir,
                            action_index=action_index,
                            next_frame_id=next_frame_id,
                            policy=test_case.observation_policy,
                            step_id=step.step_id,
                        )
                        actions.append(recovery_action)
                        frames.extend(recovery_frames)
                        for recovery_frame in recovery_frames:
                            frame_visible_texts[str(recovery_frame["frame_id"])] = list(
                                recovery_frame["visible_texts"]
                            )
                        next_frame_id += len(recovery_frames)
                        attempt_audit = _build_attempt_evidence(
                            attempt=attempts,
                            action_index=action_index,
                            pre_frame=current_frame,
                            action_record=action_record,
                            post_frames=gate_post_frames,
                            gate=gate,
                            dispatch_started_ms=attempt_started_ms,
                            dispatch_finished_ms=int(time.time() * 1000),
                            action_dispatched=True,
                            retry_class=recovery_kind,
                            retry_reason=gate.reason,
                        )
                        attempt_audit["recovery_action"] = dict(recovery_action)
                        attempt_audit["recovery_frame_ids"] = [
                            frame["frame_id"] for frame in recovery_frames
                        ]
                        attempt_evidence.append(attempt_audit)
                        evidence["retry_class"] = recovery_kind
                        evidence["retry_reason"] = gate.reason
                        evidence["attempt_evidence"] = list(attempt_evidence)
                        history.append(
                            json.dumps(
                                {
                                    "action": "press_back",
                                    "step_id": step.step_id,
                                    "reason": "declared destination context was not reached",
                                    "recovery": True,
                                },
                                ensure_ascii=False,
                            )
                        )
                        continue
                if gate.gate_decision == StepGateDecision.RETRY:
                    gate = replace(
                        gate,
                        gate_decision=StepGateDecision.INCONCLUSIVE,
                        reason=(
                            "a dispatched action was not proven safe to repeat; "
                            "no business action was re-dispatched"
                        ),
                    )
                    evidence["requested_gate_decision"] = StepGateDecision.RETRY
                    evidence["retry_class"] = "NO_REDISPATCH"
                    evidence["retry_reason"] = gate.reason
                    evidence["step_gate"] = gate.as_dict()
                    evidence["gate_decision"] = gate.gate_decision
                attempt_evidence.append(
                    _build_attempt_evidence(
                        attempt=attempts,
                        action_index=action_index,
                        pre_frame=current_frame,
                        action_record=action_record,
                        post_frames=gate_post_frames,
                        gate=gate,
                        dispatch_started_ms=attempt_started_ms,
                        dispatch_finished_ms=int(time.time() * 1000),
                        action_dispatched=True,
                        retry_class=evidence.get("retry_class") or "NO_REDISPATCH",
                        retry_reason=evidence.get("retry_reason") or gate.reason,
                    )
                )
                evidence["attempt_evidence"] = list(attempt_evidence)
                status = _step_status_from_gate(gate.gate_decision)
                step_results.append(
                    StepExecutionResult(
                        step_id=step.step_id,
                        status=status,
                        action_type=step.action_type,
                        attempts=attempts,
                        resolved_value=step.resolved_value(test_case.test_data),
                        target=step.target,
                        pre_frame=pre_frame,
                        post_frames=post_frame_ids,
                        blocker=gate.environment_signal,
                        error=gate.reason,
                        evidence=evidence,
                    )
                )
                break
            if len(step_results) <= step_index or step_results[-1].step_id != step.step_id:
                break
            if step_results[-1].status != StepStatus.STEP_COMPLETED:
                break

        self._write_actions(raw_trace_dir, test_case, actions)
        final_texts = tuple(frames[-1].get("visible_texts", ())) if frames else ()
        initial_texts = tuple(frames[0].get("visible_texts", ())) if frames else ()
        return ExecutionRecord(
            test_case_id=test_case.test_case_id,
            executor=self.name,
            step_results=tuple(step_results),
            final_state=EvidenceState(
                visible_texts=final_texts,
                state_changed=(initial_texts != final_texts) if frames else None,
                evidence_sufficient=bool(frames),
                notes=("mobiagent real step executor",),
            ),
            raw_trace_dir=str(raw_trace_dir),
            metadata={
                "initial_visible_texts": list(initial_texts),
                "frame_visible_texts": frame_visible_texts,
                "frames": frames,
                "actions_path": str(raw_trace_dir / "actions.json"),
                "runner_done_is_step_done_only": True,
                "decider_history": list(history),
                "decider_history_count": len(history),
                "primary_locator": "runner.mobiagent.decider_grounder_step_bound",
            },
        )

    def _connect_device(self) -> Any:
        try:
            from runner.mobiagent.mobiagent import AndroidDevice, HarmonyDevice
        except Exception as exc:  # noqa: BLE001
            if self.device == "Harmony":
                from .harmony_hdc_device import HdcHarmonyDevice

                return HdcHarmonyDevice(serial=self.device_serial)
            raise RuntimeError(f"cannot import runner.mobiagent.mobiagent: {exc}") from exc
        if self.device == "Android":
            return AndroidDevice(adb_endpoint=self.device_serial)
        if self.device == "Harmony":
            return HarmonyDevice(serial=self.device_serial)
        raise RuntimeError(f"unsupported MobiAgent device type: {self.device}")

    def _execute_one_step(
        self,
        device: Any,
        step: TestStep,
        test_case: TestCaseSpec,
        *,
        action_index: int,
        raw_trace_dir: Path,
        current_frame: Mapping[str, Any] | None,
        next_frame_id: int,
        history: list[str],
    ) -> dict[str, Any]:
        expected_value = step.resolved_value(test_case.test_data)
        if step.action_type == "OPEN_APP":
            package = test_case.app_under_test.package or test_case.app_under_test.name
            if test_case.app_under_test.package:
                device.app_start(package)
            else:
                device.start_app(package)
            action = {
                "type": "open_app",
                "app_name": test_case.app_under_test.name,
                "package": test_case.app_under_test.package,
                "action_index": action_index,
                "target_match": True,
            }
            history.append(json.dumps({"action": "open_app", "step_id": step.step_id}, ensure_ascii=False))
            return action
        if step.action_type == "WAIT":
            time.sleep(min(step.timeout_seconds, 3.0))
            action = {
                "type": "wait",
                "seconds": min(step.timeout_seconds, 3.0),
                "action_index": action_index,
                "target_match": True,
            }
            history.append(json.dumps({"action": "wait", "step_id": step.step_id}, ensure_ascii=False))
            return action
        if step.action_type == "BACK":
            key = "back" if self.device == "Android" else 2
            device.keyevent(key)
            action = {
                "type": "press_back",
                "action_index": action_index,
                "target_match": True,
            }
            history.append(json.dumps({"action": "press_back", "step_id": step.step_id}, ensure_ascii=False))
            return action
        if step.action_type == "CLICK":
            return self._execute_mobiagent_decided_step(
                device,
                step,
                test_case,
                action_index=action_index,
                raw_trace_dir=raw_trace_dir,
                current_frame=current_frame,
                expected_runner_actions={"click"},
                wants_text_input=False,
                history=history,
            )
        if step.action_type == "INPUT":
            if expected_value is None:
                raise _UnsupportedRealAction("INPUT requires resolved value")
            return self._execute_mobiagent_decided_step(
                device,
                step,
                test_case,
                action_index=action_index,
                raw_trace_dir=raw_trace_dir,
                current_frame=current_frame,
                expected_runner_actions={"click_input", "input"},
                wants_text_input=True,
                history=history,
            )
        if step.action_type == "GUI_TASK" or step.step_mode == "GOAL":
            return self._execute_mobiagent_goal_step(
                device,
                step,
                test_case,
                action_index=action_index,
                raw_trace_dir=raw_trace_dir,
                current_frame=current_frame,
                next_frame_id=next_frame_id,
                history=history,
            )
        raise _UnsupportedRealAction(f"unsupported real MobiAgent action type: {step.action_type}")

    def _execute_mobiagent_goal_step(
        self,
        device: Any,
        step: TestStep,
        test_case: TestCaseSpec,
        *,
        action_index: int,
        raw_trace_dir: Path,
        current_frame: Mapping[str, Any] | None,
        next_frame_id: int,
        history: list[str],
    ) -> dict[str, Any]:
        if current_frame is None:
            raise _TargetNotFound(f"goal step {step.step_id} has no pre-frame for model grounding")
        expected_value = step.resolved_value(test_case.test_data)
        intent = compile_step_execution_intent(step, test_case)
        emitted_actions: list[dict[str, Any]] = []
        goal_observation_frames: list[dict[str, Any]] = []
        micro_observations: list[dict[str, Any]] = []
        micro_gates: list[dict[str, Any]] = []
        model_decisions: list[dict[str, Any]] = []
        current_goal_frame: Mapping[str, Any] = current_frame
        decision_source = "runner_mobiagent_decider"
        goal_completed = _goal_stage_confirmed(step, test_case, current_goal_frame)
        budget = _goal_micro_action_budget(step)
        goal_next_frame_id = next_frame_id
        for micro_index in range(1, budget + 1):
            if goal_completed:
                break
            if self.step_decider is not None:
                raw_decision = self.step_decider(intent, test_case, current_goal_frame)
                decision_source = "injected_step_decider"
            else:
                raw_decision = self._decide_with_mobiagent(
                    intent,
                    test_case,
                    current_goal_frame,
                    wants_text_input=expected_value is not None,
                    history=history,
                )
                decision_source = "runner_mobiagent_decider"
            decision = _normalize_runner_decision(raw_decision, intent)
            _log_step_model_decision(intent, decision)
            model_decisions.append(decision)
            micro_decision = _next_goal_micro_decision(decision, intent)
            if micro_decision is None:
                break
            micro_action = str(micro_decision.get("action") or "").lower()
            if micro_action == "done":
                goal_completed = _goal_stage_confirmed(step, test_case, current_goal_frame)
                break
            if micro_action not in {
                "click",
                "click_input",
                "input",
                "swipe",
                "wait",
                "press_back",
                "long_press",
                "press_home",
                "info",
                "call_user",
                "abort",
            }:
                raise _TargetNotFound(
                    f"runner chose unsupported micro-action {micro_action!r} for goal step {step.step_id}"
                )
            if micro_action in {"input", "click_input"} and expected_value is not None:
                micro_decision.setdefault("parameters", {})["text"] = expected_value
            emitted = self._dispatch_goal_micro_decision(
                device,
                micro_decision,
                action_index=(action_index * 100) + micro_index,
                raw_trace_dir=raw_trace_dir,
                current_frame=current_goal_frame,
                history=history,
            )
            emitted_actions.append(emitted)
            post_frames = self._capture_observation_burst(
                device,
                raw_trace_dir,
                next_frame_id=goal_next_frame_id,
                pre_frame=current_goal_frame,
                policy=test_case.observation_policy,
            )
            goal_next_frame_id += len(post_frames)
            for post_frame in post_frames:
                post_frame["observation_phase"] = "goal_micro_action"
                post_frame["goal_micro_action_index"] = micro_index
            goal_observation_frames.extend(post_frames)
            post_frame = post_frames[-1] if post_frames else current_goal_frame
            micro_gate = evaluate_micro_action_gate(
                test_case=test_case,
                step=step,
                micro_action_index=micro_index,
                action_record=emitted,
                pre_frame=current_goal_frame,
                post_frame=post_frame,
                post_frames=tuple(post_frames),
            )
            micro_gates.append(micro_gate.as_dict())
            micro_observations.append(
                {
                    "micro_action_index": micro_index,
                    "action_id": emitted.get("action_index"),
                    "action_type": emitted.get("type"),
                    "action_evidence": emitted,
                    "pre_frame": current_goal_frame.get("frame_id"),
                    "post_frame": post_frame.get("frame_id"),
                    "post_frame_ids": [
                        frame["frame_id"]
                        for frame in post_frames
                        if isinstance(frame.get("frame_id"), int)
                    ],
                    "post_observation_burst": _observation_burst_summary(
                        post_frames,
                        policy=test_case.observation_policy,
                    ),
                    "post_visible_texts": list(post_frame.get("visible_texts", ())),
                    "post_screenshot_sha256": post_frame.get("screenshot_sha256"),
                    "post_hierarchy_sha256": post_frame.get("hierarchy_sha256"),
                    "target_evidence": micro_gate.target_evidence,
                    "action_conformance": micro_gate.action_conformance,
                    "progress_status": micro_gate.progress_status,
                    "micro_gate_decision": micro_gate.gate_decision,
                    "micro_gate": micro_gate.as_dict(),
                }
            )
            current_goal_frame = post_frame
            goal_completed = _goal_stage_confirmed(step, test_case, current_goal_frame)
            if micro_gate.gate_decision != StepGateDecision.CONTINUE:
                break
        if not emitted_actions:
            raise _TargetNotFound(f"goal step {step.step_id} did not dispatch any micro-action")
        action_ids = [
            int(item["action_index"])
            for item in emitted_actions
            if isinstance(item.get("action_index"), int)
        ]
        return {
            "type": "gui_task",
            "action_index": action_index,
            "action_ids": action_ids,
            "micro_action_count": len(emitted_actions),
            "micro_actions": emitted_actions,
            "micro_action_observations": micro_observations,
            "micro_gates": micro_gates,
            "micro_gate_count": len(micro_gates),
            "goal_observation_frames": goal_observation_frames,
            "goal_completed": goal_completed,
            "goal_state": _goal_state(
                step,
                test_case,
                current_goal_frame,
                micro_gates,
                goal_completed,
            ),
            "goal_completion_evidence": _goal_completion_evidence(
                step,
                test_case,
                current_goal_frame,
            ),
            "text": expected_value,
            "target_match": True,
            "test_case_id": test_case.test_case_id,
            "step_id": step.step_id,
            "step_execution_intent": intent.as_dict(),
            "step_execution_intent_sha256": intent.sha256,
            "model_decisions": model_decisions,
            "model_decision": model_decisions[-1] if model_decisions else {},
            "runner_done_is_step_done_only": True,
            "runner_control_events": [
                item for item in emitted_actions
                if isinstance(item, Mapping) and item.get("runner_control_action")
            ],
            "target_source": decision_source,
            "generated_runtime_data": dict(test_case.runtime_generated_data),
        }

    def _dispatch_goal_micro_decision(
        self,
        device: Any,
        decision: Mapping[str, Any],
        *,
        action_index: int,
        raw_trace_dir: Path,
        current_frame: Mapping[str, Any],
        history: list[str],
    ) -> dict[str, Any]:
        action = str(decision.get("action") or "").lower()
        return self._dispatch_runner_decision(
            device,
            decision,
            action_index=action_index,
            raw_trace_dir=raw_trace_dir,
            current_frame=current_frame,
            history=history,
        )

    def _execute_mobiagent_decided_step(
        self,
        device: Any,
        step: TestStep,
        test_case: TestCaseSpec,
        *,
        action_index: int,
        raw_trace_dir: Path,
        current_frame: Mapping[str, Any] | None,
        expected_runner_actions: set[str],
        wants_text_input: bool,
        history: list[str],
    ) -> dict[str, Any]:
        if current_frame is None:
            raise _TargetNotFound(f"step {step.step_id} has no pre-frame for model grounding")
        intent = compile_step_execution_intent(step, test_case)
        hierarchy_target = (
            _resolve_hierarchy_control_target(
                current_frame,
                step,
                wants_text_input=wants_text_input,
            )
            if (
                self.step_decider is None
                and self.target_locator is None
                and not self.allow_legacy_target_hints
                # An instance-level model replacement is a deterministic
                # integration/test hook. Preserve its authority except for
                # identity-critical exact-text navigation, where its output
                # cannot safely broaden the selected conversation/contact.
                and (
                    "_decide_with_mobiagent" not in self.__dict__
                    or _prefer_exact_hierarchy_target(step)
                )
            )
            else None
        )
        if hierarchy_target is not None:
            emitted = self._dispatch_hierarchy_target(
                device,
                hierarchy_target,
                action_index=action_index,
                step=step,
                expected_value=intent.value if wants_text_input else None,
                history=history,
            )
            emitted.update(
                {
                    "test_case_id": test_case.test_case_id,
                    "step_id": step.step_id,
                    "step_execution_intent": intent.as_dict(),
                    "step_execution_intent_sha256": intent.sha256,
                    "runner_done_is_step_done_only": True,
                    "target_source": str(hierarchy_target.get("source") or "hierarchy_control"),
                    "runner_control_events": [],
                }
            )
            return emitted
        control_events: list[dict[str, Any]] = []
        while True:
            if self.step_decider is not None:
                raw_decision = self.step_decider(intent, test_case, current_frame)
                decision_source = "injected_step_decider"
            elif self.target_locator is not None:
                raw_decision = self._legacy_locator_as_decider_response(
                    step,
                    test_case,
                    current_frame,
                    wants_text_input=wants_text_input,
                )
                decision_source = "legacy_target_locator_adapter"
            elif self.allow_legacy_target_hints:
                raw_decision = self._legacy_target_hint_as_decider_response(
                    step,
                    current_frame,
                    wants_text_input=wants_text_input,
                )
                decision_source = "legacy_target_hint_adapter"
            else:
                raw_decision = self._decide_with_mobiagent(
                    intent,
                    test_case,
                    current_frame,
                    wants_text_input=wants_text_input,
                    history=history,
                )
                decision_source = "runner_mobiagent_decider"
            decision = _normalize_runner_decision(raw_decision, intent)
            _log_step_model_decision(intent, decision)
            action = str(decision.get("action") or "").lower()
            if action in RUNNER_CONTROL_ACTIONS:
                control = self._dispatch_runner_decision(
                    device,
                    decision,
                    action_index=action_index * 100 + len(control_events) + 1,
                    raw_trace_dir=raw_trace_dir,
                    current_frame=current_frame,
                    history=history,
                )
                control_events.append(control)
                if control.get("runner_control_continue") is True:
                    continue
                return {
                    **control,
                    "test_case_id": test_case.test_case_id,
                    "step_id": step.step_id,
                    "runner_control_events": control_events,
                    "step_execution_intent": intent.as_dict(),
                    "step_execution_intent_sha256": intent.sha256,
                    "model_decision": decision,
                    "runner_done_is_step_done_only": True,
                    "target_source": decision_source,
                }
            if action == "done":
                raise _TargetNotFound(
                    f"runner marked step {step.step_id} done before dispatching required action"
                )
            if action not in expected_runner_actions:
                raise _TargetNotFound(
                    f"runner chose action {action!r} for step {step.step_id}; expected one of {sorted(expected_runner_actions)}"
                )
            aligned_target = (
                _resolve_decider_aligned_text_target(current_frame, step.target, decision)
                if (
                    action == "click"
                    and self.step_decider is None
                    and self.target_locator is None
                    and not self.allow_legacy_target_hints
                )
                else None
            )
            if aligned_target is not None:
                emitted = self._dispatch_hierarchy_target(
                    device,
                    aligned_target,
                    action_index=action_index,
                    step=step,
                    expected_value=None,
                    history=history,
                )
                emitted.update(
                    {
                        "test_case_id": test_case.test_case_id,
                        "step_id": step.step_id,
                        "step_execution_intent": intent.as_dict(),
                        "step_execution_intent_sha256": intent.sha256,
                        "model_decision": decision,
                        "runner_done_is_step_done_only": True,
                        "target_source": "hierarchy_decider_aligned_text",
                        "runner_control_events": control_events,
                    }
                )
                return emitted
            if wants_text_input and intent.value is not None:
                decision.setdefault("parameters", {})["text"] = intent.value
            emitted = self._dispatch_runner_decision(
                device,
                decision,
                action_index=action_index,
                raw_trace_dir=raw_trace_dir,
                current_frame=current_frame,
                history=history,
            )
            break
        emitted.update(
            {
                "test_case_id": test_case.test_case_id,
                "step_id": step.step_id,
                "step_execution_intent": intent.as_dict(),
                "step_execution_intent_sha256": intent.sha256,
                "model_decision": decision,
                "runner_done_is_step_done_only": True,
                "target_source": decision_source,
                "runner_control_events": control_events,
            }
        )
        return emitted

    def _dispatch_hierarchy_target(
        self,
        device: Any,
        target: Mapping[str, Any],
        *,
        action_index: int,
        step: TestStep,
        expected_value: str | None,
        history: list[str],
    ) -> dict[str, Any]:
        """Dispatch an exact accessibility control without visual re-grounding.

        A text node can be a non-clickable child of the tappable list row.  Its
        own center is nevertheless inside that row, so clicking it preserves
        the exact target identity while avoiding a model bbox being snapped to
        a neighbouring low-information container.
        """

        center = target.get("center")
        bounds = _parse_bounds(target.get("bounds"))
        if (
            not isinstance(center, (list, tuple))
            or len(center) != 2
            or bounds is None
        ):
            raise _TargetNotFound("exact hierarchy text target is missing usable bounds")
        try:
            x, y = int(center[0]), int(center[1])
        except (TypeError, ValueError) as exc:
            raise _TargetNotFound("exact hierarchy text target has invalid center") from exc
        device.click(x, y)
        label = str(target.get("text") or step.instruction)
        hit_node = target.get("hit_node")
        hit_node = dict(hit_node) if isinstance(hit_node, Mapping) else {}
        is_input = expected_value is not None
        if is_input:
            # Harmony's RichEditor does not accept text until its focus change
            # has reached the UI process.  This is deliberately short and is
            # independent of a specific app or screen geometry.
            time.sleep(0.25)
            device.input(expected_value)
        decision = {
            "reasoning": "exact accessible control resolved from the current UI hierarchy",
            "action": "click_input" if is_input else "click",
            "parameters": {
                "target_element": label,
                "coords": [x, y],
            },
        }
        if is_input:
            decision["parameters"]["text"] = expected_value
        history.append(
            json.dumps(
                {
                    "action": "click_input" if is_input else "click",
                    "step_id": step.step_id,
                    "target_source": str(target.get("source") or "hierarchy_control"),
                    "target_element": label,
                    "click_point": [x, y],
                },
                ensure_ascii=False,
            )
        )
        return {
            "type": "click_input" if is_input else "click",
            "action_index": action_index,
            "target_element": label,
            "position_x": x,
            "position_y": y,
            "click_point": [x, y],
            "bounds": list(bounds),
            "target_match": True,
            "selector_clicked": True,
            "text": expected_value if is_input else None,
            "model_decision": decision,
            "xml_hit_test_result": {
                "click_point": [x, y],
                "target_element": label,
                "alignment_basis": str(target.get("source") or "hierarchy_control"),
                "selected_node": hit_node,
                "direct_hits": [hit_node] if hit_node else [],
            },
        }

    def _dispatch_runner_decision(
        self,
        device: Any,
        decision: Mapping[str, Any],
        *,
        action_index: int,
        raw_trace_dir: Path,
        current_frame: Mapping[str, Any],
        history: list[str],
    ) -> dict[str, Any]:
        runner_mobiagent = _import_original_mobiagent()
        screenshot_path = current_frame.get("screenshot_abs")
        if not isinstance(screenshot_path, str):
            raise _TargetNotFound("current frame is missing screenshot_abs")
        action = str(decision.get("action") or "").lower()
        actions: list[dict[str, Any]] = []
        if action in RUNNER_CONTROL_ACTIONS:
            if action == "info":
                continued = runner_mobiagent.handle_info_action(
                    decision, action_index, actions, history
                )
            elif action == "call_user":
                continued = runner_mobiagent.handle_call_user_action(
                    decision, action_index, actions, history
                )
            else:
                runner_mobiagent.handle_abort_action(decision, action_index, actions, history)
                continued = False
            if not actions:
                raise _TargetNotFound(f"runner control action {action!r} did not emit a trace record")
            emitted = dict(actions[-1])
            emitted["runner_control_action"] = action
            emitted["runner_control_continue"] = bool(continued)
            return emitted

        img = Image.open(screenshot_path)
        hierarchy_text = self._frame_hierarchy_text(current_frame, raw_trace_dir)
        screenshot_resize = _mobiagent_resized_screenshot_b64(screenshot_path)
        device_paths = {
            "current_dir": str(raw_trace_dir),
            "screenshot_name": str(current_frame.get("screenshot") or Path(screenshot_path).name),
        }
        if action == "click":
            grounder_bbox, grounder_no_bbox = self._grounder_prompt_templates(runner_mobiagent)
            try:
                runner_mobiagent.handle_click_action(
                    decision,
                    device,
                    img,
                    screenshot_resize,
                    grounder_bbox,
                    grounder_no_bbox,
                    self.bbox_flag,
                    self.use_qwen3,
                    self._use_direct_decider_geometry(),
                    str(raw_trace_dir),
                    device_paths,
                    device_paths["screenshot_name"],
                    action_index,
                    actions,
                    history,
                    hierarchy_text,
                )
            except ValueError as exc:
                # A malformed Grounder response cannot have dispatched a
                # device action. Normalize this narrow runner failure into
                # the executor's pre-dispatch retry path; letting it escape
                # as a generic ValueError incorrectly hard-fails the step
                # without consuming its safe retry budget.
                message = str(exc)
                if (
                    "Grounder response must contain" in message
                    or "Grounder response validation failed" in message
                ):
                    raise _TargetNotFound(
                        f"runner grounder returned no usable target geometry: {message}"
                    ) from exc
                raise
        elif action == "click_input":
            runner_mobiagent.handle_click_input_action(
                decision,
                device,
                img,
                str(raw_trace_dir),
                action_index,
                actions,
                history,
                hierarchy_text,
            )
        elif action == "input":
            runner_mobiagent.handle_input_action(decision, device, action_index, actions, history)
        elif action == "wait":
            runner_mobiagent.handle_wait_action(decision, action_index, actions, history)
        elif action == "press_back":
            runner_mobiagent.handle_press_back_action(
                decision,
                device,
                self.device,
                action_index,
                actions,
                history,
            )
        elif action == "swipe":
            runner_mobiagent.handle_swipe_action(
                decision,
                device,
                img,
                self.use_e2e,
                self.use_qwen3,
                str(raw_trace_dir),
                action_index,
                actions,
                history,
            )
        elif action == "long_press":
            runner_mobiagent.handle_long_press_action(
                decision,
                device,
                img,
                action_index,
                actions,
                history,
            )
        elif action == "press_home":
            runner_mobiagent.handle_press_home_action(
                decision,
                device,
                self.device,
                action_index,
                actions,
                history,
            )
        else:
            raise _TargetNotFound(f"unsupported step-bound runner action: {action}")
        if not actions:
            raise _TargetNotFound(f"runner action {action!r} did not emit an action record")
        emitted = dict(actions[-1])
        _attach_hierarchy_hit_test_evidence(emitted, decision, current_frame)
        return emitted

    def _grounder_prompt_templates(self, runner_mobiagent: Any) -> tuple[str | None, str | None]:
        if self.use_e2e:
            return None, None
        if self.use_qwen3:
            return (
                runner_mobiagent.load_prompt("grounder_qwen3_bbox.md"),
                runner_mobiagent.load_prompt("grounder_qwen3_coordinates.md"),
            )
        return (
            runner_mobiagent.load_prompt("grounder_bbox.md"),
            runner_mobiagent.load_prompt("grounder_coordinates.md"),
        )

    def _use_direct_decider_geometry(self) -> bool:
        """Keep direct bbox dispatch opt-in for real runs.

        A normal Decider response can contain a coarse bbox while still being
        intended for the Grounder refinement stage. Test-injected deciders and
        legacy locators already provide deterministic coordinates, so they
        retain direct dispatch for compatibility.
        """

        return bool(
            self.use_e2e
            or self.step_decider is not None
            or self.target_locator is not None
            or self.allow_legacy_target_hints
            # Instance-level replacement is the supported test/integration
            # hook for a deterministic Decider; it owns the supplied bbox.
            or "_decide_with_mobiagent" in self.__dict__
        )

    def _decide_with_mobiagent(
        self,
        intent: StepExecutionIntent,
        test_case: TestCaseSpec,
        current_frame: Mapping[str, Any],
        *,
        wants_text_input: bool,
        history: list[str],
    ) -> Mapping[str, Any]:
        del wants_text_input
        _reject_placeholder_mobiagent_env()
        runner_mobiagent = _import_original_mobiagent()
        if getattr(runner_mobiagent, "decider_client", None) is None:
            runner_mobiagent.init(
                os.getenv("MOBIAGENT_SERVICE_IP", "127.0.0.1"),
                int(os.getenv("MOBIAGENT_DECIDER_PORT", "8000")),
                int(os.getenv("MOBIAGENT_GROUNDER_PORT", "8001")),
                int(os.getenv("MOBIAGENT_PLANNER_PORT", "8002")),
            )
        screenshot_path = current_frame.get("screenshot_abs")
        if not isinstance(screenshot_path, str):
            raise _TargetNotFound("current frame is missing screenshot_abs")
        prompt = _step_bound_task_prompt(intent, test_case)
        messages = runner_mobiagent.build_decider_messages(
            prompt,
            list(history),
            _mobiagent_resized_screenshot_b64(screenshot_path),
            self.use_e2e,
            self.decider_protocol,
            self.device,
        )
        return runner_mobiagent.call_model_with_validation_retry(
            runner_mobiagent.decider_client,
            runner_mobiagent.decider_model,
            messages,
            validator_func=lambda response: runner_mobiagent.validate_decider_response(
                response,
                use_e2e=self.use_e2e,
                decider_protocol=self.decider_protocol,
            ),
            parser_func=runner_mobiagent.get_decider_parser(self.decider_protocol),
            max_retries=runner_mobiagent.MAX_RETRIES,
            max_tokens=runner_mobiagent.DECIDER_MAX_TOKENS,
            context="Decider",
        )

    def _legacy_locator_as_decider_response(
        self,
        step: TestStep,
        test_case: TestCaseSpec,
        current_frame: Mapping[str, Any],
        *,
        wants_text_input: bool,
    ) -> Mapping[str, Any]:
        assert self.target_locator is not None
        raw = self.target_locator(step, test_case, current_frame, wants_text_input)
        if raw is None:
            raise _TargetNotFound(f"step {step.step_id} target was not found by injected locator")
        try:
            x = int(raw["x"])
            y = int(raw["y"])
        except (KeyError, TypeError, ValueError) as exc:
            raise _TargetNotFound(f"injected target locator returned invalid coordinates: {raw}") from exc
        params = {
            "target_element": str(raw.get("target_element") or step.instruction),
        }
        bounds = raw.get("bounds")
        if isinstance(bounds, (list, tuple)) and len(bounds) == 4:
            params["bbox"] = [int(int(item) * 0.5) for item in bounds]
        else:
            params["coords"] = [x, y]
        if wants_text_input:
            params["text"] = step.resolved_value(test_case.test_data) or ""
        return {
            "reasoning": str(raw.get("reason") or "injected target locator"),
            "action": "click_input" if wants_text_input else "click",
            "parameters": params,
        }

    def _legacy_target_hint_as_decider_response(
        self,
        step: TestStep,
        current_frame: Mapping[str, Any] | None,
        *,
        wants_text_input: bool,
    ) -> Mapping[str, Any]:
        target = _declared_coordinate_target(step.target, fallback_label=step.instruction)
        if target is None:
            target = _resolve_target(current_frame, step.target, wants_text_input=wants_text_input)
        if target is None:
            raise _TargetNotFound(
                f"step {step.step_id} target was not found by legacy target hints"
            )
        params = {
            "coords": list(target["center"]),
            "target_element": target["text"],
        }
        if wants_text_input:
            params["text"] = ""
        return {
            "reasoning": "legacy target hints were explicitly enabled",
            "action": "click_input" if wants_text_input else "click",
            "parameters": params,
        }

    def _frame_hierarchy_text(
        self,
        current_frame: Mapping[str, Any],
        raw_trace_dir: Path,
    ) -> Any:
        hierarchy_name = current_frame.get("hierarchy")
        if not isinstance(hierarchy_name, str):
            return None
        path = raw_trace_dir / hierarchy_name
        if not path.is_file():
            return None
        text = path.read_text(encoding="utf-8")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text

    def _locate_with_vision(
        self,
        step: TestStep,
        test_case: TestCaseSpec,
        current_frame: Mapping[str, Any] | None,
        *,
        wants_text_input: bool,
    ) -> dict[str, Any] | None:
        if current_frame is None:
            return None
        if self.target_locator is not None:
            raw = self.target_locator(step, test_case, current_frame, wants_text_input)
        else:
            raw = _model_target_locator(step, test_case, current_frame, wants_text_input)
        if raw is None:
            return None
        try:
            x = int(raw["x"])
            y = int(raw["y"])
        except (KeyError, TypeError, ValueError) as exc:
            raise _TargetNotFound(f"vision target locator returned invalid coordinates: {raw}") from exc
        return {
            "text": str(raw.get("target_element") or step.instruction),
            "bounds": (x, y, x, y),
            "center": (x, y),
            "source": "vision_model",
            "confidence": raw.get("confidence"),
            "reason": raw.get("reason"),
        }

    def _resolve_next_step_target(
        self,
        next_step: TestStep | None,
        test_case: TestCaseSpec,
        current_frame: Mapping[str, Any] | None,
    ) -> dict[str, Any] | None:
        if next_step is None:
            return None
        target = dict(next_step.target)
        if not target.get("text_candidates"):
            candidates = [
                target[key]
                for key in ("label", "text", "name")
                if isinstance(target.get(key), str) and target.get(key).strip()
            ]
            if candidates:
                target["text_candidates"] = candidates
        wants_text_input = next_step.action_type == "INPUT"
        resolved = _resolve_target(
            current_frame,
            target,
            wants_text_input=wants_text_input,
        )
        if resolved is not None:
            return {
                "status": "CONFORMANT",
                "source": "post_frame_hierarchy_target_resolution",
                "target": resolved,
                "frame_id": current_frame.get("frame_id") if isinstance(current_frame, Mapping) else None,
            }
        if self.target_locator is not None and isinstance(current_frame, Mapping):
            try:
                located = self.target_locator(
                    next_step,
                    test_case,
                    current_frame,
                    wants_text_input,
                )
            except Exception as exc:  # noqa: BLE001 - gate evidence is conservative.
                return {
                    "status": "UNKNOWN",
                    "source": "runtime_target_locator_error",
                    "error": type(exc).__name__,
                }
            if isinstance(located, Mapping):
                return {
                    "status": "CONFORMANT",
                    "source": "runtime_target_locator",
                    "target": dict(located),
                    "frame_id": current_frame.get("frame_id"),
                }
        return {
            "status": "UNKNOWN",
            "source": "runtime_target_not_locatable",
            "frame_id": current_frame.get("frame_id") if isinstance(current_frame, Mapping) else None,
        }

    def _capture_frame(
        self,
        device: Any,
        raw_trace_dir: Path,
        *,
        frame_id: int,
        relative_to_action_ms: int,
    ) -> dict[str, Any]:
        screenshot_path = raw_trace_dir / f"{frame_id}.jpg"
        device.screenshot(str(screenshot_path))
        hierarchy = device.dump_hierarchy()
        hierarchy_text, hierarchy_suffix, nodes = _parse_hierarchy_dump(hierarchy)
        hierarchy_path = raw_trace_dir / f"{frame_id}{hierarchy_suffix}"
        hierarchy_kind = hierarchy_suffix.lstrip(".")
        hierarchy_path.write_text(hierarchy_text, encoding="utf-8")
        return {
            "frame_id": frame_id,
            "timestamp_ms": int(time.time() * 1000),
            "relative_to_action_ms": relative_to_action_ms,
            "screenshot": screenshot_path.name,
            "screenshot_abs": str(screenshot_path),
            "screenshot_sha256": _file_sha256(screenshot_path),
            "hierarchy": hierarchy_path.name,
            "hierarchy_kind": hierarchy_kind,
            "hierarchy_sha256": _file_sha256(hierarchy_path),
            "stability": "UNKNOWN",
            "visible_texts": list(_visible_texts(nodes)),
            "xml_nodes": nodes,
        }

    def _capture_observation_burst(
        self,
        device: Any,
        raw_trace_dir: Path,
        *,
        next_frame_id: int,
        pre_frame: Mapping[str, Any] | None,
        policy: Mapping[str, Any],
        force_full_schedule: bool = False,
    ) -> list[dict[str, Any]]:
        """Capture only the evidence needed for this action's state transition.

        Every action still receives an immediate post-action observation.  A
        non-terminal action stops after the configured number of consecutive
        stable observations; a step referenced by an assertion always keeps
        the complete window so delayed success/failure remains auditable.
        """

        schedule = _observation_schedule(policy)
        frames: list[dict[str, Any]] = []
        previous: Mapping[str, Any] | None = pre_frame
        elapsed_ms = 0
        consecutive_stable = 0
        stable_frames_required = _stable_frames_required(policy)
        for index, delay_ms in enumerate(schedule):
            sleep_ms = max(0, delay_ms - elapsed_ms)
            self._sleep_for_observation(sleep_ms)
            elapsed_ms = delay_ms
            frame = self._capture_frame(
                device,
                raw_trace_dir,
                frame_id=next_frame_id + index,
                relative_to_action_ms=delay_ms,
            )
            frame["observation_burst_index"] = index
            frame["observation_phase"] = "immediate" if delay_ms == 0 else "delayed"
            frame["stability"] = _frame_stability(previous, frame)
            frames.append(frame)
            previous = frame
            if frame["stability"] == "STABLE":
                consecutive_stable += 1
            else:
                consecutive_stable = 0
            if (
                policy.get("adaptive_capture", False) is True
                and policy.get("stop_when_stable", True) is True
                and not force_full_schedule
                and consecutive_stable >= stable_frames_required
                and index < len(schedule) - 1
            ):
                frame["observation_stop_reason"] = "consecutive_stable_frames"
                break
        return frames

    def _dismiss_external_overlay(
        self,
        device: Any,
        frame: Mapping[str, Any] | None,
        app_package: str | None,
        *,
        raw_trace_dir: Path,
        action_index: int,
        next_frame_id: int,
        policy: Mapping[str, Any],
        step_id: str,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
        if frame is None:
            return None
        point = _blank_dismiss_point(frame, app_package)
        if point is None:
            return None
        device.click(*point)
        recovery_action = {
            "type": "dismiss_external_overlay",
            "action_index": action_index * 100 + 1,
            "click_point": list(point),
            "target_source": "hierarchy_blank_point",
            "recovery_reason": "external overlay blocked the declared target",
            "recovery_for_step": step_id,
            "target_match": True,
        }
        recovery_frames = self._capture_observation_burst(
            device,
            raw_trace_dir,
            next_frame_id=next_frame_id,
            pre_frame=frame,
            policy=policy,
        )
        return recovery_action, recovery_frames

    def _recover_navigation_context(
        self,
        device: Any,
        frame: Mapping[str, Any] | None,
        *,
        raw_trace_dir: Path,
        action_index: int,
        next_frame_id: int,
        policy: Mapping[str, Any],
        step_id: str,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Return from a proven wrong navigation destination before retrying."""

        runner_mobiagent = _import_original_mobiagent()
        key = "back" if self.device == "Android" else getattr(
            getattr(runner_mobiagent, "KeyCode", None), "BACK", 2
        )
        device.keyevent(key)
        recovery_action = {
            "type": "press_back",
            "action_index": action_index * 100 + 2,
            "target_source": "destination_context_recovery",
            "recovery_reason": "declared destination context was not reached",
            "recovery_for_step": step_id,
            "target_match": True,
        }
        recovery_frames = self._capture_observation_burst(
            device,
            raw_trace_dir,
            next_frame_id=next_frame_id,
            pre_frame=frame,
            policy=policy,
        )
        return recovery_action, recovery_frames

    def _sleep_for_observation(self, delay_ms: int) -> None:
        if delay_ms <= 0:
            return
        scale = self._observation_sleep_scale()
        if scale <= 0:
            return
        time.sleep((delay_ms / 1000.0) * scale)

    def _observation_sleep_scale(self) -> float:
        if self.observation_sleep_scale is not None:
            return max(0.0, float(self.observation_sleep_scale))
        raw = os.getenv("APP_TEST_OBSERVATION_SLEEP_SCALE")
        if raw:
            try:
                return max(0.0, float(raw))
            except ValueError:
                return 1.0
        if self.device_instance is not None:
            return 0.0
        return 1.0

    def _write_actions(
        self,
        raw_trace_dir: Path,
        test_case: TestCaseSpec,
        actions: list[dict[str, Any]],
    ) -> None:
        payload = {
            "schema_version": "app-test-mobiagent-step-actions-v1",
            "test_case_id": test_case.test_case_id,
            "app_name": test_case.app_under_test.name,
            "package": test_case.app_under_test.package,
            "action_count": len(actions),
            "actions": actions,
        }
        (raw_trace_dir / "actions.json").write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )


def build_mobiagent_step_payload(
    test_case: TestCaseSpec,
    *,
    run_id: str,
    device: str = "Harmony",
    device_serial: str | None = None,
    runner_root: Path | None = None,
) -> dict[str, Any]:
    """Build the exact step-level payload expected by the future runner path."""

    return {
        "schema_version": MOBIAGENT_STEP_PAYLOAD_SCHEMA_VERSION,
        "run_id": run_id,
        "test_case_id": test_case.test_case_id,
        "test_case_sha256": test_case.sha256,
        "contract_sha256": compile_app_test_contract(test_case).sha256,
        "device": device,
        "device_serial": device_serial,
        "runner_root": str(runner_root.resolve()) if runner_root is not None else None,
        "app_under_test": test_case.app_under_test.as_dict(),
        "feature": test_case.feature,
        "preconditions": [item.as_dict() for item in test_case.preconditions],
        "test_data": dict(test_case.test_data),
        "runtime_generated_data": dict(test_case.runtime_generated_data),
        "observation_policy": dict(test_case.observation_policy),
        "verification_policy": dict(test_case.verification_policy),
        "verification_steps": [
            {
                "ordinal": index + 1,
                "verification_step_id": step.verification_step_id,
                "instruction": step.instruction,
                "action_type": step.action_type,
                "target": dict(step.target),
                "timeout_seconds": step.timeout_seconds,
                "max_retries": step.max_retries,
                "read_only_action": step.is_read_only,
            }
            for index, step in enumerate(test_case.verification_steps)
        ],
        "runner_constraints": {
            "one_step_per_call": True,
            "preserve_step_order": True,
            "do_not_modify_test_data": True,
            "goal_step_allows_internal_micro_actions": True,
            "goal_step_must_not_skip_next_user_step": True,
            "runner_done_is_step_done_only": True,
            "app_result_not_decided_by_runner": True,
            "verification_steps_are_read_only": True,
        },
        "steps": [
            _step_payload(step, test_case, index)
            for index, step in enumerate(test_case.steps)
        ],
    }


def prepare_mobiagent_preflight(
    test_case: TestCaseSpec,
    output_dir: Path,
    *,
    run_id: str | None = None,
    device: str = "Harmony",
    device_serial: str | None = None,
    runner_root: Path | None = None,
) -> MobiAgentPreflightResult:
    """Write step-bound runner payload and an accepted pre-dispatch manifest."""

    resolved_run_id = run_id or f"{test_case.test_case_id}-{uuid4().hex[:12]}"
    runtime_test_case = test_case.with_runtime_context(run_id=resolved_run_id)
    contract = compile_app_test_contract(runtime_test_case)
    root = output_dir.resolve()
    root.mkdir(parents=True, exist_ok=True)
    dump_json(root / "test_case.normalized.json", runtime_test_case.as_dict())
    payload = build_mobiagent_step_payload(
        runtime_test_case,
        run_id=resolved_run_id,
        device=device,
        device_serial=device_serial,
        runner_root=runner_root,
    )
    payload_path = root / MOBIAGENT_STEP_PAYLOAD_FILE
    dump_json(payload_path, payload)
    manifest = build_preflight_manifest(
        runtime_test_case,
        run_id=resolved_run_id,
        executor="mobiagent_preflight",
        payload_path=payload_path,
        contract_sha256=contract.sha256,
    )
    manifest_path = root / MOBIAGENT_PREFLIGHT_MANIFEST_FILE
    write_execution_manifest(manifest_path, manifest)
    manifest.validate_against(runtime_test_case, contract.sha256)
    return MobiAgentPreflightResult(
        run_id=resolved_run_id,
        output_dir=root,
        payload_path=payload_path,
        manifest_path=manifest_path,
        step_count=len(test_case.steps),
    )


def build_preflight_manifest(
    test_case: TestCaseSpec,
    *,
    run_id: str,
    executor: str,
    payload_path: Path,
    contract_sha256: str,
) -> TestExecutionManifest:
    steps = tuple(
        StepEvidenceRecord(
            step_id=step.step_id,
            dispatch_status="ACTION_NOT_DISPATCHED",
            conformance_status="UNKNOWN",
            effect_status="NOT_EVALUATED",
            action_type=step.action_type,
            attempts=0,
            expected_value=step.resolved_value(test_case.test_data),
            evidence={
                "preflight_only": True,
                "instruction": step.instruction,
                "payload_path": str(payload_path),
                "runtime_intent": compile_step_execution_intent(step, test_case).as_dict(),
            },
        )
        for step in test_case.steps
    )
    return TestExecutionManifest(
        run_id=run_id,
        test_case_id=test_case.test_case_id,
        test_case_sha256=test_case.sha256,
        contract_sha256=contract_sha256,
        executor=executor,
        steps=steps,
        final_state=_preflight_final_state(),
        metadata={
            "schema_version": EXECUTION_MANIFEST_SCHEMA_VERSION,
            "preflight_only": True,
            "payload_path": str(payload_path),
            "runtime_generated_data": dict(test_case.runtime_generated_data),
        },
    )


def _step_payload(step: TestStep, test_case: TestCaseSpec, index: int) -> Mapping[str, Any]:
    value = step.resolved_value(test_case.test_data)
    intent = compile_step_execution_intent(step, test_case)
    return {
        "ordinal": index + 1,
        "step_id": step.step_id,
        "instruction": step.instruction,
        "action_type": step.action_type,
        "step_mode": step.step_mode,
        "target": dict(step.target),
        "target_is_legacy_hint": bool(step.target),
        "runtime_intent": intent.as_dict(),
        "runtime_intent_sha256": intent.sha256,
        "value": value,
        "value_ref": step.value_ref,
        "timeout_seconds": step.timeout_seconds,
        "max_retries": step.max_retries,
        "runner_prompt": _runner_prompt(step, value),
    }


def _runner_prompt(step: TestStep, value: str | None) -> str:
    lines = [
        f"Execute only this test step: [{step.step_id}] {step.instruction}",
        f"Action type: {step.action_type}",
        "Do not skip, reorder, or execute later test steps.",
        "Do not decide whether the App feature passed.",
    ]
    if value is not None:
        lines.append(f"Use this exact input value: {value}")
    return "\n".join(lines)


def _preflight_final_state():
    from .executor import EvidenceState

    return EvidenceState(
        evidence_sufficient=False,
        notes=("mobiagent preflight only; no device action dispatched",),
    )


def _step_bound_task_prompt(
    intent: StepExecutionIntent,
    test_case: TestCaseSpec,
) -> str:
    lines = [
        f"App under test: {test_case.app_under_test.name}",
        f"Package: {test_case.app_under_test.package or 'unknown'}",
        f"Feature under test: {test_case.feature}",
        "Execute exactly one user App test step and stop after that user step.",
        f"Step id: {intent.step_id}",
        f"User instruction: {intent.original_instruction}",
        f"Required action family: {intent.action_family}",
        f"Step mode: {intent.step_mode}",
        f"Semantic target: {intent.semantic_target}",
        "Do not execute later business steps.",
        "Do not decide whether the App feature passed.",
        "If you emit done, it only means the current step is complete.",
    ]
    if intent.allow_micro_actions:
        lines.append(
            "This is a GOAL step: you may execute multiple internal micro-actions "
            "needed to complete this user step, but do not advance to the next user step."
        )
    if intent.value is not None:
        lines.append(f"Use this exact input value when input is needed: {intent.value!r}")
    return "\n".join(lines)


def _normalize_runner_decision(
    decision: Mapping[str, Any],
    intent: StepExecutionIntent,
) -> dict[str, Any]:
    if not isinstance(decision, Mapping):
        raise _TargetNotFound(f"runner decision for step {intent.step_id} is not an object")
    action = str(decision.get("action") or "").strip().lower()
    if action == "scroll":
        action = "swipe"
    parameters = decision.get("parameters")
    if not isinstance(parameters, Mapping):
        parameters = {}
    normalized = {
        "reasoning": str(decision.get("reasoning") or ""),
        "action": action,
        "parameters": dict(parameters),
    }
    for key in ("raw_protocol", "stepfun_fields"):
        if key in decision:
            normalized[key] = decision[key]
    if not action:
        raise _TargetNotFound(f"runner decision for step {intent.step_id} is missing action")
    return normalized


def _log_step_model_decision(
    intent: StepExecutionIntent,
    decision: Mapping[str, Any],
) -> None:
    """Keep the step-bound model judgment visible in the CLI and trace logs."""
    LOGGER.info(
        "App-test model response step=%s:\n%s",
        intent.step_id,
        json.dumps(dict(decision), ensure_ascii=False, indent=2, sort_keys=True),
    )


def _goal_micro_decisions(
    decision: Mapping[str, Any],
    intent: StepExecutionIntent,
) -> list[dict[str, Any]]:
    raw_micro = decision.get("micro_actions")
    params = decision.get("parameters")
    if raw_micro is None and isinstance(params, Mapping):
        raw_micro = params.get("micro_actions")
    if isinstance(raw_micro, list):
        return [
            _normalize_runner_decision(item, intent)
            for item in raw_micro
            if isinstance(item, Mapping)
        ]
    action = str(decision.get("action") or "").lower()
    if action == "scroll":
        action = "swipe"
        decision = {**dict(decision), "action": action}
    if action in {
        "click",
        "click_input",
        "input",
        "swipe",
        "wait",
        "press_back",
        "long_press",
        "press_home",
        "info",
        "call_user",
        "abort",
        "done",
    }:
        return [dict(decision)]
    return []


def _next_goal_micro_decision(
    decision: Mapping[str, Any],
    intent: StepExecutionIntent,
) -> dict[str, Any] | None:
    micro_decisions = _goal_micro_decisions(decision, intent)
    for item in micro_decisions:
        action = str(item.get("action") or "").lower()
        if action:
            return item
    return None


def _goal_micro_action_budget(step: TestStep) -> int:
    target = step.target if isinstance(step.target, Mapping) else {}
    raw = target.get("max_micro_actions")
    if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0:
        return min(raw, 20)
    return 8


def _goal_stage_confirmed(
    step: TestStep,
    test_case: TestCaseSpec,
    frame: Mapping[str, Any] | None,
) -> bool:
    if not isinstance(frame, Mapping):
        return False
    texts = [str(item) for item in frame.get("visible_texts", ()) if str(item)]
    folded = [text.casefold() for text in texts]
    for expected in _goal_expected_values(step, test_case):
        needle = expected.casefold()
        if any(needle in text for text in folded):
            return True
    for marker in _goal_stage_markers(step):
        needle = marker.casefold()
        if any(needle in text for text in folded):
            return True
    return False


def _goal_completion_evidence(
    step: TestStep,
    test_case: TestCaseSpec,
    frame: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if not isinstance(frame, Mapping):
        return {"confirmed": False}
    texts = [str(item) for item in frame.get("visible_texts", ()) if str(item)]
    matched_values = [
        value
        for value in _goal_expected_values(step, test_case)
        if any(value.casefold() in text.casefold() for text in texts)
    ]
    matched_markers = [
        marker
        for marker in _goal_stage_markers(step)
        if any(marker.casefold() in text.casefold() for text in texts)
    ]
    return {
        "confirmed": bool(matched_values or matched_markers),
        "frame_id": frame.get("frame_id"),
        "matched_expected_values": matched_values,
        "matched_stage_markers": matched_markers,
        "visible_texts": texts,
    }


def _goal_state(
    step: TestStep,
    test_case: TestCaseSpec,
    frame: Mapping[str, Any] | None,
    micro_gates: list[dict[str, Any]],
    goal_completed: bool,
) -> dict[str, Any]:
    completion = _goal_completion_evidence(step, test_case, frame)
    last_gate = micro_gates[-1] if micro_gates else None
    blocked = (
        isinstance(last_gate, Mapping)
        and last_gate.get("gate_decision")
        in {
            StepGateDecision.TEST_EXECUTION_FAIL,
            StepGateDecision.ENV_BLOCKED,
            StepGateDecision.INCONCLUSIVE,
        }
    )
    completed = goal_completed and not blocked
    return {
        "status": "COMPLETED" if completed else ("BLOCKED" if blocked else "IN_PROGRESS"),
        "completed": completed,
        "stage_result_observed": goal_completed,
        "frame_id": frame.get("frame_id") if isinstance(frame, Mapping) else None,
        "micro_action_count": len(micro_gates),
        "last_micro_gate_decision": last_gate.get("gate_decision") if isinstance(last_gate, Mapping) else None,
        "last_progress_status": last_gate.get("progress_status") if isinstance(last_gate, Mapping) else None,
        "completion_evidence": completion,
    }


def _goal_expected_values(step: TestStep, test_case: TestCaseSpec) -> tuple[str, ...]:
    values: list[str] = []
    step_value = step.resolved_value(test_case.test_data)
    if step_value:
        values.append(step_value)
    for assertion in test_case.expected_results:
        if assertion.type != "TEXT_VISIBLE":
            continue
        value = assertion.resolved_value(test_case.test_data)
        if value:
            values.append(value)
    return tuple(dict.fromkeys(values))


def _goal_stage_markers(step: TestStep) -> tuple[str, ...]:
    target = step.target if isinstance(step.target, Mapping) else {}
    markers: list[str] = []
    raw = target.get("stage_result_text_candidates")
    if isinstance(raw, (list, tuple)):
        markers.extend(str(item) for item in raw if str(item))
    for key in ("stage_result_text", "completion_text", "success_text"):
        value = target.get(key)
        if isinstance(value, str) and value.strip():
            markers.append(value.strip())
    markers.extend(["发布完成", "发布成功", "已发布", "posted", "published", "sent", "success"])
    return tuple(dict.fromkeys(item for item in markers if item))


def _mobiagent_resized_screenshot_b64(screenshot_path: str) -> str:
    with Image.open(screenshot_path) as img:
        runner_mobiagent = _import_original_mobiagent()

        resized = img.resize(
            (
                int(img.width * runner_mobiagent.factor),
                int(img.height * runner_mobiagent.factor),
            ),
            Image.Resampling.LANCZOS,
        )
        import io

        buffered = io.BytesIO()
        resized.save(buffered, format="JPEG")
    return base64.b64encode(buffered.getvalue()).decode("utf-8")


def _reject_placeholder_mobiagent_env() -> None:
    for name in (
        "MOBIAGENT_BASE_URL",
        "MOBIAGENT_DECIDER_BASE_URL",
        "MOBIAGENT_GROUNDER_BASE_URL",
        "MOBIAGENT_MODEL",
        "MOBIAGENT_DECIDER_MODEL",
        "MOBIAGENT_GROUNDER_MODEL",
        "MOBIAGENT_API_KEY",
    ):
        value = os.getenv(name)
        if value and _looks_like_placeholder(value):
            raise _TargetNotFound(
                f"{name} still contains a placeholder value; replace it with a real model service setting"
            )


def _looks_like_placeholder(value: str) -> bool:
    text = str(value or "")
    return any(
        marker in text
        for marker in (
            "YOUR_",
            "YOUR-",
            "your_",
            "your-",
            "your_model_endpoint",
            "your-model-endpoint",
            "YOUR_MODEL_ENDPOINT",
            "YOUR_MODEL_NAME",
            "YOUR_KEY",
        )
    )


def _is_model_service_blocker(exc: BaseException) -> bool:
    """Avoid coupling the adapter to an optional runner import at module load."""

    return bool(getattr(exc, "is_model_service_blocker", False))


def _step_status_from_gate(gate_decision: str) -> str:
    if gate_decision == StepGateDecision.ENV_BLOCKED:
        return StepStatus.ENV_BLOCKED
    if gate_decision == StepGateDecision.INCONCLUSIVE:
        return StepStatus.INCONCLUSIVE
    return StepStatus.STEP_FAILED


def _has_dispatched_action(action_record: Mapping[str, Any] | None) -> bool:
    if not isinstance(action_record, Mapping):
        return False
    raw_ids = action_record.get("action_ids")
    if isinstance(raw_ids, (list, tuple)) and any(
        isinstance(item, int) and not isinstance(item, bool) for item in raw_ids
    ):
        return True
    return isinstance(action_record.get("action_index"), int) and not isinstance(
        action_record.get("action_index"), bool
    )


def _frame_attempt_evidence(frame: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(frame, Mapping):
        return None
    return {
        "frame_id": frame.get("frame_id"),
        "timestamp_ms": frame.get("timestamp_ms"),
        "relative_to_action_ms": frame.get("relative_to_action_ms"),
        "observation_phase": frame.get("observation_phase"),
        "stability": frame.get("stability"),
        "screenshot": frame.get("screenshot"),
        "screenshot_sha256": frame.get("screenshot_sha256"),
        "hierarchy": frame.get("hierarchy"),
        "hierarchy_sha256": frame.get("hierarchy_sha256"),
        "visible_texts": list(frame.get("visible_texts", ())),
    }


def _build_attempt_evidence(
    *,
    attempt: int,
    action_index: int,
    pre_frame: Mapping[str, Any] | None,
    action_record: Mapping[str, Any] | None,
    post_frames: tuple[Mapping[str, Any], ...],
    gate: Any,
    dispatch_started_ms: int,
    dispatch_finished_ms: int,
    action_dispatched: bool,
    retry_class: str,
    retry_reason: str | None,
    error: str | None = None,
) -> dict[str, Any]:
    action = dict(action_record) if isinstance(action_record, Mapping) else None
    action_ids = []
    if isinstance(action, Mapping):
        raw_ids = action.get("action_ids")
        if isinstance(raw_ids, (list, tuple)):
            action_ids = [item for item in raw_ids if isinstance(item, int)]
        elif isinstance(action.get("action_index"), int):
            action_ids = [action["action_index"]]
    if not action_ids and gate is not None:
        action_ids = list(getattr(gate, "action_ids", ()) or ())
    frame_evidence = [_frame_attempt_evidence(frame) for frame in post_frames]
    frame_evidence = [item for item in frame_evidence if item is not None]
    immediate = [
        item for item in frame_evidence if item.get("relative_to_action_ms") == 0
    ]
    delayed = [
        item for item in frame_evidence if item.get("relative_to_action_ms") not in (None, 0)
    ]
    gate_payload = gate.as_dict() if hasattr(gate, "as_dict") else None
    return {
        "schema_version": "app-test-mobiagent-attempt-evidence-v1",
        "attempt": attempt,
        "action_index": action_index,
        "action_dispatched": bool(action_dispatched),
        "action_ids": action_ids,
        "pre_frame": _frame_attempt_evidence(pre_frame),
        "dispatch": {
            "started_ms": dispatch_started_ms,
            "finished_ms": dispatch_finished_ms,
            "duration_ms": max(0, dispatch_finished_ms - dispatch_started_ms),
        },
        "action": action,
        "immediate_post_frames": immediate,
        "delayed_post_frames": delayed,
        "post_frames": frame_evidence,
        "target_evidence": gate_payload.get("target_evidence") if gate_payload else None,
        "progress_evidence": {
            "progress_status": gate_payload.get("progress_status") if gate_payload else None,
            "action_conformance": gate_payload.get("action_conformance") if gate_payload else None,
            "next_step_target_evidence": (
                gate_payload.get("next_step_target_evidence") if gate_payload else None
            ),
        },
        "environment_signal": gate_payload.get("environment_signal") if gate_payload else None,
        "gate_decision": gate_payload.get("gate_decision") if gate_payload else None,
        "gate_reason": gate_payload.get("reason") if gate_payload else None,
        "gate": gate_payload,
        "retry_class": retry_class,
        "retry_reason": retry_reason,
        "error": error,
    }


def _retry_is_safe(step: TestStep, action_record: Mapping[str, Any]) -> bool:
    """Return true only for a pre-dispatch retry opportunity.

    Once an action index or runner action id exists, the action may have
    changed App state.  WAIT/BACK are not exceptions here: an observation
    deficit is handled by re-observation, never by replaying a business step.
    """

    del step
    return not _has_dispatched_action(action_record)


def _needs_navigation_context_recovery(
    step: TestStep,
    action_record: Mapping[str, Any],
) -> bool:
    context = action_record.get("post_action_context")
    if not isinstance(context, Mapping) or context.get("status") != "NON_CONFORMANT":
        return False
    if step.action_type != "CLICK" or str(action_record.get("type") or "").lower() != "click":
        return False
    target = step.target if isinstance(step.target, Mapping) else {}
    if str(target.get("role") or "").casefold() not in {
        "conversation",
        "contact",
        "chat",
        "thread",
        "tab",
        "section",
        "navigation",
    }:
        return False
    if not _safe_navigation_recovery_action(step, action_record):
        return False
    return True


def _safe_navigation_recovery_action(
    step: TestStep,
    action_record: Mapping[str, Any],
) -> bool:
    """Require proof that a destination click was read-only before recovery."""

    if not _has_dispatched_action(action_record) or action_record.get("target_match") is not True:
        return False
    if action_record.get("micro_actions") or action_record.get("micro_gates"):
        return False
    target = step.target if isinstance(step.target, Mapping) else {}
    role = str(target.get("role") or "").casefold()
    if role not in {"conversation", "contact", "chat", "thread", "tab", "section", "navigation"}:
        return False
    signature = " ".join(
        str(value or "")
        for value in (
            step.instruction,
            target.get("label"),
            target.get("text"),
            target.get("name"),
            target.get("role"),
        )
    ).casefold()
    if any(
        marker in signature
        for marker in (
            "publish", "post", "send", "delete", "pay", "payment", "submit",
            "confirm", "like", "follow", "发布", "发送", "删除", "支付",
            "提交", "确认", "点赞", "关注",
        )
    ):
        return False
    if action_record.get("selector_clicked") is True:
        return True
    point = action_record.get("click_point")
    if not isinstance(point, (list, tuple)) or len(point) != 2:
        return False
    try:
        x, y = int(point[0]), int(point[1])
    except (TypeError, ValueError):
        return False
    hit_test = action_record.get("xml_hit_test_result")
    bounds: list[tuple[int, int, int, int]] = []
    if isinstance(hit_test, Mapping):
        nodes = list(hit_test.get("direct_hits", ()) or ())
        if isinstance(hit_test.get("selected_node"), Mapping):
            nodes.append(hit_test["selected_node"])
        for node in nodes:
            if isinstance(node, Mapping):
                parsed = _parse_bounds(node.get("bounds"))
                if parsed is not None:
                    bounds.append(parsed)
    for key in ("runtime_bounds", "resolved_bounds"):
        parsed = _parse_bounds(action_record.get(key))
        if parsed is not None:
            bounds.append(parsed)
    source = str(action_record.get("target_source") or "").casefold()
    if source.startswith("hierarchy_"):
        parsed = _parse_bounds(action_record.get("bounds"))
        if parsed is not None:
            bounds.append(parsed)
    return any(x1 <= x <= x2 and y1 <= y <= y2 for x1, y1, x2, y2 in bounds)


def _action_has_external_overlay(
    action_record: Mapping[str, Any],
    app_package: str | None,
) -> bool:
    hit_test = action_record.get("xml_hit_test_result")
    if isinstance(hit_test, Mapping):
        nodes = list(hit_test.get("direct_hits", ()) or ())
        selected_node = hit_test.get("selected_node")
        if isinstance(selected_node, Mapping):
            nodes.append(selected_node)
    else:
        nodes = ()
    for node in nodes:
        if not isinstance(node, Mapping):
            continue
        bundle_name = node.get("window_bundle_name")
        signature = " ".join(
            str(node.get(key) or "")
            for key in ("tag", "text", "semantic_text", "window_page_path")
        ).casefold()
        if (
            bundle_name
            and bundle_name != app_package
            and not any(marker in signature for marker in ("keyboard", "inputmethod", "sceneboard"))
        ):
            return True
    return False


def _blank_dismiss_point(
    frame: Mapping[str, Any],
    app_package: str | None,
) -> tuple[int, int] | None:
    nodes = frame.get("xml_nodes", ())
    app_bounds: tuple[int, int, int, int] | None = None
    blocked: list[tuple[int, int, int, int]] = []
    for node in nodes:
        bounds = _parse_bounds(node.get("bounds"))
        if bounds is None:
            continue
        if node.get("window_bundle_name") == app_package:
            if app_bounds is None or _bounds_area(bounds) > _bounds_area(app_bounds):
                app_bounds = bounds
            if node.get("clickable") or node.get("text") or node.get("semantic_text"):
                blocked.append(bounds)
        elif node.get("window_bundle_name"):
            blocked.append(bounds)
    if app_bounds is None:
        return None
    left, top, right, bottom = app_bounds
    if right <= left or bottom <= top:
        return None
    candidates = (
        (0.18, 0.22),
        (0.50, 0.22),
        (0.82, 0.22),
        (0.18, 0.48),
        (0.82, 0.48),
        (0.18, 0.78),
        (0.82, 0.78),
    )
    for x_ratio, y_ratio in candidates:
        point = (
            int(left + (right - left) * x_ratio),
            int(top + (bottom - top) * y_ratio),
        )
        if not any(_point_in_bounds(point[0], point[1], bounds) for bounds in blocked):
            return point
    return None


def _bounds_area(bounds: tuple[int, int, int, int]) -> int:
    left, top, right, bottom = bounds
    return max(0, right - left) * max(0, bottom - top)


def _import_original_mobiagent() -> Any:
    for _ in range(5):
        try:
            from runner.mobiagent import mobiagent as runner_mobiagent

            return runner_mobiagent
        except ModuleNotFoundError as exc:
            if not _install_optional_runner_dependency_stub(exc.name):
                raise _TargetNotFound(f"cannot import original MobiAgent runner: {exc}") from exc
    raise _TargetNotFound("cannot import original MobiAgent runner after installing optional stubs")


def _install_optional_runner_dependency_stub(name: str | None) -> bool:
    if name == "openai":
        module = types.ModuleType("openai")

        class _MissingOpenAI:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                del args, kwargs

            def __getattr__(self, attr: str) -> Any:
                raise RuntimeError("openai package is required for real MobiAgent model calls")

        module.OpenAI = _MissingOpenAI  # type: ignore[attr-defined]
        sys.modules.setdefault("openai", module)
        return True
    if name == "uiautomator2":
        module = types.ModuleType("uiautomator2")

        def _connect(*args: Any, **kwargs: Any) -> Any:
            del args, kwargs
            raise RuntimeError("uiautomator2 package is required for real Android device calls")

        module.connect = _connect  # type: ignore[attr-defined]
        sys.modules.setdefault("uiautomator2", module)
        return True
    if name == "dotenv":
        module = types.ModuleType("dotenv")
        module.load_dotenv = lambda *args, **kwargs: None  # type: ignore[attr-defined]
        sys.modules.setdefault("dotenv", module)
        return True
    if name == "cv2":
        module = types.ModuleType("cv2")
        module.FONT_HERSHEY_SIMPLEX = 0  # type: ignore[attr-defined]
        module.arrowedLine = lambda *args, **kwargs: None  # type: ignore[attr-defined]
        module.putText = lambda *args, **kwargs: None  # type: ignore[attr-defined]
        module.imwrite = lambda *args, **kwargs: False  # type: ignore[attr-defined]
        sys.modules.setdefault("cv2", module)
        return True
    if name == "llama_index":
        root = types.ModuleType("llama_index")
        core = types.ModuleType("llama_index.core")
        embeddings = types.ModuleType("llama_index.embeddings")
        huggingface = types.ModuleType("llama_index.embeddings.huggingface")

        class _Unavailable:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                del args, kwargs

            @classmethod
            def from_defaults(cls, *args: Any, **kwargs: Any) -> "_Unavailable":
                del args, kwargs
                return cls()

            def __getattr__(self, attr: str) -> Any:
                raise RuntimeError("llama_index package is required for MobiAgent experience retrieval")

        class _Settings:
            llm: Any = None
            embed_model: Any = None

        core.VectorStoreIndex = _Unavailable  # type: ignore[attr-defined]
        core.SimpleDirectoryReader = _Unavailable  # type: ignore[attr-defined]
        core.Document = _Unavailable  # type: ignore[attr-defined]
        core.Settings = _Settings  # type: ignore[attr-defined]
        core.StorageContext = _Unavailable  # type: ignore[attr-defined]
        core.load_index_from_storage = lambda *args, **kwargs: _Unavailable()  # type: ignore[attr-defined]
        huggingface.HuggingFaceEmbedding = _Unavailable  # type: ignore[attr-defined]
        sys.modules.setdefault("llama_index", root)
        sys.modules.setdefault("llama_index.core", core)
        sys.modules.setdefault("llama_index.embeddings", embeddings)
        sys.modules.setdefault("llama_index.embeddings.huggingface", huggingface)
        return True
    return False


def reject_unimplemented_device_execution() -> None:
    raise TestCaseError(
        "MobiAgent device execution requires --execute-runner with a connected "
        "Android/Harmony device and available runner dependencies."
    )


class _TargetNotFound(RuntimeError):
    pass


class _UnsupportedRealAction(RuntimeError):
    pass


def _environment_blocked_record(
    test_case: TestCaseSpec,
    executor: str,
    raw_trace_dir: Path,
    reason: str,
) -> ExecutionRecord:
    first_step = test_case.steps[0]
    return ExecutionRecord(
        test_case_id=test_case.test_case_id,
        executor=executor,
        step_results=(
            StepExecutionResult(
                step_id=first_step.step_id,
                status=StepStatus.ENV_BLOCKED,
                action_type=first_step.action_type,
                attempts=0,
                blocker=reason,
                error=reason,
            ),
        ),
        final_state=EvidenceState(
            evidence_sufficient=False,
            notes=("mobiagent real execution did not start", reason),
        ),
        raw_trace_dir=str(raw_trace_dir),
        metadata={"mobiagent_blocker": reason},
    )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _parse_hierarchy_dump(hierarchy: Any) -> tuple[str, str, list[dict[str, Any]]]:
    if isinstance(hierarchy, str) and hierarchy.lstrip().startswith("<"):
        return hierarchy, ".xml", _xml_nodes(hierarchy)
    value = hierarchy
    if isinstance(hierarchy, str):
        value = _parse_jsonish_hierarchy(hierarchy)
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)
        return text, ".json", _json_nodes(value)
    text = str(hierarchy)
    return text, ".txt", []


def _parse_jsonish_hierarchy(value: str) -> Any:
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        pass
    try:
        return ast.literal_eval(value)
    except (SyntaxError, ValueError):
        return None


def _xml_nodes(hierarchy_text: str) -> list[dict[str, Any]]:
    try:
        root = ET.fromstring(hierarchy_text)
    except ET.ParseError:
        return []
    nodes: list[dict[str, Any]] = []
    for element in root.iter():
        text = _joined_attributes(element.attrib, TEXT_ATTRIBUTE_KEYS)
        semantic_text = _joined_attributes(element.attrib, SEMANTIC_ATTRIBUTE_KEYS)
        bounds = _parse_bounds(element.attrib.get("bounds") or element.attrib.get("rect"))
        nodes.append(
            {
                "tag": element.tag,
                "attributes": dict(element.attrib),
                "text": text,
                "semantic_text": semantic_text,
                "bounds": bounds,
                "clickable": str(element.attrib.get("clickable", "")).lower() == "true",
                "enabled": str(element.attrib.get("enabled", "true")).lower() != "false",
            }
        )
    return nodes


def _json_nodes(value: Any) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []

    def visit(
        item: Any,
        window_bundle_name: str = "",
        window_page_path: str = "",
        window_focused: bool = False,
    ) -> None:
        if isinstance(item, Mapping):
            attrs = item.get("attributes")
            attributes = dict(attrs) if isinstance(attrs, Mapping) else {
                str(key): child
                for key, child in item.items()
                if not isinstance(child, (dict, list, tuple))
            }
            bundle_name = str(attributes.get("bundleName") or window_bundle_name)
            page_path = str(attributes.get("pagePath") or window_page_path)
            focused = (
                str(attributes.get("focused", "")).lower() == "true"
                or window_focused
            )
            text = _joined_attributes(attributes, TEXT_ATTRIBUTE_KEYS)
            semantic_text = _joined_attributes(attributes, SEMANTIC_ATTRIBUTE_KEYS)
            nodes.append(
                {
                    "tag": str(item.get("type") or attributes.get("type") or "node"),
                    "attributes": attributes,
                    "text": text,
                    "semantic_text": semantic_text,
                    "bounds": _parse_bounds(attributes.get("bounds") or attributes.get("rect")),
                    "clickable": str(attributes.get("clickable", "")).lower() == "true",
                    "enabled": str(attributes.get("enabled", "true")).lower() != "false",
                    "visible": str(attributes.get("visible", "true")).lower() != "false",
                    "window_bundle_name": bundle_name,
                    "window_page_path": page_path,
                    "window_focused": focused,
                }
            )
            children = item.get("children")
            if isinstance(children, list):
                for child in children:
                    visit(child, bundle_name, page_path, focused)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return nodes


def _joined_attributes(attributes: Mapping[str, Any], keys: tuple[str, ...]) -> str:
    values = [
        str(attributes.get(key) or "").strip()
        for key in keys
        if str(attributes.get(key) or "").strip()
    ]
    return " ".join(dict.fromkeys(values)).strip()


def _visible_texts(nodes: list[dict[str, Any]]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(node["text"] for node in nodes if node.get("text")))


def _attach_hierarchy_hit_test_evidence(
    action_record: dict[str, Any],
    decision: Mapping[str, Any],
    current_frame: Mapping[str, Any],
) -> None:
    action_type = str(action_record.get("type") or "").lower()
    if action_type not in {"click", "click_input"}:
        return
    existing = action_record.get("xml_hit_test_result")
    if isinstance(existing, Mapping) and _xml_hit_test_result_is_decisive(existing):
        return
    point = _action_click_point(action_record)
    if point is None:
        return
    nodes = current_frame.get("xml_nodes")
    if not isinstance(nodes, list):
        return
    x, y = point
    direct_hits = [
        node
        for node in nodes
        if _node_supports_runtime_hit(node) and _point_in_bounds(x, y, node.get("bounds"))
    ]
    if not direct_hits:
        action_record["xml_hit_test_result"] = {
            "click_point": [x, y],
            "alignment_basis": "hierarchy_hit_test",
            "rejection_reason": "outside_target",
            "direct_hits": [],
        }
        return
    direct_hits.sort(key=_node_area)
    target_element = _decision_target_element(decision)
    action_record["xml_hit_test_result"] = {
        "click_point": [x, y],
        "target_element": target_element,
        "alignment_basis": "direct_supported_hit",
        "selected_node": _runtime_hit_node_summary(direct_hits[0]),
        "direct_hits": [_runtime_hit_node_summary(node) for node in direct_hits[:5]],
    }


def _action_click_point(action_record: Mapping[str, Any]) -> tuple[int, int] | None:
    point = action_record.get("click_point")
    if isinstance(point, (list, tuple)) and len(point) == 2:
        try:
            return int(point[0]), int(point[1])
        except (TypeError, ValueError):
            return None
    bounds = _parse_bounds(action_record.get("bounds"))
    if bounds is None:
        return None
    x1, y1, x2, y2 = bounds
    return (x1 + x2) // 2, (y1 + y2) // 2


def _xml_hit_test_result_is_decisive(value: Mapping[str, Any]) -> bool:
    if value.get("snapped") is True and value.get("selected_node"):
        return True
    direct_hits = value.get("direct_hits")
    if isinstance(direct_hits, list) and direct_hits:
        return True
    if value.get("alignment_basis") == "direct_supported_hit":
        return True
    return value.get("rejection_reason") in {"wrong_target", "outside_target"}


def _node_supports_runtime_hit(node: Mapping[str, Any]) -> bool:
    attrs = node.get("attributes") if isinstance(node.get("attributes"), Mapping) else {}
    tag = str(node.get("tag") or "").casefold()
    class_text = " ".join(
        str(attrs.get(key) or "")
        for key in ("class", "type", "resource-id", "resourceId", "id", "key")
    ).casefold()
    if tag == "hierarchy" or class_text.strip() == "root":
        return False
    if node.get("visible") is False or node.get("enabled") is False:
        return False
    semantic = " ".join(
        str(value or "")
        for value in (node.get("text"), node.get("semantic_text"), class_text)
    ).strip()
    return bool(semantic or node.get("clickable"))


def _point_in_bounds(x: int, y: int, bounds: Any) -> bool:
    parsed = _parse_bounds(bounds)
    if parsed is None:
        return False
    x1, y1, x2, y2 = parsed
    return x1 <= x <= x2 and y1 <= y <= y2


def _node_area(node: Mapping[str, Any]) -> int:
    bounds = _parse_bounds(node.get("bounds"))
    if bounds is None:
        return sys.maxsize
    x1, y1, x2, y2 = bounds
    return max(1, x2 - x1) * max(1, y2 - y1)


def _runtime_hit_node_summary(node: Mapping[str, Any]) -> dict[str, Any]:
    attrs = node.get("attributes") if isinstance(node.get("attributes"), Mapping) else {}
    attributes = {
        key: str(attrs[key])
        for key in ("id", "resource-id", "resourceId", "class", "type", "clickable", "enabled")
        if key in attrs
    }
    return {
        "tag": node.get("tag"),
        "text": node.get("text"),
        "semantic_text": node.get("semantic_text"),
        "semantic_context": node.get("semantic_context"),
        "window_bundle_name": node.get("window_bundle_name"),
        "window_page_path": node.get("window_page_path"),
        "window_focused": node.get("window_focused"),
        "bounds": list(_parse_bounds(node.get("bounds")) or ()),
        "clickable": bool(node.get("clickable")),
        "enabled": node.get("enabled") is not False,
        "attributes": attributes,
    }


def _decision_target_element(decision: Mapping[str, Any]) -> str | None:
    params = decision.get("parameters")
    if isinstance(params, Mapping):
        value = params.get("target_element")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _observation_schedule(policy: Mapping[str, Any]) -> list[int]:
    max_wait = policy.get("max_wait_ms")
    max_wait_ms = max_wait if isinstance(max_wait, int) and not isinstance(max_wait, bool) else 5000
    values: list[int] = []
    if policy.get("immediate", True) is True:
        values.append(0)
    delays = policy.get("delays_ms")
    if isinstance(delays, list):
        for item in delays:
            if isinstance(item, int) and not isinstance(item, bool) and 0 <= item <= max_wait_ms:
                values.append(item)
    if not values:
        values.append(min(500, max_wait_ms))
    return sorted(dict.fromkeys(values))


def _stable_frames_required(policy: Mapping[str, Any]) -> int:
    """Return the bounded adaptive-capture stability threshold."""

    configured = policy.get("stable_frames_required", 1)
    if isinstance(configured, int) and not isinstance(configured, bool):
        return max(1, min(configured, 3))
    return 1


def _step_requires_full_observation_window(test_case: TestCaseSpec, step: TestStep) -> bool:
    """Outcome assertions need their full declared eventual-state window."""

    return any(assertion.after_step == step.step_id for assertion in test_case.expected_results)


def _frame_stability(
    previous: Mapping[str, Any] | None,
    current: Mapping[str, Any],
) -> str:
    if previous is None:
        return "UNKNOWN"
    previous_texts = tuple(previous.get("visible_texts", ()))
    current_texts = tuple(current.get("visible_texts", ()))
    if previous_texts == current_texts:
        return "STABLE"
    return "CHANGED"


def _observation_burst_summary(
    frames: list[dict[str, Any]],
    *,
    policy: Mapping[str, Any],
) -> dict[str, Any]:
    stabilities = [str(frame.get("stability") or "UNKNOWN") for frame in frames]
    return {
        "policy": dict(policy),
        "frame_ids": [frame["frame_id"] for frame in frames],
        "relative_to_action_ms": [frame["relative_to_action_ms"] for frame in frames],
        "stability_sequence": stabilities,
        "stable_frame_count": sum(item == "STABLE" for item in stabilities),
        "changed_within_burst": any(item == "CHANGED" for item in stabilities),
        "final_frame_id": frames[-1]["frame_id"] if frames else None,
        "stopped_early": bool(frames and frames[-1].get("observation_stop_reason")),
        "stop_reason": frames[-1].get("observation_stop_reason") if frames else None,
    }


def _environment_blocker_frame(frame: Mapping[str, Any]) -> str | None:
    texts = "\n".join(str(item) for item in frame.get("visible_texts", ())).casefold()
    for term in (
        "login",
        "log in",
        "sign in",
        "permission",
        "network",
        "offline",
        "retry",
        "no available way to open",
        "no available opener",
        "请先登录",
        "登录",
        "权限",
        "网络",
        "无网络",
        "未连接",
        "重试",
        "暂无可用打开方式",
    ):
        if term.casefold() in texts:
            return term
    nodes = frame.get("xml_nodes")
    if isinstance(nodes, list):
        joined_nodes = "\n".join(
            " ".join(
                str(node.get(key) or "")
                for key in ("tag", "text", "semantic_text")
            )
            for node in nodes
            if isinstance(node, Mapping)
        ).casefold()
        if "screenlockrootcomponent" in joined_nodes or "screenlock" in joined_nodes:
            return "screen_locked"
    return None


def _parse_bounds(value: Any) -> tuple[int, int, int, int] | None:
    if isinstance(value, (list, tuple)) and len(value) == 4:
        try:
            x1, y1, x2, y2 = (int(item) for item in value)
        except (TypeError, ValueError):
            return None
        return (x1, y1, x2, y2) if x2 > x1 and y2 > y1 else None
    text = str(value or "")
    import re

    match = re.search(r"\[\s*(-?\d+)\s*,\s*(-?\d+)\s*\]\[\s*(-?\d+)\s*,\s*(-?\d+)\s*\]", text)
    if not match:
        return None
    x1, y1, x2, y2 = (int(item) for item in match.groups())
    return (x1, y1, x2, y2) if x2 > x1 and y2 > y1 else None


def _declared_coordinate_target(
    target: Mapping[str, Any],
    *,
    fallback_label: str,
) -> dict[str, Any] | None:
    bounds = _parse_bounds(target.get("bounds"))
    if bounds is not None:
        x1, y1, x2, y2 = bounds
        center = ((x1 + x2) // 2, (y1 + y2) // 2)
    else:
        raw_center = target.get("coordinates", target.get("center"))
        if not isinstance(raw_center, (list, tuple)) or len(raw_center) != 2:
            return None
        try:
            x, y = (int(item) for item in raw_center)
        except (TypeError, ValueError):
            return None
        center = (x, y)
        bounds = (x, y, x, y)
    label = str(
        target.get("label")
        or target.get("text")
        or target.get("name")
        or fallback_label
    )
    return {
        "text": label,
        "bounds": bounds,
        "center": center,
        "source": "declared_coordinates",
    }


def _resolve_target(
    frame: Mapping[str, Any] | None,
    target: Mapping[str, Any],
    *,
    wants_text_input: bool,
) -> dict[str, Any] | None:
    if frame is None:
        return None
    nodes = frame.get("xml_nodes")
    if not isinstance(nodes, list):
        return None
    text_candidates = tuple(
        str(item).strip()
        for item in target.get("text_candidates", ())
        if str(item).strip()
    )
    role = str(target.get("role") or "").casefold()
    candidates = []
    for node in nodes:
        bounds = node.get("bounds")
        if not bounds:
            continue
        if node.get("visible") is False:
            continue
        node_text = str(node.get("text") or "")
        semantic_text = " ".join(
            item for item in (node_text, str(node.get("semantic_text") or "")) if item
        )
        attrs = node.get("attributes") if isinstance(node.get("attributes"), Mapping) else {}
        class_text = " ".join(
            str(attrs.get(key) or "")
            for key in ("class", "type", "resource-id", "resourceId", "id", "key")
        ).casefold()
        text_score = max(
            (2 if item and item in semantic_text else 0 for item in text_candidates),
            default=0,
        )
        role_score = 0
        if wants_text_input:
            # Prefer the editable leaf over an input-bar/container whose id
            # happens to contain "input".  This vocabulary covers platform
            # accessibility roles (not application-specific identifiers).
            if any(marker in class_text for marker in ("richeditor", "edittext", "textarea", "textfield")):
                role_score = 4
            elif any(marker in class_text for marker in ("edit", "input")):
                role_score = 2
        elif role == "button":
            role_score = 1 if node.get("clickable") or "button" in class_text else 0
        else:
            role_score = 1 if node.get("clickable") else 0
        if text_candidates and text_score == 0 and not wants_text_input:
            continue
        if wants_text_input and role_score == 0:
            continue
        score = text_score + role_score + (1 if node.get("enabled") else 0)
        x1, y1, x2, y2 = bounds
        candidates.append(
            (
                score,
                text_score,
                {
                    "text": node_text,
                    "bounds": bounds,
                    "center": ((x1 + x2) // 2, (y1 + y2) // 2),
                    "source": "hierarchy",
                    "hit_node": _runtime_hit_node_summary(node),
                },
            )
        )
    if not candidates:
        return None
    # When an editable field exposes one of the caller's labels/placeholders,
    # it is stronger evidence than a generic editable surface. This lets the
    # same selector distinguish title/body, username/password, etc. without
    # naming any application. If accessibility does not expose labels, retain
    # the generic editable-control fallback.
    if wants_text_input and text_candidates:
        labelled = [item for item in candidates if item[1] > 0]
        if labelled:
            candidates = labelled
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][2]


def _resolve_exact_text_target(
    frame: Mapping[str, Any] | None,
    target: Mapping[str, Any],
    *,
    wants_text_input: bool,
) -> dict[str, Any] | None:
    """Resolve one unambiguous, exact visible text target from the UI hierarchy.

    This deliberately does not guess from a partial text match and does not
    attempt to locate text inputs: those controls often expose a placeholder
    rather than their editable surface.  It is safe for ordinary text-labelled
    rows, tabs, menus and buttons, including a non-clickable Text child inside
    a clickable container.
    """

    if frame is None or wants_text_input:
        return None
    nodes = frame.get("xml_nodes")
    if not isinstance(nodes, list):
        return None
    requested = tuple(
        _normalize_target_text(item)
        for item in target.get("text_candidates", ())
        if _normalize_target_text(item)
    )
    if not requested:
        return None
    matches: list[dict[str, Any]] = []
    for node in nodes:
        if not isinstance(node, Mapping) or node.get("visible") is False or node.get("enabled") is False:
            continue
        bounds = _parse_bounds(node.get("bounds"))
        node_text = _normalize_target_text(node.get("text"))
        if bounds is None or not node_text or node_text not in requested:
            continue
        click_node = _smallest_clickable_container(nodes, bounds)
        click_bounds = _parse_bounds(click_node.get("bounds")) if click_node is not None else bounds
        assert click_bounds is not None
        x1, y1, x2, y2 = click_bounds
        matches.append(
            {
                "text": str(node.get("text") or "").strip(),
                "bounds": click_bounds,
                "center": ((x1 + x2) // 2, (y1 + y2) // 2),
                "source": "hierarchy_exact_text",
                "hit_node": _runtime_hit_node_summary(click_node or node),
                "clickable": bool((click_node or node).get("clickable")),
                "matched_text_node": _runtime_hit_node_summary(node),
            }
        )
    if not matches:
        return None
    unique_bounds = {tuple(item["bounds"]) for item in matches}
    if len(unique_bounds) == 1:
        return max(matches, key=lambda item: int(item["clickable"]))
    clickable = [item for item in matches if item["clickable"]]
    if len(clickable) == 1:
        return clickable[0]
    # Multiple identical visible labels are ambiguous; keep the model path
    # available instead of selecting an arbitrary candidate.
    return None


def _smallest_clickable_container(
    nodes: list[Any],
    target_bounds: tuple[int, int, int, int],
) -> Mapping[str, Any] | None:
    """Find the nearest practical clickable ancestor from flat UI hierarchy data.

    Accessibility dumps are often flattened: the visible label itself is a
    non-clickable Text child, while its tappable row/button is a containing
    sibling/ancestor. Clicking the container centre is more tolerant to edge
    taps than clicking the text glyph centre. Large page-level containers are
    excluded so this remains a control-level, not screen-level, heuristic.
    """

    target_area = max(1, _bounds_area(target_bounds))
    candidates: list[tuple[int, Mapping[str, Any]]] = []
    for node in nodes:
        if not isinstance(node, Mapping) or not node.get("clickable") or node.get("visible") is False:
            continue
        bounds = _parse_bounds(node.get("bounds"))
        if bounds is None or not _bounds_contains(bounds, target_bounds):
            continue
        area = _bounds_area(bounds)
        height = bounds[3] - bounds[1]
        if area > target_area * 60 or height > max(260, (target_bounds[3] - target_bounds[1]) * 5):
            continue
        candidates.append((area, node))
    return min(candidates, key=lambda item: item[0])[1] if candidates else None


def _bounds_contains(
    outer: tuple[int, int, int, int],
    inner: tuple[int, int, int, int],
) -> bool:
    return outer[0] <= inner[0] and outer[1] <= inner[1] and outer[2] >= inner[2] and outer[3] >= inner[3]


def _resolve_decider_aligned_text_target(
    frame: Mapping[str, Any] | None,
    target: Mapping[str, Any],
    decision: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Use a Decider's intent to disambiguate repeated accessible labels.

    A page can legitimately expose the same label in a bottom navigation bar,
    a tab and a list item.  Exact-text selection correctly refuses that
    ambiguity.  When the Decider's *own* bounding box is close to one of these
    visible labels, however, the hierarchy supplies a more reliable final
    click point than a second visual Grounder.  This is generic reconciliation
    of model intent and accessibility evidence, not an app-specific rule.
    """

    if frame is None:
        return None
    params = decision.get("parameters")
    params = params if isinstance(params, Mapping) else {}
    raw_bbox = params.get("bbox")
    if not isinstance(raw_bbox, (list, tuple)) or len(raw_bbox) != 4:
        return None
    try:
        # The MobiAgent Decider bbox protocol uses a 540x1222 coordinate
        # space; the original handler converts it to native device space by 2.
        x1, y1, x2, y2 = (int(item) * 2 for item in raw_bbox)
    except (TypeError, ValueError):
        return None
    if x2 <= x1 or y2 <= y1:
        return None
    requested = tuple(
        _normalize_target_text(item)
        for item in target.get("text_candidates", ())
        if _normalize_target_text(item)
    )
    if not requested:
        return None
    nodes = frame.get("xml_nodes")
    if not isinstance(nodes, list):
        return None
    model_center = ((x1 + x2) // 2, (y1 + y2) // 2)
    candidates: list[tuple[int, int, dict[str, Any]]] = []
    seen_bounds: set[tuple[int, int, int, int]] = set()
    for node in nodes:
        if not isinstance(node, Mapping) or node.get("visible") is False or node.get("enabled") is False:
            continue
        bounds = _parse_bounds(node.get("bounds"))
        text = _normalize_target_text(node.get("text"))
        if bounds is None or text not in requested or bounds in seen_bounds:
            continue
        seen_bounds.add(bounds)
        click_node = _smallest_clickable_container(nodes, bounds)
        click_bounds = _parse_bounds(click_node.get("bounds")) if click_node is not None else bounds
        assert click_bounds is not None
        cx, cy = ((click_bounds[0] + click_bounds[2]) // 2, (click_bounds[1] + click_bounds[3]) // 2)
        distance = abs(model_center[0] - cx) + abs(model_center[1] - cy)
        overlap_x = max(0, min(x2, click_bounds[2]) - max(x1, click_bounds[0]))
        overlap_y = max(0, min(y2, click_bounds[3]) - max(y1, click_bounds[1]))
        candidates.append(
            (
                distance,
                overlap_x * overlap_y,
                {
                    "text": str(node.get("text") or "").strip(),
                    "bounds": click_bounds,
                    "center": (cx, cy),
                    "source": "hierarchy_decider_aligned_text",
                    "hit_node": _runtime_hit_node_summary(click_node or node),
                    "clickable": bool((click_node or node).get("clickable")),
                    "matched_text_node": _runtime_hit_node_summary(node),
                },
            )
        )
    if not candidates:
        return None
    candidates.sort(key=lambda item: (-item[1], item[0], -int(item[2]["clickable"])))
    distance, overlap, selected = candidates[0]
    # Avoid using a vague model bbox to select an unrelated repeated label.
    target_width = max(1, x2 - x1)
    target_height = max(1, y2 - y1)
    if overlap <= 0 and distance > max(160, target_width + target_height):
        return None
    return selected


def _prefer_exact_hierarchy_target(step: TestStep) -> bool:
    """Select the deterministic hierarchy path only for identity-critical targets.

    Explicit decider/locator injection remains authoritative for test fixtures
    and integration users.  For default real-device execution, conversation
    and contact selection change the scope of every later action, so an exact
    accessible-text selector is safer than a vision bbox.  Other target kinds
    may opt in declaratively with ``prefer_hierarchy_exact_text``.
    """

    target = step.target if isinstance(step.target, Mapping) else {}
    if target.get("prefer_hierarchy_exact_text") is True:
        return True
    role = str(target.get("role") or "").casefold()
    return role in {"conversation", "contact", "chat", "thread"}


def _resolve_hierarchy_control_target(
    frame: Mapping[str, Any] | None,
    step: TestStep,
    *,
    wants_text_input: bool,
) -> dict[str, Any] | None:
    """Return a deterministic accessible control when it is safe to do so.

    The model remains responsible for ambiguous visual targets and for
    exploratory recovery.  This selector only supersedes it where the current
    hierarchy proves the intended control: an identity-critical exact text
    destination, an editable text control for INPUT, or a uniquely-labelled
    button.  It is deliberately driven by generic roles/classes rather than
    application names, labels, or screen coordinates.
    """

    target = step.target if isinstance(step.target, Mapping) else {}
    if wants_text_input:
        resolved = _resolve_target(frame, target, wants_text_input=True)
        if resolved is not None:
            resolved["source"] = "hierarchy_text_input"
        return resolved
    exact = _resolve_exact_text_target(frame, target, wants_text_input=False)
    if exact is None:
        return None
    if _prefer_exact_hierarchy_target(step):
        return exact
    role = str(target.get("role") or "").casefold()
    if role == "button":
        exact["source"] = "hierarchy_exact_button_text"
        return exact
    return None


def _normalize_target_text(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


def _post_action_context_spec(step: TestStep) -> dict[str, Any] | None:
    target = step.target if isinstance(step.target, Mapping) else {}
    raw = target.get("post_action_context")
    if isinstance(raw, Mapping):
        candidates = raw.get("text_candidates", raw.get("texts", ()))
        if isinstance(candidates, str):
            candidates = [candidates]
        if isinstance(candidates, (list, tuple)):
            cleaned = [str(item).strip() for item in candidates if str(item).strip()]
            if cleaned:
                return {
                    "text_candidates": cleaned,
                    "required": bool(raw.get("required", True)),
                    "source": "declared_post_action_context",
                }
    # A conversation/contact selection is a state transition.  Its declared
    # target text is also the expected title/context of the destination page.
    # This uses the existing generic role vocabulary, not an app-specific rule.
    role = str(target.get("role") or "").casefold()
    if role in {"conversation", "contact", "chat", "thread"}:
        candidates = target.get("text_candidates", ())
        if isinstance(candidates, (list, tuple)):
            cleaned = [str(item).strip() for item in candidates if str(item).strip()]
            if cleaned:
                return {
                    "text_candidates": cleaned,
                    "required": True,
                    "source": "role_derived_destination_context",
                }
    return None


def _evaluate_post_action_context(
    step: TestStep,
    post_frames: tuple[Mapping[str, Any], ...],
) -> dict[str, Any] | None:
    spec = _post_action_context_spec(step)
    if spec is None:
        return None
    final_frame = post_frames[-1] if post_frames else None
    visible_texts = (
        [str(item) for item in final_frame.get("visible_texts", ()) if str(item).strip()]
        if isinstance(final_frame, Mapping)
        else []
    )
    candidates = list(spec["text_candidates"])
    matched = [
        candidate
        for candidate in candidates
        if any(_normalize_target_text(candidate) in _normalize_target_text(text) for text in visible_texts)
    ]
    if matched:
        status = "CONFORMANT"
    elif not visible_texts:
        status = "UNKNOWN"
    elif spec["required"]:
        status = "NON_CONFORMANT"
    else:
        status = "UNKNOWN"
    return {
        "status": status,
        "source": spec["source"],
        "required": spec["required"],
        "text_candidates": candidates,
        "matched_candidates": matched,
        "frame_id": final_frame.get("frame_id") if isinstance(final_frame, Mapping) else None,
        "visible_texts": visible_texts,
    }


def _evaluate_input_effect(
    step: TestStep,
    action_record: Mapping[str, Any],
    post_frames: tuple[Mapping[str, Any], ...],
) -> dict[str, Any] | None:
    """Confirm that an INPUT step changed the editable surface before send.

    Dispatching text through HDC/ADB proves neither focus nor delivery.  The
    accessibility observation is therefore the completion evidence for an
    input step; if it is absent, orchestration must not advance to a button
    that could submit stale or empty content.
    """

    expected_value = action_record.get("text")
    if step.action_type != "INPUT" or not isinstance(expected_value, str) or not expected_value:
        return None
    final_frame = post_frames[-1] if post_frames else None
    visible_texts = (
        [str(item) for item in final_frame.get("visible_texts", ()) if str(item).strip()]
        if isinstance(final_frame, Mapping)
        else []
    )
    if any(expected_value in text for text in visible_texts):
        status = "CONFORMANT"
    elif visible_texts:
        status = "NON_CONFORMANT"
    else:
        status = "UNKNOWN"
    return {
        "status": status,
        "expected_value": expected_value,
        "frame_id": final_frame.get("frame_id") if isinstance(final_frame, Mapping) else None,
        "visible_texts": visible_texts,
        "reason": (
            "expected input value observed in the post-action UI"
            if status == "CONFORMANT"
            else "expected input value was not observed after text dispatch"
        ),
    }


def _model_target_locator(
    step: TestStep,
    test_case: TestCaseSpec,
    current_frame: Mapping[str, Any],
    wants_text_input: bool,
) -> Mapping[str, Any] | None:
    screenshot_path = current_frame.get("screenshot_abs")
    if not isinstance(screenshot_path, str) or not Path(screenshot_path).is_file():
        return None
    try:
        config = model_config_from_env(
            base_url_names=(
                "MOBIAGENT_GROUNDER_BASE_URL",
                "MOBIAGENT_BASE_URL",
            ),
            model_names=(
                "MOBIAGENT_GROUNDER_MODEL",
                "MOBIAGENT_MODEL",
            ),
        )
        with Image.open(screenshot_path) as image:
            width, height = image.size
        image_b64 = base64.b64encode(Path(screenshot_path).read_bytes()).decode("ascii")
        prompt = (
            "You are locating one control on a real mobile screenshot for an automated "
            "App test step. Return JSON only. Do not decide whether the App test passed.\n"
            f"App: {test_case.app_under_test.name}\n"
            f"Step id: {step.step_id}\n"
            f"Instruction: {step.instruction}\n"
            f"Action type: {step.action_type}\n"
            f"Target spec: {json.dumps(dict(step.target), ensure_ascii=False)}\n"
            f"Need text input control: {wants_text_input}\n"
            f"Screenshot size: {width}x{height}\n"
            "Return exactly: {\"x\": integer absolute pixel x, \"y\": integer absolute pixel y, "
            "\"target_element\": short label, \"confidence\": number 0..1, \"reason\": short string}. "
            "If no suitable target is visible, return {\"x\": null, \"y\": null, "
            "\"target_element\": \"\", \"confidence\": 0, \"reason\": \"not visible\"}."
        )
        body = post_chat_completion(
            config,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"},
                        },
                        {"type": "text", "text": prompt},
                    ],
                }
            ],
            max_tokens=256,
        )
        parsed = extract_json_object(body)
        if parsed.get("x") is None or parsed.get("y") is None:
            return None
        return parsed
    except Exception as exc:  # noqa: BLE001
        raise _TargetNotFound(f"vision target locator failed: {type(exc).__name__}: {exc}") from exc
