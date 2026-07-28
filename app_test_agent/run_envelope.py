"""Auditable run envelope for App functional test evidence."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from .contract import AppTestContract
from .executor import ExecutionRecord
from .manifest import TestExecutionManifest
from .offline_verifier import OfflineTraceReview
from .result_types import (
    AppBehaviorResult,
    AttributionResult,
    ExecutionConformanceResult,
)
from .schema import TestCaseSpec, dump_json
from .temporal_boundaries import build_temporal_boundaries
from .verification_intent import compile_verification_intent
from .verification_runner import VerificationRunResult


RUN_ENVELOPE_SCHEMA_VERSION = "app-test-run-envelope-v1"
RUN_ENVELOPE_FILE = "run_envelope.json"


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def build_run_envelope(
    *,
    run_id: str,
    test_case: TestCaseSpec,
    contract: AppTestContract,
    execution: ExecutionRecord,
    execution_manifest: TestExecutionManifest,
    conformance: ExecutionConformanceResult,
    direct_behavior: AppBehaviorResult,
    behavior: AppBehaviorResult,
    verification_result: VerificationRunResult,
    business_offline_review: OfflineTraceReview,
    verification_offline_review: OfflineTraceReview | None = None,
    attribution: AttributionResult,
) -> dict[str, Any]:
    verification_payload = verification_result.as_dict()
    verification_trace = verification_payload.get("observation_record")
    verification_intent = compile_verification_intent(test_case)
    temporal_boundaries = build_temporal_boundaries(
        test_case=test_case,
        execution=execution,
        verification_result=verification_result,
        business_offline_review=business_offline_review,
        verification_offline_review=verification_offline_review,
    )
    body = {
        "schema_version": RUN_ENVELOPE_SCHEMA_VERSION,
        "run_id": run_id,
        "test_case_id": test_case.test_case_id,
        "test_case_sha256": test_case.sha256,
        "contract_sha256": contract.sha256,
        "execution_manifest_sha256": execution_manifest.sha256,
        "app_under_test": test_case.app_under_test.as_dict(),
        "feature": test_case.feature,
        "runtime_generated_data": dict(test_case.runtime_generated_data),
        "runtime_generated_data_sha256": canonical_sha256(dict(test_case.runtime_generated_data)),
        "result_summary": {
            "overall_result": attribution.overall_result,
            "attribution": attribution.attribution,
            "execution_status": conformance.status,
            "direct_app_behavior_status": direct_behavior.status,
            "app_behavior_status": behavior.status,
            "verification_runner_status": verification_result.status,
            "verification_runner_used": verification_result.used_runner,
            "business_offline_review_status": business_offline_review.status,
            "verification_offline_review_status": (
                verification_offline_review.status
                if verification_offline_review is not None
                else None
            ),
        },
        "artifact_registry": _artifact_registry(
            test_case=test_case,
            contract=contract,
            execution_manifest=execution_manifest,
            conformance=conformance,
            direct_behavior=direct_behavior,
            behavior=behavior,
            business_offline_review=business_offline_review,
            verification_offline_review=verification_offline_review,
            verification_payload=verification_payload,
            verification_trace=verification_trace,
            attribution=attribution,
        ),
        "business_execution": _business_execution_summary(
            execution=execution,
            execution_manifest=execution_manifest,
            business_offline_review=business_offline_review,
        ),
        "verification": _verification_summary(
            verification_runner_policy=test_case.verification_runner_policy,
            verification_result=verification_result,
            verification_payload=verification_payload,
            verification_trace=verification_trace,
            verification_intent=verification_intent.as_dict(),
            verification_intent_sha256=verification_intent.sha256,
            generated_from_intent=not bool(test_case.verification_steps),
            verification_offline_review=verification_offline_review,
        ),
        "temporal_boundaries": temporal_boundaries,
    }
    envelope_sha256 = canonical_sha256(body)
    return {
        **body,
        "run_envelope_sha256": envelope_sha256,
    }


def write_run_envelope(path: Path, envelope: Mapping[str, Any]) -> None:
    dump_json(path, envelope)


def load_run_envelope(path: Path) -> dict[str, Any]:
    payload = json.loads(path.resolve(strict=True).read_text(encoding="utf-8-sig"))
    if not isinstance(payload, Mapping):
        raise ValueError("run envelope must be an object")
    schema_version = payload.get("schema_version")
    if schema_version != RUN_ENVELOPE_SCHEMA_VERSION:
        raise ValueError(f"unsupported run envelope schema_version: {schema_version}")
    recorded = payload.get("run_envelope_sha256")
    if not isinstance(recorded, str) or not recorded:
        raise ValueError("run envelope missing run_envelope_sha256")
    body = dict(payload)
    body.pop("run_envelope_sha256", None)
    actual = canonical_sha256(body)
    if actual != recorded:
        raise ValueError("run envelope sha256 does not match payload")
    return dict(payload)


def _artifact_registry(
    *,
    test_case: TestCaseSpec,
    contract: AppTestContract,
    execution_manifest: TestExecutionManifest,
    conformance: ExecutionConformanceResult,
    direct_behavior: AppBehaviorResult,
    behavior: AppBehaviorResult,
    business_offline_review: OfflineTraceReview,
    verification_offline_review: OfflineTraceReview | None,
    verification_payload: Mapping[str, Any],
    verification_trace: Any,
    attribution: AttributionResult,
) -> list[dict[str, Any]]:
    artifacts = [
        _artifact("test_case.normalized.json", test_case.as_dict(), test_case.sha256, "normalized_test_case"),
        _artifact("app_test_contract.json", contract.as_dict(), contract.sha256, "app_test_contract"),
        _artifact(
            "test_execution_manifest.json",
            execution_manifest.as_dict(),
            execution_manifest.sha256,
            "business_execution_manifest",
        ),
        _artifact("execution_result.json", conformance.as_dict(), None, "execution_conformance_result"),
        _artifact(
            "business_offline_review.json",
            business_offline_review.as_dict(),
            None,
            "business_offline_trace_review",
        ),
        _artifact("direct_app_behavior_result.json", direct_behavior.as_dict(), None, "direct_app_behavior_result"),
        _artifact("app_behavior_result.json", behavior.as_dict(), None, "app_behavior_result"),
        _artifact("attribution_result.json", attribution.as_dict(), None, "attribution_result"),
    ]
    artifacts.append(
        _artifact(
            "verification_offline_review.json",
            verification_offline_review.as_dict() if verification_offline_review is not None else None,
            None,
            "verification_offline_trace_review",
            present=verification_offline_review is not None,
        )
    )
    artifacts.append(
        _artifact(
            "verification_runner_result.json",
            verification_payload,
            None,
            "verification_runner_result",
            present=bool(verification_payload),
        )
    )
    artifacts.append(
        _artifact(
            "verification_trace.json",
            verification_trace,
            None,
            "verification_observation_trace",
            present=isinstance(verification_trace, Mapping),
        )
    )
    return artifacts


def _artifact(
    relative_ref: str,
    payload: Any,
    known_sha256: str | None,
    kind: str,
    *,
    present: bool = True,
) -> dict[str, Any]:
    payload_sha256 = known_sha256 or canonical_sha256(payload)
    return {
        "relative_ref": relative_ref,
        "kind": kind,
        "present": present,
        "payload_sha256": payload_sha256,
    }


def _business_execution_summary(
    *,
    execution: ExecutionRecord,
    execution_manifest: TestExecutionManifest,
    business_offline_review: OfflineTraceReview,
) -> dict[str, Any]:
    return {
        "executor": execution.executor,
        "execution_record_sha256": canonical_sha256(execution.as_dict()),
        "execution_manifest_sha256": execution_manifest.sha256,
        "raw_trace_dir": execution.raw_trace_dir,
        "step_count": len(execution.step_results),
        "frames": [
            {
                "frame_id": frame.frame_id,
                "screenshot": frame.screenshot,
                "screenshot_sha256": frame.screenshot_sha256,
                "hierarchy": frame.hierarchy,
                "hierarchy_sha256": frame.hierarchy_sha256,
                "timestamp_ms": frame.timestamp_ms,
                "relative_to_action_ms": frame.relative_to_action_ms,
                "stability": frame.stability,
            }
            for frame in execution_manifest.frames
        ],
        "steps": [_step_integrity_summary(result.as_dict()) for result in execution.step_results],
        "runtime_generated_data": dict(execution_manifest.metadata.get("runtime_generated_data", {}))
        if isinstance(execution_manifest.metadata.get("runtime_generated_data"), Mapping)
        else {},
        "offline_review": {
            "status": business_offline_review.status,
            "reason": business_offline_review.reason,
            "trace_integrity": business_offline_review.trace_integrity,
            "authoritative": business_offline_review.authoritative,
            "assertion_review_count": len(business_offline_review.assertion_reviews),
            "offline_review_sha256": canonical_sha256(business_offline_review.as_dict()),
        },
    }


def _step_integrity_summary(step_result: Mapping[str, Any]) -> dict[str, Any]:
    evidence = step_result.get("evidence")
    evidence_map = dict(evidence) if isinstance(evidence, Mapping) else {}
    gate = evidence_map.get("step_gate")
    gate_attempts = evidence_map.get("step_gate_attempts")
    normalized_gate_attempts = (
        gate_attempts
        if isinstance(gate_attempts, list)
        else ([gate] if isinstance(gate, Mapping) else None)
    )
    burst = evidence_map.get("post_observation_burst")
    micro_observations = evidence_map.get("micro_action_observations")
    micro_gates = evidence_map.get("micro_gates")
    goal_state = evidence_map.get("goal_state")
    return {
        "step_id": step_result.get("step_id"),
        "status": step_result.get("status"),
        "action_type": step_result.get("action_type"),
        "attempts": step_result.get("attempts"),
        "pre_frame": step_result.get("pre_frame"),
        "post_frames": list(step_result.get("post_frames") or []),
        "step_result_sha256": canonical_sha256(step_result),
        "step_execution_intent_sha256": evidence_map.get("step_execution_intent_sha256"),
        "target_evidence": evidence_map.get("target_evidence"),
        "next_step_target_evidence": (
            gate.get("next_step_target_evidence")
            if isinstance(gate, Mapping)
            else None
        ),
        "next_step_target_resolution": evidence_map.get("next_step_target_resolution"),
        "progress_status": evidence_map.get("progress_status"),
        "gate_decision": evidence_map.get("gate_decision"),
        "step_gate_sha256": canonical_sha256(gate) if isinstance(gate, Mapping) else None,
        "step_gate_attempts_sha256": (
            canonical_sha256(normalized_gate_attempts)
            if isinstance(normalized_gate_attempts, list)
            else None
        ),
        "step_gate_attempt_count": (
            len(normalized_gate_attempts)
            if isinstance(normalized_gate_attempts, list)
            else 0
        ),
        "post_observation_burst_sha256": (
            canonical_sha256(burst) if isinstance(burst, Mapping) else None
        ),
        "post_observation_burst": burst if isinstance(burst, Mapping) else None,
        "micro_action_observations_sha256": (
            canonical_sha256(micro_observations)
            if isinstance(micro_observations, list)
            else None
        ),
        "micro_gates_sha256": (
            canonical_sha256(micro_gates)
            if isinstance(micro_gates, list)
            else None
        ),
        "micro_gate_count": len(micro_gates) if isinstance(micro_gates, list) else 0,
        "goal_state_sha256": canonical_sha256(goal_state) if isinstance(goal_state, Mapping) else None,
        "goal_state": dict(goal_state) if isinstance(goal_state, Mapping) else None,
    }


def _verification_summary(
    *,
    verification_runner_policy: str,
    verification_result: VerificationRunResult,
    verification_payload: Mapping[str, Any],
    verification_trace: Any,
    verification_intent: Mapping[str, Any],
    verification_intent_sha256: str,
    generated_from_intent: bool,
    verification_offline_review: OfflineTraceReview | None,
) -> dict[str, Any]:
    return {
        "runner_policy": verification_runner_policy,
        "status": verification_result.status,
        "used_runner": verification_result.used_runner,
        "target_surface": verification_result.target_surface,
        "reached_surface": verification_result.reached_surface,
        "observation_sufficient": verification_result.observation_sufficient,
        "verification_runner_result_sha256": canonical_sha256(verification_payload),
        "verification_trace_sha256": (
            canonical_sha256(verification_trace)
            if isinstance(verification_trace, Mapping)
            else None
        ),
        "verification_intent": dict(verification_intent),
        "verification_intent_sha256": verification_intent_sha256,
        "generated_from_verification_intent": generated_from_intent,
        "offline_review": (
            {
                "status": verification_offline_review.status,
                "reason": verification_offline_review.reason,
                "trace_integrity": verification_offline_review.trace_integrity,
                "authoritative": verification_offline_review.authoritative,
                "assertion_review_count": len(verification_offline_review.assertion_reviews),
                "offline_review_sha256": canonical_sha256(
                    verification_offline_review.as_dict()
                ),
            }
            if verification_offline_review is not None
            else None
        ),
        "step_results": [
            {
                "verification_step_id": item.get("verification_step_id"),
                "status": item.get("status"),
                "action_type": item.get("action_type"),
                "attempts": item.get("attempts"),
                "observation_frames": list(item.get("observation_frames") or []),
                "reached_surface": item.get("reached_surface"),
                "step_result_sha256": canonical_sha256(item),
            }
            for item in verification_payload.get("step_results", [])
            if isinstance(item, Mapping)
        ],
    }


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
