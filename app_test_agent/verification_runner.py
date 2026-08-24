"""Constrained read-only runner for result evidence verification.

The verification runner is not a second business execution runner.  It may only
collect additional observations after conformant test execution, and its trace
is kept separate from the declared business steps.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from pathlib import Path
import time
from typing import Any, Callable, Mapping, Protocol

from PIL import Image

from .contract import AppTestContract
from .executor import EvidenceState, ExecutionRecord
from .model_client import extract_json_object, model_config_from_env, post_chat_completion
from .mobiagent_executor import (
    _declared_coordinate_target,
    _file_sha256,
    _parse_hierarchy_dump,
    _resolve_target,
    _visible_texts,
)
from .schema import READ_ONLY_VERIFICATION_ACTION_TYPES, TestCaseSpec
from .verification_intent import compile_verification_intent, effective_verification_steps


class VerificationRunStatus:
    NOT_RUN = "NOT_RUN"
    COMPLETED = "COMPLETED"
    ROUTE_FAILED = "ROUTE_FAILED"
    ENV_BLOCKED = "ENV_BLOCKED"
    UNSUPPORTED = "UNSUPPORTED"


REAL_VERIFICATION_ACTION_TYPES = frozenset(
    {"NAVIGATE", "WAIT", "BACK", "REFRESH", "SCROLL", "OBSERVE"}
)
DANGEROUS_VERIFICATION_TERMS = (
    "publish",
    "submit",
    "send",
    "delete",
    "remove",
    "like",
    "pay",
    "payment",
    "order",
    "checkout",
    "comment",
    "edit",
    "input",
    "save",
    "create post",
    "new post",
    "发帖",
    "发文",
    "发布",
    "发送",
    "提交",
    "删除",
    "移除",
    "点赞",
    "赞",
    "支付",
    "付款",
    "下单",
    "评论",
    "编辑",
    "输入",
    "保存",
)
ENV_BLOCKER_TERMS = (
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
)


@dataclass(frozen=True)
class VerificationStepResult:
    verification_step_id: str
    status: str
    action_type: str
    attempts: int = 1
    target: Mapping[str, Any] = field(default_factory=dict)
    observation_frames: tuple[int, ...] = ()
    reached_surface: bool | None = None
    blocker: str | None = None
    error: str | None = None
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "verification_step_id": self.verification_step_id,
            "status": self.status,
            "action_type": self.action_type,
            "attempts": self.attempts,
            "target": dict(self.target),
            "observation_frames": list(self.observation_frames),
            "reached_surface": self.reached_surface,
            "blocker": self.blocker,
            "error": self.error,
            "evidence": dict(self.evidence),
        }


@dataclass(frozen=True)
class VerificationRunResult:
    status: str
    used_runner: bool
    reason: str
    target_surface: str | None = None
    reached_surface: bool = False
    observation_sufficient: bool = False
    step_results: tuple[VerificationStepResult, ...] = ()
    observation_record: ExecutionRecord | None = None
    contract_sha256: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "used_runner": self.used_runner,
            "reason": self.reason,
            "target_surface": self.target_surface,
            "reached_surface": self.reached_surface,
            "observation_sufficient": self.observation_sufficient,
            "contract_sha256": self.contract_sha256,
            "step_results": [item.as_dict() for item in self.step_results],
            "observation_record": (
                self.observation_record.as_dict()
                if self.observation_record is not None
                else None
            ),
            "metadata": dict(self.metadata),
        }


class VerificationRunner(Protocol):
    name: str

    def execute(
        self,
        *,
        test_case: TestCaseSpec,
        business_execution: ExecutionRecord,
        contract: AppTestContract,
    ) -> VerificationRunResult: ...


@dataclass(frozen=True)
class ScriptedVerificationRunner:
    """Offline verification runner used by manifests, mocks, and unit tests.

    The runner reads an optional test-case metadata block:

    ``metadata.verification_runner.scenario`` may be one of ``found``,
    ``not_found``, ``route_failed``, ``env_blocked``, or ``unsupported``.
    ``visible_texts`` can provide explicit observation text for the completed
    route.  This keeps v1 deterministic and avoids device/model calls.
    """

    scenario: str | None = None
    visible_texts: tuple[str, ...] | None = None
    observation_sufficient: bool | None = None
    name: str = "scripted_verification"

    def execute(
        self,
        *,
        test_case: TestCaseSpec,
        business_execution: ExecutionRecord,
        contract: AppTestContract,
    ) -> VerificationRunResult:
        del business_execution
        intent = compile_verification_intent(test_case)
        verification_steps = effective_verification_steps(test_case)
        if not verification_steps:
            return VerificationRunResult(
                status=VerificationRunStatus.NOT_RUN,
                used_runner=False,
                reason="test case has no explicit verification_steps and no observable verification intent",
                contract_sha256=contract.sha256,
            )

        unsupported = next(
            (
                step
                for step in verification_steps
                if step.action_type not in READ_ONLY_VERIFICATION_ACTION_TYPES
            ),
            None,
        )
        if unsupported is not None:
            result = VerificationStepResult(
                verification_step_id=unsupported.verification_step_id,
                status=VerificationRunStatus.UNSUPPORTED,
                action_type=unsupported.action_type,
                attempts=0,
                target=unsupported.target,
                error=(
                    f"verification action {unsupported.action_type} is not read-only "
                    "and cannot be executed"
                ),
            )
            return VerificationRunResult(
                status=VerificationRunStatus.UNSUPPORTED,
                used_runner=True,
                reason=result.error or "unsupported verification action",
                target_surface=_target_surface(test_case),
                step_results=(result,),
                contract_sha256=contract.sha256,
            )

        config = _metadata_config(test_case)
        scenario = (self.scenario or str(config.get("scenario") or "route_failed")).lower()
        explicit_texts = self.visible_texts
        if explicit_texts is None and isinstance(config.get("visible_texts"), list):
            explicit_texts = tuple(str(item) for item in config["visible_texts"] if str(item))
        sufficient = self.observation_sufficient
        if sufficient is None:
            sufficient = bool(config.get("observation_sufficient", scenario in {"found", "not_found"}))

        if scenario == "env_blocked":
            return self._blocked(
                test_case,
                contract,
                status=VerificationRunStatus.ENV_BLOCKED,
                reason=str(config.get("reason") or "environment blocked verification route"),
                blocker=str(config.get("blocker") or "verification_environment_blocker"),
            )
        if scenario == "unsupported":
            return self._blocked(
                test_case,
                contract,
                status=VerificationRunStatus.UNSUPPORTED,
                reason=str(config.get("reason") or "verification route is unsupported"),
            )
        if scenario == "route_failed":
            return self._blocked(
                test_case,
                contract,
                status=VerificationRunStatus.ROUTE_FAILED,
                reason=str(config.get("reason") or "verification runner could not reach target surface"),
            )
        if scenario not in {"found", "not_found"}:
            return self._blocked(
                test_case,
                contract,
                status=VerificationRunStatus.ROUTE_FAILED,
                reason=f"unknown scripted verification scenario: {scenario}",
            )

        texts = tuple(explicit_texts or ())
        if scenario == "found" and not texts:
            texts = tuple(
                dict.fromkeys(
                    value
                    for value in (
                        assertion.resolved_value(test_case.test_data)
                        for assertion in test_case.expected_results
                        if assertion.type == "TEXT_VISIBLE"
                    )
                    if value is not None
                )
            )
        if scenario == "not_found" and not texts:
            texts = ("Feed",)
        surface_markers = _surface_marker_texts(test_case, verification_steps)
        if scenario in {"found", "not_found"} and surface_markers:
            texts = tuple(dict.fromkeys((*surface_markers, *texts)))

        frames = [
            _verification_frame(
                index,
                texts if index == len(verification_steps) else ("Feed",),
            )
            for index, _step in enumerate(verification_steps, 1)
        ]
        if scenario == "not_found" and sufficient and frames:
            terminal = dict(frames[-1])
            terminal["relative_to_action_ms"] = 0
            frames[-1] = terminal
            next_frame_id = int(terminal["frame_id"]) + 1
            raw_delays = test_case.observation_policy.get("delays_ms", ())
            max_wait = test_case.observation_policy.get("max_wait_ms", 0)
            for delay in sorted(
                {
                    value
                    for value in raw_delays
                    if isinstance(value, int)
                    and not isinstance(value, bool)
                    and isinstance(max_wait, int)
                    and 0 <= value <= max_wait
                }
            ) if isinstance(raw_delays, (list, tuple)) else ():
                frames.append(
                    {
                        **_verification_frame(next_frame_id, texts),
                        "relative_to_action_ms": delay,
                    }
                )
                next_frame_id += 1
        frame_texts = {str(frame["frame_id"]): list(frame["visible_texts"]) for frame in frames}
        step_results = tuple(
            VerificationStepResult(
                verification_step_id=step.verification_step_id,
                status=VerificationRunStatus.COMPLETED,
                action_type=step.action_type,
                attempts=1,
                target=step.target,
                observation_frames=(
                    tuple(frame["frame_id"] for frame in frames[index:])
                    if index == len(verification_steps) - 1
                    else (frames[index]["frame_id"],)
                ),
                reached_surface=index == len(verification_steps) - 1,
                evidence={
                    "read_only_action": True,
                    "instruction": step.instruction,
                    "generated_from_verification_intent": not bool(test_case.verification_steps),
                },
            )
            for index, step in enumerate(verification_steps)
        )
        record = ExecutionRecord(
            test_case_id=test_case.test_case_id,
            executor=self.name,
            step_results=(),
            final_state=EvidenceState(
                visible_texts=texts,
                evidence_sufficient=bool(sufficient),
                notes=("verification runner observation",),
            ),
            metadata={
                "verification_runner": self.name,
                "target_surface": _target_surface(test_case),
                "reached_surface": True,
                "observation_sufficient": bool(sufficient),
                "verification_intent": intent.as_dict(),
                "verification_intent_sha256": intent.sha256,
                "generated_verification_steps": not bool(test_case.verification_steps),
                "frames": frames,
                "frame_visible_texts": frame_texts,
            },
        )
        return VerificationRunResult(
            status=VerificationRunStatus.COMPLETED,
            used_runner=True,
            reason="verification runner reached target surface and collected observations",
            target_surface=_target_surface(test_case),
            reached_surface=True,
            observation_sufficient=bool(sufficient),
            step_results=step_results,
            observation_record=record,
            contract_sha256=contract.sha256,
            metadata={
                "scenario": scenario,
                "verification_intent": intent.as_dict(),
                "verification_intent_sha256": intent.sha256,
                "generated_verification_steps": not bool(test_case.verification_steps),
            },
        )

    def _blocked(
        self,
        test_case: TestCaseSpec,
        contract: AppTestContract,
        *,
        status: str,
        reason: str,
        blocker: str | None = None,
    ) -> VerificationRunResult:
        verification_steps = effective_verification_steps(test_case)
        if not verification_steps:
            return VerificationRunResult(
                status=VerificationRunStatus.NOT_RUN,
                used_runner=False,
                reason="test case has no observable verification intent",
                target_surface=_target_surface(test_case),
                reached_surface=False,
                observation_sufficient=False,
                contract_sha256=contract.sha256,
            )
        first = verification_steps[0]
        step_status = (
            VerificationRunStatus.ENV_BLOCKED
            if status == VerificationRunStatus.ENV_BLOCKED
            else status
        )
        step = VerificationStepResult(
            verification_step_id=first.verification_step_id,
            status=step_status,
            action_type=first.action_type,
            attempts=1 if status != VerificationRunStatus.UNSUPPORTED else 0,
            target=first.target,
            blocker=blocker,
            error=reason,
        )
        return VerificationRunResult(
            status=status,
            used_runner=True,
            reason=reason,
            target_surface=_target_surface(test_case),
            reached_surface=False,
            observation_sufficient=False,
            step_results=(step,),
            contract_sha256=contract.sha256,
        )


def _metadata_config(test_case: TestCaseSpec) -> Mapping[str, Any]:
    raw = test_case.metadata.get("verification_runner")
    return dict(raw) if isinstance(raw, Mapping) else {}


def _target_surface(test_case: TestCaseSpec) -> str | None:
    for assertion in test_case.expected_results:
        if assertion.required and assertion.surface:
            return assertion.surface
    for assertion in test_case.expected_results:
        if assertion.surface:
            return assertion.surface
    return None


def _surface_marker_texts(
    test_case: TestCaseSpec,
    verification_steps: tuple[Any, ...],
) -> tuple[str, ...]:
    values: list[str] = []
    for step in verification_steps:
        target = step.target
        if not isinstance(target, Mapping):
            continue
        for key in ("surface_text_candidates", "text_candidates"):
            raw = target.get(key)
            if isinstance(raw, list):
                values.extend(str(item).strip() for item in raw if str(item).strip())
        for key in (
            "required_surface_text_groups",
            "surface_text_groups",
            "surface_shape_text_groups",
            "required_text_groups",
        ):
            raw_groups = target.get(key)
            if not isinstance(raw_groups, list):
                continue
            for group in raw_groups:
                if isinstance(group, list):
                    values.extend(
                        str(item).strip() for item in group if str(item).strip()
                    )
                elif isinstance(group, str) and group.strip():
                    values.append(group.strip())
    surface = _target_surface(test_case)
    folded = str(surface or "").casefold()
    if any(term in folded for term in ("feed", "timeline", "列表", "动态")):
        values.extend(["Feed", "列表", "动态"])
    if any(term in folded for term in ("post", "note", "笔记", "帖子", "内容")):
        values.extend(["Post", "Notes", "笔记", "帖子", "内容"])
    if any(term in folded for term in ("profile", "personal", "mine", "my", "own", "主页", "我的", "个人")):
        values.extend(["Profile", "Me", "Mine", "我", "我的", "个人主页"])
    return tuple(dict.fromkeys(item for item in values if item))


def _verification_frame(frame_id: int, visible_texts: tuple[str, ...]) -> dict[str, Any]:
    import hashlib

    seed = f"verification:{frame_id}:{'|'.join(visible_texts)}".encode("utf-8")
    digest = hashlib.sha256(seed).hexdigest()
    return {
        "frame_id": frame_id,
        "timestamp_ms": frame_id * 1000,
        "relative_to_action_ms": 500,
        "screenshot": f"mock://verification/frame/{frame_id}.png",
        "screenshot_sha256": digest,
        "hierarchy": f"mock://verification/frame/{frame_id}.xml",
        "hierarchy_sha256": hashlib.sha256((digest + ":xml").encode("utf-8")).hexdigest(),
        "stability": "STABLE",
        "visible_texts": list(visible_texts),
    }


@dataclass
class MobiAgentVerificationRunner:
    """Real-device, constrained read-only verification runner.

    This runner reconnects to the same Android/Harmony device after business
    execution and collects a separate verification trace.  It never emits an
    App verdict; the App behavior oracle consumes its observation frames.
    """

    output_dir: Path
    device: str = "Harmony"
    device_serial: str | None = None
    runner_root: Path | None = None
    device_instance: Any | None = None
    target_locator: Callable[[Mapping[str, Any], TestCaseSpec, Mapping[str, Any]], Mapping[str, Any] | None] | None = None
    name: str = "mobiagent_real_verification"

    def execute(
        self,
        *,
        test_case: TestCaseSpec,
        business_execution: ExecutionRecord,
        contract: AppTestContract,
    ) -> VerificationRunResult:
        del business_execution
        trace_dir = self.output_dir.resolve() / "mobiagent_verification_trace"
        trace_dir.mkdir(parents=True, exist_ok=True)
        start_time = time.monotonic()
        policy = dict(test_case.verification_policy)
        timeout_seconds = float(policy.get("timeout_seconds", 30.0))
        retry_budget = int(policy.get("max_retries", 0))
        intent = compile_verification_intent(test_case)
        verification_steps = effective_verification_steps(test_case)
        max_steps = int(policy.get("max_steps", len(verification_steps)))
        if not verification_steps:
            return VerificationRunResult(
                status=VerificationRunStatus.NOT_RUN,
                used_runner=False,
                reason="test case has no explicit verification_steps and no observable verification intent",
                contract_sha256=contract.sha256,
            )
        if len(verification_steps) > max_steps:
            return VerificationRunResult(
                status=VerificationRunStatus.UNSUPPORTED,
                used_runner=True,
                reason="effective verification_steps length exceeds verification_policy.max_steps",
                contract_sha256=contract.sha256,
            )
        unsupported_reason = _first_unsupported_real_step(test_case, verification_steps)
        if unsupported_reason is not None:
            return VerificationRunResult(
                status=VerificationRunStatus.UNSUPPORTED,
                used_runner=True,
                reason=unsupported_reason,
                target_surface=_target_surface(test_case),
                contract_sha256=contract.sha256,
            )

        frames: list[dict[str, Any]] = []
        frame_visible_texts: dict[str, list[str]] = {}
        actions: list[dict[str, Any]] = []
        step_results: list[VerificationStepResult] = []
        next_frame_id = 1
        try:
            device = self.device_instance or self._connect_device()
            initial = self._capture_frame(
                device,
                trace_dir,
                frame_id=0,
                relative_to_action_ms=0,
            )
            frames.append(initial)
            frame_visible_texts["0"] = list(initial["visible_texts"])
            blocker = _environment_blocker_frame(initial)
            if blocker is not None:
                self._write_actions(trace_dir, test_case, actions)
                return self._env_blocked_result(
                    test_case,
                    contract,
                    reason=f"environment blocked verification before route started: {blocker}",
                    blocker=blocker,
                    frames=frames,
                    frame_visible_texts=frame_visible_texts,
                    step_results=tuple(step_results),
                    trace_dir=trace_dir,
                )
        except Exception as exc:  # noqa: BLE001
            return VerificationRunResult(
                status=VerificationRunStatus.ENV_BLOCKED,
                used_runner=True,
                reason=f"verification device setup failed: {type(exc).__name__}: {exc}",
                target_surface=_target_surface(test_case),
                contract_sha256=contract.sha256,
            )

        for index, step in enumerate(verification_steps, 1):
            if time.monotonic() - start_time > timeout_seconds:
                step_results.append(
                    _failed_verification_step(
                        step,
                        status=VerificationRunStatus.ROUTE_FAILED,
                        error="verification_policy.timeout_seconds was exceeded",
                    )
                )
                break
            attempts_allowed = 1 + min(step.max_retries, retry_budget)
            last_error: str | None = None
            completed = False
            for attempt in range(1, attempts_allowed + 1):
                try:
                    action = self._execute_one_step(
                        device,
                        step.as_dict(),
                        test_case,
                        current_frame=frames[-1] if frames else None,
                    )
                    actions.append(
                        {
                            **action,
                            "verification_step_id": step.verification_step_id,
                            "attempt": attempt,
                            "action_index": len(actions) + 1,
                        }
                    )
                    post = self._capture_frame(
                        device,
                        trace_dir,
                        frame_id=next_frame_id,
                        relative_to_action_ms=500,
                    )
                    next_frame_id += 1
                    frames.append(post)
                    frame_visible_texts[str(post["frame_id"])] = list(post["visible_texts"])
                    blocker = _environment_blocker_frame(post)
                    if blocker is not None:
                        step_results.append(
                            VerificationStepResult(
                                verification_step_id=step.verification_step_id,
                                status=VerificationRunStatus.ENV_BLOCKED,
                                action_type=step.action_type,
                                attempts=attempt,
                                target=step.target,
                                observation_frames=(post["frame_id"],),
                                blocker=blocker,
                                error=f"environment blocker observed: {blocker}",
                                evidence=action,
                            )
                        )
                        self._write_actions(trace_dir, test_case, actions)
                        return self._env_blocked_result(
                            test_case,
                            contract,
                            reason=f"environment blocked verification route: {blocker}",
                            blocker=blocker,
                            frames=frames,
                            frame_visible_texts=frame_visible_texts,
                            step_results=tuple(step_results),
                            trace_dir=trace_dir,
                        )
                    step_results.append(
                        VerificationStepResult(
                            verification_step_id=step.verification_step_id,
                            status=VerificationRunStatus.COMPLETED,
                            action_type=step.action_type,
                            attempts=attempt,
                            target=step.target,
                            observation_frames=(post["frame_id"],),
                            reached_surface=_surface_reached(step.target, post["visible_texts"]),
                            evidence=action,
                        )
                    )
                    completed = True
                    break
                except _VerificationRouteBlocked as exc:
                    blocker = str(exc)
                    step_results.append(
                        _failed_verification_step(
                            step,
                            status=VerificationRunStatus.ENV_BLOCKED,
                            error=blocker,
                            blocker=blocker,
                            attempts=attempt,
                        )
                    )
                    self._write_actions(trace_dir, test_case, actions)
                    return self._env_blocked_result(
                        test_case,
                        contract,
                        reason=blocker,
                        blocker=blocker,
                        frames=frames,
                        frame_visible_texts=frame_visible_texts,
                        step_results=tuple(step_results),
                        trace_dir=trace_dir,
                    )
                except Exception as exc:  # noqa: BLE001
                    last_error = f"{type(exc).__name__}: {exc}"
                    if attempt <= retry_budget:
                        retry_budget -= 1
                        continue
            if not completed:
                step_results.append(
                    _failed_verification_step(
                        step,
                        status=VerificationRunStatus.ROUTE_FAILED,
                        error=last_error or "verification step did not complete",
                        attempts=attempts_allowed,
                    )
                )
                break

        self._write_actions(trace_dir, test_case, actions)
        final_frame = frames[-1] if frames else {}
        final_texts = tuple(str(item) for item in final_frame.get("visible_texts", ()) if str(item))
        reached_surface = _route_reached_surface(verification_steps, tuple(step_results), final_texts)
        observation_sufficient = bool(reached_surface and frames and final_frame.get("hierarchy_sha256"))
        observation_record = ExecutionRecord(
            test_case_id=test_case.test_case_id,
            executor=self.name,
            step_results=(),
            final_state=EvidenceState(
                visible_texts=final_texts,
                evidence_sufficient=observation_sufficient,
                notes=("mobiagent real verification runner observation",),
            ),
            raw_trace_dir=str(trace_dir),
            metadata={
                "verification_runner": self.name,
                "target_surface": _target_surface(test_case),
                "reached_surface": reached_surface,
                "observation_sufficient": observation_sufficient,
                "verification_intent": intent.as_dict(),
                "verification_intent_sha256": intent.sha256,
                "generated_verification_steps": not bool(test_case.verification_steps),
                "frames": frames,
                "frame_visible_texts": frame_visible_texts,
                "actions_path": str(trace_dir / "verification_actions.json"),
            },
        )
        all_steps_completed = len(step_results) == len(verification_steps) and all(
            result.status == VerificationRunStatus.COMPLETED for result in step_results
        )
        status = VerificationRunStatus.COMPLETED if all_steps_completed else VerificationRunStatus.ROUTE_FAILED
        reason = (
            "verification runner reached target surface and collected observations"
            if status == VerificationRunStatus.COMPLETED and reached_surface
            else "verification runner did not reach the declared observation surface"
        )
        return VerificationRunResult(
            status=status,
            used_runner=True,
            reason=reason,
            target_surface=_target_surface(test_case),
            reached_surface=reached_surface,
            observation_sufficient=observation_sufficient,
            step_results=tuple(step_results),
            observation_record=observation_record if frames else None,
            contract_sha256=contract.sha256,
            metadata={
                "trace_dir": str(trace_dir),
                "elapsed_seconds": round(time.monotonic() - start_time, 3),
                "retry_budget_remaining": retry_budget,
                "verification_intent": intent.as_dict(),
                "verification_intent_sha256": intent.sha256,
                "generated_verification_steps": not bool(test_case.verification_steps),
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
        step: Mapping[str, Any],
        test_case: TestCaseSpec,
        *,
        current_frame: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        action_type = str(step["action_type"]).upper()
        if action_type == "WAIT":
            seconds = min(float(step.get("timeout_seconds") or 1.0), 5.0)
            time.sleep(seconds)
            return {"type": "wait", "seconds": seconds, "read_only_action": True}
        if action_type == "OBSERVE":
            return {"type": "observe", "read_only_action": True}
        if action_type == "BACK":
            key = "back" if self.device == "Android" else 2
            device.keyevent(key)
            return {"type": "press_back", "read_only_action": True}
        if action_type == "SCROLL":
            direction = _direction(step.get("target"), default="up")
            device.swipe(direction)
            return {"type": "scroll", "direction": direction, "read_only_action": True}
        if action_type == "REFRESH":
            device.swipe("down")
            return {"type": "refresh", "direction": "down", "read_only_action": True}
        if action_type == "NAVIGATE":
            step_target = step.get("target", {})
            target = None
            if isinstance(step_target, Mapping):
                target = _declared_coordinate_target(
                    step_target,
                    fallback_label=str(step.get("instruction") or "navigation target"),
                )
            if target is None:
                target = _resolve_target(current_frame, step_target, wants_text_input=False)
            if target is None:
                target = self._locate_with_vision(step, test_case, current_frame)
            if target is None:
                raise RuntimeError("navigation target was not found")
            x, y = target["center"]
            device.click(x, y)
            return {
                "type": "navigate",
                "target_element": target["text"],
                "position_x": x,
                "position_y": y,
                "click_point": [x, y],
                "bounds": list(target["bounds"]),
                "target_source": target.get("source", "hierarchy"),
                "read_only_action": True,
            }
        raise RuntimeError(f"unsupported real verification action type: {action_type}")

    def _locate_with_vision(
        self,
        step: Mapping[str, Any],
        test_case: TestCaseSpec,
        current_frame: Mapping[str, Any] | None,
    ) -> dict[str, Any] | None:
        if current_frame is None:
            return None
        if self.target_locator is not None:
            raw = self.target_locator(step, test_case, current_frame)
        else:
            raw = _model_verification_target_locator(step, test_case, current_frame)
        if raw is None:
            return None
        try:
            x = int(raw["x"])
            y = int(raw["y"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(f"verification target locator returned invalid coordinates: {raw}") from exc
        return {
            "text": str(raw.get("target_element") or step.get("instruction") or "navigation target"),
            "bounds": (x, y, x, y),
            "center": (x, y),
            "source": "vision_model",
            "confidence": raw.get("confidence"),
            "reason": raw.get("reason"),
        }

    def _capture_frame(
        self,
        device: Any,
        trace_dir: Path,
        *,
        frame_id: int,
        relative_to_action_ms: int,
    ) -> dict[str, Any]:
        screenshot_path = trace_dir / f"{frame_id}.jpg"
        device.screenshot(str(screenshot_path))
        hierarchy = device.dump_hierarchy()
        hierarchy_text, hierarchy_suffix, nodes = _parse_hierarchy_dump(hierarchy)
        hierarchy_path = trace_dir / f"{frame_id}{hierarchy_suffix}"
        hierarchy_path.write_text(hierarchy_text, encoding="utf-8")
        return {
            "frame_id": frame_id,
            "timestamp_ms": int(time.time() * 1000),
            "relative_to_action_ms": relative_to_action_ms,
            "screenshot": screenshot_path.name,
            "screenshot_abs": str(screenshot_path),
            "screenshot_sha256": _file_sha256(screenshot_path),
            "hierarchy": hierarchy_path.name,
            "hierarchy_kind": hierarchy_suffix.lstrip("."),
            "hierarchy_sha256": _file_sha256(hierarchy_path),
            "stability": "UNKNOWN",
            "visible_texts": list(_visible_texts(nodes)),
            "xml_nodes": nodes,
        }

    def _write_actions(
        self,
        trace_dir: Path,
        test_case: TestCaseSpec,
        actions: list[dict[str, Any]],
    ) -> None:
        payload = {
            "schema_version": "app-test-mobiagent-verification-actions-v1",
            "test_case_id": test_case.test_case_id,
            "app_name": test_case.app_under_test.name,
            "package": test_case.app_under_test.package,
            "action_count": len(actions),
            "actions": actions,
        }
        (trace_dir / "verification_actions.json").write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )

    def _env_blocked_result(
        self,
        test_case: TestCaseSpec,
        contract: AppTestContract,
        *,
        reason: str,
        blocker: str,
        frames: list[dict[str, Any]],
        frame_visible_texts: dict[str, list[str]],
        step_results: tuple[VerificationStepResult, ...],
        trace_dir: Path,
    ) -> VerificationRunResult:
        record = ExecutionRecord(
            test_case_id=test_case.test_case_id,
            executor=self.name,
            step_results=(),
            final_state=EvidenceState(
                visible_texts=tuple(frames[-1].get("visible_texts", ())) if frames else (),
                evidence_sufficient=False,
                notes=("verification environment blocked", blocker),
            ),
            raw_trace_dir=str(trace_dir),
            metadata={
                "verification_runner": self.name,
                "target_surface": _target_surface(test_case),
                "reached_surface": False,
                "observation_sufficient": False,
                "frames": frames,
                "frame_visible_texts": frame_visible_texts,
            },
        )
        return VerificationRunResult(
            status=VerificationRunStatus.ENV_BLOCKED,
            used_runner=True,
            reason=reason,
            target_surface=_target_surface(test_case),
            reached_surface=False,
            observation_sufficient=False,
            step_results=step_results,
            observation_record=record,
            contract_sha256=contract.sha256,
            metadata={"trace_dir": str(trace_dir), "blocker": blocker},
        )


class _VerificationRouteBlocked(RuntimeError):
    pass


def _first_unsupported_real_step(
    test_case: TestCaseSpec,
    verification_steps: tuple[Any, ...],
) -> str | None:
    del test_case
    for step in verification_steps:
        if step.action_type not in REAL_VERIFICATION_ACTION_TYPES:
            return (
                f"verification action {step.action_type} is not allowed for real "
                f"read-only verification step {step.verification_step_id}"
            )
        if _dangerous_step_text(step.as_dict()):
            return (
                f"verification step {step.verification_step_id} contains write-like "
                "or dangerous semantics and cannot be executed"
            )
    return None


def _dangerous_step_text(step: Mapping[str, Any]) -> bool:
    target = step.get("target", {})
    text = " ".join(
        str(value)
        for value in (
            step.get("instruction"),
            json.dumps(target, ensure_ascii=False, sort_keys=True)
            if isinstance(target, Mapping)
            else target,
        )
    ).casefold()
    return any(term.casefold() in text for term in DANGEROUS_VERIFICATION_TERMS)


def _failed_verification_step(
    step: Any,
    *,
    status: str,
    error: str,
    blocker: str | None = None,
    attempts: int = 1,
) -> VerificationStepResult:
    return VerificationStepResult(
        verification_step_id=step.verification_step_id,
        status=status,
        action_type=step.action_type,
        attempts=attempts,
        target=step.target,
        blocker=blocker,
        error=error,
    )


def _direction(target: Any, *, default: str) -> str:
    if isinstance(target, Mapping):
        raw = str(target.get("direction") or default).lower()
    else:
        raw = default
    if raw not in {"up", "down", "left", "right"}:
        return default
    return raw


def _environment_blocker(texts: tuple[str, ...] | list[str]) -> str | None:
    joined = "\n".join(str(item) for item in texts).casefold()
    for term in ENV_BLOCKER_TERMS:
        if term.casefold() in joined:
            return term
    return None


def _environment_blocker_frame(frame: Mapping[str, Any]) -> str | None:
    blocker = _environment_blocker(
        tuple(str(item) for item in frame.get("visible_texts", ()) if str(item))
    )
    if blocker is not None:
        return blocker
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


def _surface_reached(target: Mapping[str, Any], texts: tuple[str, ...] | list[str]) -> bool | None:
    candidates = tuple(
        str(item).strip()
        for key in ("surface_text_candidates", "text_candidates")
        for item in target.get(key, ())
        if str(item).strip()
    )
    if not candidates:
        return None
    joined = "\n".join(str(item) for item in texts)
    return any(candidate in joined for candidate in candidates)


def _route_reached_surface(
    verification_steps: tuple[Any, ...],
    step_results: tuple[VerificationStepResult, ...],
    final_texts: tuple[str, ...],
) -> bool:
    explicit = [result.reached_surface for result in step_results if result.reached_surface is not None]
    if explicit:
        return bool(explicit[-1])
    for step in reversed(verification_steps):
        reached = _surface_reached(step.target, final_texts)
        if reached is not None:
            return reached
    return False


def _model_verification_target_locator(
    step: Mapping[str, Any],
    test_case: TestCaseSpec,
    current_frame: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    screenshot_path = current_frame.get("screenshot_abs")
    if not isinstance(screenshot_path, str) or not Path(screenshot_path).is_file():
        return None
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
        "You are locating one read-only navigation target on a real mobile screenshot. "
        "Return JSON only. Do not decide whether the App test passed and do not choose "
        "targets that submit, publish, delete, like, pay, edit, or modify data.\n"
        f"App: {test_case.app_under_test.name}\n"
        f"Verification step id: {step.get('verification_step_id')}\n"
        f"Instruction: {step.get('instruction')}\n"
        f"Action type: {step.get('action_type')}\n"
        f"Target spec: {json.dumps(step.get('target', {}), ensure_ascii=False)}\n"
        f"Screenshot size: {width}x{height}\n"
        "Return exactly: {\"x\": integer absolute pixel x, \"y\": integer absolute pixel y, "
        "\"target_element\": short label, \"confidence\": number 0..1, \"reason\": short string}. "
        "If no safe read-only navigation target is visible, return {\"x\": null, \"y\": null, "
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
