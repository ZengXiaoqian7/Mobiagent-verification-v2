"""Step-level execution manifest for real runner and replay intake."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .executor import EvidenceState, ExecutionRecord, StepExecutionResult, StepStatus
from .schema import TestCaseError, TestCaseSpec, dump_json


EXECUTION_MANIFEST_SCHEMA_VERSION = "app-test-execution-manifest-v1"
DISPATCH_STATUSES = frozenset(
    {"ACTION_DISPATCHED", "ACTION_NOT_DISPATCHED", "ENV_BLOCKED", "UNSUPPORTED"}
)
CONFORMANCE_STATUSES = frozenset(
    {"CONFORMANT", "NONCONFORMANT", "ENV_BLOCKED", "UNSUPPORTED", "UNKNOWN"}
)
EFFECT_STATUSES = frozenset(
    {
        "NOT_EVALUATED",
        "EFFECT_SATISFIED",
        "EFFECT_VIOLATED",
        "EFFECT_UNKNOWN",
    }
)


class ManifestIntakeError(ValueError):
    """Raised when a test execution manifest cannot be accepted."""


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _expect_mapping(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ManifestIntakeError(f"{context} must be an object")
    return dict(value)


def _expect_str(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ManifestIntakeError(f"{context} must be a non-empty string")
    return value.strip()


def _expect_status(value: Any, allowed: frozenset[str], context: str) -> str:
    normalized = _expect_str(value, context).upper()
    if normalized not in allowed:
        raise ManifestIntakeError(
            f"{context} unsupported: {normalized}; supported={sorted(allowed)}"
        )
    return normalized


def _expect_int(value: Any, context: str, *, minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ManifestIntakeError(f"{context} must be an integer >= {minimum}")
    return value


def _optional_int_list(value: Any, context: str) -> tuple[int, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ManifestIntakeError(f"{context} must be a list")
    return tuple(_expect_int(item, f"{context}[]") for item in value)


def _optional_str_list(value: Any, context: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ManifestIntakeError(f"{context} must be a list")
    return tuple(_expect_str(item, f"{context}[]") for item in value)


@dataclass(frozen=True)
class FrameEvidence:
    frame_id: int
    screenshot: str | None = None
    hierarchy: str | None = None
    screenshot_sha256: str | None = None
    hierarchy_sha256: str | None = None
    timestamp_ms: int | None = None
    relative_to_action_ms: int | None = None
    stability: str = "UNKNOWN"
    visible_texts: tuple[str, ...] = ()
    ocr_texts: tuple[str, ...] = ()

    @classmethod
    def from_json(cls, value: Mapping[str, Any], context: str) -> "FrameEvidence":
        data = _expect_mapping(value, context)
        timestamp = data.get("timestamp_ms")
        relative = data.get("relative_to_action_ms")
        return cls(
            frame_id=_expect_int(data.get("frame_id"), f"{context}.frame_id"),
            screenshot=(
                _expect_str(data.get("screenshot"), f"{context}.screenshot")
                if data.get("screenshot") is not None
                else None
            ),
            hierarchy=(
                _expect_str(data.get("hierarchy"), f"{context}.hierarchy")
                if data.get("hierarchy") is not None
                else None
            ),
            screenshot_sha256=(
                _expect_str(
                    data.get("screenshot_sha256"), f"{context}.screenshot_sha256"
                )
                if data.get("screenshot_sha256") is not None
                else None
            ),
            hierarchy_sha256=(
                _expect_str(
                    data.get("hierarchy_sha256"), f"{context}.hierarchy_sha256"
                )
                if data.get("hierarchy_sha256") is not None
                else None
            ),
            timestamp_ms=(
                _expect_int(timestamp, f"{context}.timestamp_ms")
                if timestamp is not None
                else None
            ),
            relative_to_action_ms=(
                _expect_int(relative, f"{context}.relative_to_action_ms")
                if relative is not None
                else None
            ),
            stability=_expect_str(data.get("stability", "UNKNOWN"), f"{context}.stability"),
            visible_texts=_optional_str_list(data.get("visible_texts"), f"{context}.visible_texts"),
            ocr_texts=_optional_str_list(data.get("ocr_texts"), f"{context}.ocr_texts"),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "frame_id": self.frame_id,
            "screenshot": self.screenshot,
            "hierarchy": self.hierarchy,
            "screenshot_sha256": self.screenshot_sha256,
            "hierarchy_sha256": self.hierarchy_sha256,
            "timestamp_ms": self.timestamp_ms,
            "relative_to_action_ms": self.relative_to_action_ms,
            "stability": self.stability,
            "visible_texts": list(self.visible_texts),
            "ocr_texts": list(self.ocr_texts),
        }


@dataclass(frozen=True)
class StepEvidenceRecord:
    step_id: str
    dispatch_status: str
    conformance_status: str
    effect_status: str
    action_type: str
    attempts: int = 1
    action_ids: tuple[int, ...] = ()
    target_match: bool | None = None
    input_match: bool | None = None
    expected_value: str | None = None
    actual_value: str | None = None
    pre_frames: tuple[int, ...] = ()
    post_frames: tuple[int, ...] = ()
    blocker: str | None = None
    error: str | None = None
    evidence: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_json(cls, value: Mapping[str, Any], index: int) -> "StepEvidenceRecord":
        data = _expect_mapping(value, f"steps[{index}]")
        target_match = data.get("target_match")
        input_match = data.get("input_match")
        if target_match is not None and not isinstance(target_match, bool):
            raise ManifestIntakeError(f"steps[{index}].target_match must be boolean")
        if input_match is not None and not isinstance(input_match, bool):
            raise ManifestIntakeError(f"steps[{index}].input_match must be boolean")
        return cls(
            step_id=_expect_str(data.get("step_id"), f"steps[{index}].step_id"),
            dispatch_status=_expect_status(
                data.get("dispatch_status"),
                DISPATCH_STATUSES,
                f"steps[{index}].dispatch_status",
            ),
            conformance_status=_expect_status(
                data.get("conformance_status"),
                CONFORMANCE_STATUSES,
                f"steps[{index}].conformance_status",
            ),
            effect_status=_expect_status(
                data.get("effect_status", "NOT_EVALUATED"),
                EFFECT_STATUSES,
                f"steps[{index}].effect_status",
            ),
            action_type=_expect_str(data.get("action_type"), f"steps[{index}].action_type").upper(),
            attempts=_expect_int(data.get("attempts", 1), f"steps[{index}].attempts", minimum=0),
            action_ids=_optional_int_list(data.get("action_ids"), f"steps[{index}].action_ids"),
            target_match=target_match,
            input_match=input_match,
            expected_value=(
                _expect_str(data.get("expected_value"), f"steps[{index}].expected_value")
                if data.get("expected_value") is not None
                else None
            ),
            actual_value=(
                _expect_str(data.get("actual_value"), f"steps[{index}].actual_value")
                if data.get("actual_value") is not None
                else None
            ),
            pre_frames=_optional_int_list(data.get("pre_frames"), f"steps[{index}].pre_frames"),
            post_frames=_optional_int_list(data.get("post_frames"), f"steps[{index}].post_frames"),
            blocker=(
                _expect_str(data.get("blocker"), f"steps[{index}].blocker")
                if data.get("blocker") is not None
                else None
            ),
            error=(
                _expect_str(data.get("error"), f"steps[{index}].error")
                if data.get("error") is not None
                else None
            ),
            evidence=_expect_mapping(data.get("evidence", {}), f"steps[{index}].evidence"),
        )

    def to_step_result(self, test_case: TestCaseSpec) -> StepExecutionResult:
        status = StepStatus.STEP_COMPLETED
        if self.dispatch_status == "ENV_BLOCKED" or self.conformance_status == "ENV_BLOCKED":
            status = StepStatus.ENV_BLOCKED
        elif self.dispatch_status == "UNSUPPORTED" or self.conformance_status == "UNSUPPORTED":
            status = StepStatus.UNSUPPORTED
        elif self.conformance_status == "UNKNOWN":
            status = StepStatus.INCONCLUSIVE
        elif (
            self.dispatch_status != "ACTION_DISPATCHED"
            or self.conformance_status != "CONFORMANT"
        ):
            status = StepStatus.STEP_FAILED
        resolved_value = self.actual_value
        step = next((item for item in test_case.steps if item.step_id == self.step_id), None)
        target = {} if step is None else step.target
        return StepExecutionResult(
            step_id=self.step_id,
            status=status,
            action_type=self.action_type,
            attempts=self.attempts,
            resolved_value=resolved_value,
            target=target,
            pre_frame=self.pre_frames[0] if self.pre_frames else None,
            post_frames=self.post_frames,
            blocker=self.blocker,
            error=self.error,
            evidence={
                **dict(self.evidence),
                "dispatch_status": self.dispatch_status,
                "conformance_status": self.conformance_status,
                "effect_status": self.effect_status,
                "action_ids": list(self.action_ids),
                "target_match": self.target_match,
                "input_match": self.input_match,
                "expected_value": self.expected_value,
                "pre_frames": list(self.pre_frames),
                "post_frames": list(self.post_frames),
            },
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "dispatch_status": self.dispatch_status,
            "conformance_status": self.conformance_status,
            "effect_status": self.effect_status,
            "action_type": self.action_type,
            "attempts": self.attempts,
            "action_ids": list(self.action_ids),
            "target_match": self.target_match,
            "input_match": self.input_match,
            "expected_value": self.expected_value,
            "actual_value": self.actual_value,
            "pre_frames": list(self.pre_frames),
            "post_frames": list(self.post_frames),
            "blocker": self.blocker,
            "error": self.error,
            "evidence": dict(self.evidence),
        }


@dataclass(frozen=True)
class TestExecutionManifest:
    run_id: str
    test_case_id: str
    test_case_sha256: str
    contract_sha256: str
    executor: str
    steps: tuple[StepEvidenceRecord, ...]
    frames: tuple[FrameEvidence, ...] = ()
    final_state: EvidenceState = field(default_factory=EvidenceState)
    raw_trace_dir: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    schema_version: str = EXECUTION_MANIFEST_SCHEMA_VERSION

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> "TestExecutionManifest":
        data = _expect_mapping(value, "test_execution_manifest")
        schema_version = _expect_str(data.get("schema_version"), "schema_version")
        if schema_version != EXECUTION_MANIFEST_SCHEMA_VERSION:
            raise ManifestIntakeError(f"unsupported schema_version: {schema_version}")
        final_state = _expect_mapping(data.get("final_state", {}), "final_state")
        return cls(
            run_id=_expect_str(data.get("run_id"), "run_id"),
            test_case_id=_expect_str(data.get("test_case_id"), "test_case_id"),
            test_case_sha256=_expect_str(data.get("test_case_sha256"), "test_case_sha256"),
            contract_sha256=_expect_str(data.get("contract_sha256"), "contract_sha256"),
            executor=_expect_str(data.get("executor"), "executor"),
            steps=tuple(
                StepEvidenceRecord.from_json(item, index)
                for index, item in enumerate(_list(data.get("steps"), "steps"))
            ),
            frames=tuple(
                FrameEvidence.from_json(item, f"frames[{index}]")
                for index, item in enumerate(
                    _optional_list(data.get("frames", []), "frames")
                )
            ),
            final_state=EvidenceState(
                visible_texts=_optional_str_list(final_state.get("visible_texts"), "final_state.visible_texts"),
                state_changed=final_state.get("state_changed")
                if isinstance(final_state.get("state_changed"), bool)
                or final_state.get("state_changed") is None
                else _bad_bool("final_state.state_changed"),
                success_signals=_optional_str_list(final_state.get("success_signals"), "final_state.success_signals"),
                evidence_sufficient=(
                    final_state.get("evidence_sufficient", True)
                    if isinstance(final_state.get("evidence_sufficient", True), bool)
                    else _bad_bool("final_state.evidence_sufficient")
                ),
                notes=_optional_str_list(final_state.get("notes"), "final_state.notes"),
            ),
            raw_trace_dir=(
                _expect_str(data.get("raw_trace_dir"), "raw_trace_dir")
                if data.get("raw_trace_dir") is not None
                else None
            ),
            metadata=_expect_mapping(data.get("metadata", {}), "metadata"),
            schema_version=schema_version,
        )

    def validate_against(self, test_case: TestCaseSpec, contract_sha256: str | None = None) -> None:
        if self.test_case_id != test_case.test_case_id:
            raise ManifestIntakeError("manifest test_case_id does not match test case")
        if self.test_case_sha256 != test_case.sha256:
            raise ManifestIntakeError("manifest test_case_sha256 does not match test case")
        if contract_sha256 is not None and self.contract_sha256 != contract_sha256:
            raise ManifestIntakeError("manifest contract_sha256 does not match App test contract")
        expected_ids = [step.step_id for step in test_case.steps]
        actual_ids = [step.step_id for step in self.steps]
        if actual_ids != expected_ids:
            if not actual_ids:
                raise ManifestIntakeError("manifest steps must not be empty")
            expected_prefix = expected_ids[: len(actual_ids)]
            if actual_ids != expected_prefix:
                raise ManifestIntakeError(
                    "manifest step ids are not a strict test-case prefix; "
                    f"expected_prefix={expected_prefix}, actual={actual_ids}"
                )
            terminal = self.steps[-1]
            if (
                terminal.dispatch_status == "ACTION_DISPATCHED"
                and terminal.conformance_status == "CONFORMANT"
            ):
                raise ManifestIntakeError(
                    "truncated manifest requires a terminal non-conformant step"
                )
        frame_ids = {frame.frame_id for frame in self.frames}
        for step, declared in zip(test_case.steps, self.steps):
            if declared.action_type != step.action_type:
                raise ManifestIntakeError(
                    f"step {step.step_id} action_type mismatch: {declared.action_type} != {step.action_type}"
                )
            expected_value = step.resolved_value(test_case.test_data)
            if expected_value is not None and declared.expected_value != expected_value:
                raise ManifestIntakeError(
                    f"step {step.step_id} expected_value does not match test_data"
                )
            if (
                declared.conformance_status == "CONFORMANT"
                and declared.dispatch_status == "ACTION_DISPATCHED"
                and not declared.post_frames
            ):
                raise ManifestIntakeError(
                    f"step {step.step_id} is conformant but has no post observation frame"
                )
            for frame_id in (*declared.pre_frames, *declared.post_frames):
                if frame_ids and frame_id not in frame_ids:
                    raise ManifestIntakeError(
                        f"step {step.step_id} references unknown frame: {frame_id}"
                    )

    def to_execution_record(self, test_case: TestCaseSpec, contract_sha256: str | None = None) -> ExecutionRecord:
        self.validate_against(test_case, contract_sha256)
        frames = [frame.as_dict() for frame in self.frames]
        frame_visible_texts = {
            str(frame.frame_id): list(frame.visible_texts + frame.ocr_texts)
            for frame in self.frames
        }
        return ExecutionRecord(
            test_case_id=self.test_case_id,
            executor=self.executor,
            step_results=tuple(step.to_step_result(test_case) for step in self.steps),
            final_state=self.final_state,
            raw_trace_dir=self.raw_trace_dir,
            metadata={
                **dict(self.metadata),
                "run_id": self.run_id,
                "contract_sha256": self.contract_sha256,
                "manifest_sha256": self.sha256,
                "frames": frames,
                "frame_visible_texts": frame_visible_texts,
            },
        )

    @property
    def sha256(self) -> str:
        return hashlib.sha256(_canonical_bytes(self.as_dict())).hexdigest()

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "test_case_id": self.test_case_id,
            "test_case_sha256": self.test_case_sha256,
            "contract_sha256": self.contract_sha256,
            "executor": self.executor,
            "steps": [step.as_dict() for step in self.steps],
            "frames": [frame.as_dict() for frame in self.frames],
            "final_state": self.final_state.as_dict(),
            "raw_trace_dir": self.raw_trace_dir,
            "metadata": dict(self.metadata),
        }


def _list(value: Any, context: str) -> list[Any]:
    if not isinstance(value, list) or not value:
        raise ManifestIntakeError(f"{context} must be a non-empty list")
    return value


def _optional_list(value: Any, context: str) -> list[Any]:
    if not isinstance(value, list):
        raise ManifestIntakeError(f"{context} must be a list")
    return value


def _bad_bool(context: str) -> bool:
    raise ManifestIntakeError(f"{context} must be boolean or null")


def load_execution_manifest(
    path: Path,
    test_case: TestCaseSpec,
    contract_sha256: str | None = None,
) -> TestExecutionManifest:
    source = path.resolve(strict=True)
    try:
        payload = json.loads(source.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ManifestIntakeError(f"invalid JSON in execution manifest: {exc}") from exc
    manifest = TestExecutionManifest.from_json(payload)
    manifest.validate_against(test_case, contract_sha256)
    return manifest


def write_execution_manifest(path: Path, manifest: TestExecutionManifest) -> None:
    dump_json(path, manifest.as_dict())


def build_manifest_from_execution_record(
    *,
    test_case: TestCaseSpec,
    execution: ExecutionRecord,
    run_id: str,
    contract_sha256: str,
) -> TestExecutionManifest:
    steps = []
    for result in execution.step_results:
        step = next((item for item in test_case.steps if item.step_id == result.step_id), None)
        if step is None:
            raise TestCaseError(f"execution result references unknown step: {result.step_id}")
        dispatch_status = "ACTION_DISPATCHED"
        conformance_status = "CONFORMANT"
        if result.status == StepStatus.ENV_BLOCKED:
            dispatch_status = "ENV_BLOCKED"
            conformance_status = "ENV_BLOCKED"
        elif result.status == StepStatus.UNSUPPORTED:
            dispatch_status = "UNSUPPORTED"
            conformance_status = "UNSUPPORTED"
        elif result.status == StepStatus.INCONCLUSIVE:
            dispatch_status = (
                "ACTION_DISPATCHED"
                if result.evidence.get("action_ids")
                else "ACTION_NOT_DISPATCHED"
            )
            conformance_status = "UNKNOWN"
        elif result.status != StepStatus.STEP_COMPLETED:
            dispatch_status = "ACTION_NOT_DISPATCHED"
            conformance_status = "NONCONFORMANT"
        expected_value = step.resolved_value(test_case.test_data)
        steps.append(
            StepEvidenceRecord(
                step_id=result.step_id,
                dispatch_status=dispatch_status,
                conformance_status=conformance_status,
                effect_status="NOT_EVALUATED",
                action_type=result.action_type,
                attempts=result.attempts,
                action_ids=tuple(result.evidence.get("action_ids", ())),
                target_match=(
                    result.evidence.get("target_match")
                    if isinstance(result.evidence.get("target_match"), bool)
                    else None
                ),
                input_match=(
                    result.resolved_value == expected_value
                    if expected_value is not None
                    else None
                ),
                expected_value=expected_value,
                actual_value=result.resolved_value,
                pre_frames=(result.pre_frame,) if result.pre_frame is not None else (),
                post_frames=result.post_frames,
                blocker=result.blocker,
                error=result.error,
                evidence=result.evidence,
            )
        )
    return TestExecutionManifest(
        run_id=run_id,
        test_case_id=test_case.test_case_id,
        test_case_sha256=test_case.sha256,
        contract_sha256=contract_sha256,
        executor=execution.executor,
        steps=tuple(steps),
        frames=_frames_from_execution(execution, run_id=run_id),
        final_state=execution.final_state,
        raw_trace_dir=execution.raw_trace_dir,
        metadata={
            **dict(execution.metadata),
            "contract_sha256": contract_sha256,
            "runtime_generated_data": dict(test_case.runtime_generated_data),
        },
    )


def _frames_from_execution(execution: ExecutionRecord, *, run_id: str) -> tuple[FrameEvidence, ...]:
    supplied = execution.metadata.get("frames")
    if isinstance(supplied, list):
        return tuple(
            FrameEvidence.from_json(item, f"metadata.frames[{index}]")
            for index, item in enumerate(supplied)
            if isinstance(item, Mapping)
        )
    frame_texts = execution.metadata.get("frame_visible_texts")
    if not isinstance(frame_texts, Mapping):
        frame_texts = {}
    frame_id_set: set[int] = set()
    for result in execution.step_results:
        if result.pre_frame is not None:
            frame_id_set.add(result.pre_frame)
        frame_id_set.update(result.post_frames)
    frame_ids = sorted(frame_id_set)
    frames: list[FrameEvidence] = []
    for frame_id in frame_ids:
        raw_texts = frame_texts.get(str(frame_id), frame_texts.get(frame_id))
        visible_texts = (
            tuple(str(item) for item in raw_texts if str(item))
            if isinstance(raw_texts, list)
            else ()
        )
        artifact_seed = f"{run_id}:{execution.test_case_id}:{frame_id}".encode("utf-8")
        artifact_hash = hashlib.sha256(artifact_seed).hexdigest()
        frames.append(
            FrameEvidence(
                frame_id=frame_id,
                screenshot=f"mock://{run_id}/frame/{frame_id}.png",
                hierarchy=f"mock://{run_id}/frame/{frame_id}.xml",
                screenshot_sha256=artifact_hash,
                hierarchy_sha256=hashlib.sha256((artifact_hash + ":hierarchy").encode("utf-8")).hexdigest(),
                timestamp_ms=frame_id * 1000,
                relative_to_action_ms=0 if frame_id == 0 else 500,
                stability="STABLE",
                visible_texts=visible_texts,
            )
        )
    return tuple(frames)
