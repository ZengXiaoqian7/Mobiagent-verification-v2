"""Intake a Harmony UiTest report and re-run the PC offline semantics.

The phone report is intentionally treated as untrusted evidence.  This command
validates the embedded testcase/contract/manifest hashes, reconstructs the PC
ExecutionRecord, runs the offline trace review and emits a final PC report.
It can optionally retrieve a report from an app sandbox with ``hdc file recv
-b <bundle>``.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
from typing import Any, Mapping

from .app_verifier import verify_app_behavior
from .attribution import attribute_result
from .contract import compile_app_test_contract
from .execution_verifier import verify_execution_conformance
from .executor import EvidenceState, ExecutionRecord
from .manifest import TestExecutionManifest
from .offline_verifier import OfflineTraceRole, review_app_test_trace
from .run_envelope import canonical_sha256
from .schema import TestCaseSpec, dump_json


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    report_group = parser.add_mutually_exclusive_group()
    report_group.add_argument("--report", type=Path, help="exported Harmony report.json")
    report_group.add_argument(
        "--report-dir",
        type=Path,
        help="directory containing exported root-level Harmony report JSON files",
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--device-serial")
    parser.add_argument("--device-bundle", default="com.zengxq.mobiagentprobe")
    parser.add_argument("--remote-report", help="remote report path inside the app sandbox")
    args = parser.parse_args()
    if args.report_dir is not None:
        summaries = intake_report_directory(args.report_dir, args.output_dir)
        print(json.dumps(summaries, ensure_ascii=False, indent=2))
        return 0
    report_path = args.report or _retrieve_report(
        serial=args.device_serial,
        bundle=args.device_bundle,
        remote=args.remote_report,
        output_dir=args.output_dir,
    )
    result = intake_report(report_path, args.output_dir)
    print(json.dumps(result["summary"], ensure_ascii=False, indent=2))
    return 0


def intake_report(report_path: Path, output_dir: Path) -> dict[str, Any]:
    source = report_path.resolve(strict=True)
    payload = json.loads(source.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, Mapping):
        raise ValueError("Harmony report must be an object")
    normalized = _json_value(payload.get("normalizedTestCase"))
    test_case = TestCaseSpec.from_json(_normalize_resolved_references(normalized))
    contract = compile_app_test_contract(test_case)
    if payload.get("testCaseId") != test_case.test_case_id:
        raise ValueError("Harmony report testCaseId does not match normalized testcase")
    phone_contract = payload.get("contract")
    if not isinstance(phone_contract, Mapping):
        raise ValueError("Harmony report is missing contract")
    phone_test_case_sha256 = str(phone_contract.get("testCaseSha256") or "")
    phone_contract_sha256 = str(phone_contract.get("hash") or "")
    if phone_test_case_sha256 != _sha256_text(str(payload.get("normalizedTestCase") or "")):
        raise ValueError("Harmony normalized testcase hash does not match contract")
    manifest_payload = payload.get("executionManifest")
    if not isinstance(manifest_payload, Mapping):
        raise ValueError("Harmony report is missing executionManifest")
    if str(manifest_payload.get("test_case_sha256") or "") != phone_test_case_sha256:
        raise ValueError("Harmony manifest testcase hash does not match contract")
    if str(manifest_payload.get("contract_sha256") or "") != phone_contract_sha256:
        raise ValueError("Harmony manifest contract hash does not match contract")
    phone_envelope = payload.get("runEnvelope")
    envelope_hash_verified = False
    if isinstance(phone_envelope, Mapping):
        recorded_envelope_hash = phone_envelope.get("run_envelope_sha256")
        envelope_body = dict(phone_envelope)
        envelope_body.pop("run_envelope_sha256", None)
        envelope_hash_verified = (
            isinstance(recorded_envelope_hash, str)
            and canonical_sha256(envelope_body) == recorded_envelope_hash
        )
        if not envelope_hash_verified:
            raise ValueError("Harmony run envelope sha256 does not match payload")
    else:
        raise ValueError("Harmony report is missing runEnvelope")
    computed_phone_manifest_sha256 = canonical_sha256(manifest_payload)
    declared_phone_manifest_sha256 = phone_envelope.get("execution_manifest_sha256")
    if declared_phone_manifest_sha256 != computed_phone_manifest_sha256:
        raise ValueError(
            "Harmony executionManifestSha256 does not match recomputed manifest payload"
        )
    top_level_manifest_sha256 = payload.get("executionManifestSha256")
    if (
        top_level_manifest_sha256 is not None
        and top_level_manifest_sha256 != computed_phone_manifest_sha256
    ):
        raise ValueError(
            "Harmony top-level executionManifestSha256 does not match manifest payload"
        )
    _validate_manifest_frame_hashes(manifest_payload, "executionManifest")
    # The two runtimes preserve the phone hashes above, then use a typed PC
    # contract for semantic replay.  The bridge fields are deliberately not
    # presented as the phone's original hashes in the final report.
    bridge_manifest_payload = dict(manifest_payload)
    bridge_manifest_payload["test_case_sha256"] = test_case.sha256
    bridge_manifest_payload["contract_sha256"] = contract.sha256
    manifest = TestExecutionManifest.from_json(bridge_manifest_payload)
    manifest.validate_against(test_case, contract.sha256)
    execution = manifest.to_execution_record(test_case, contract.sha256)
    conformance = verify_execution_conformance(test_case, execution, contract)
    offline_review = review_app_test_trace(
        test_case=test_case,
        execution=execution,
        contract=contract,
        role=OfflineTraceRole.BUSINESS_EXECUTION,
    )
    verification_execution = _verification_execution(payload, test_case)
    verification_offline_review = (
        review_app_test_trace(
            test_case=test_case,
            execution=verification_execution,
            contract=contract,
            role=OfflineTraceRole.VERIFICATION_OBSERVATION,
            verification_context={"runner": "harmony-uitest-verification-trace"},
        )
        if verification_execution is not None
        else None
    )
    behavior = verify_app_behavior(
        test_case,
        execution,
        conformance,
        contract,
        verification_execution=verification_execution,
        verification_context=(
            {"runner": "harmony-uitest-verification-trace"}
            if verification_execution is not None
            else None
        ),
        offline_review=verification_offline_review or offline_review,
    )
    attribution = attribute_result(conformance, behavior)
    output_dir.mkdir(parents=True, exist_ok=True)
    report = {
        "schema_version": "app-test-pc-offline-intake-v1",
        "run_id": str(payload.get("runId") or manifest.run_id),
        "test_case_id": test_case.test_case_id,
        "test_case_sha256": test_case.sha256,
        "contract_sha256": contract.sha256,
        "manifest_sha256": manifest.sha256,
        "phone_execution_manifest_sha256": computed_phone_manifest_sha256,
        "phone_execution_manifest_hash_verified": True,
        "phone_test_case_sha256": phone_test_case_sha256,
        "phone_contract_sha256": phone_contract_sha256,
        "phone_run_envelope_hash_verified": envelope_hash_verified,
        "execution": conformance.as_dict(),
        "business_offline_review": offline_review.as_dict(),
        "verification_offline_review": (
            verification_offline_review.as_dict()
            if verification_offline_review is not None
            else None
        ),
        "app_behavior": behavior.as_dict(),
        "attribution": attribution.as_dict(),
        "phone_report_sha256": _file_sha256(source),
        "phone_run_envelope": payload.get("runEnvelope"),
    }
    dump_json(output_dir / "pc_offline_review.json", offline_review.as_dict())
    if verification_offline_review is not None:
        dump_json(
            output_dir / "pc_verification_offline_review.json",
            verification_offline_review.as_dict(),
        )
    dump_json(output_dir / "pc_execution_result.json", conformance.as_dict())
    dump_json(output_dir / "pc_app_behavior_result.json", behavior.as_dict())
    dump_json(output_dir / "pc_final_report.json", report)
    return {
        "summary": {
            "status": "HARMONY_OFFLINE_REVIEW_COMPLETE",
            "overall_result": attribution.overall_result,
            "attribution": attribution.attribution,
            "execution_status": conformance.status,
            "app_behavior_status": behavior.status,
            "offline_review_status": offline_review.status,
            "verification_offline_review_status": (
                verification_offline_review.status
                if verification_offline_review is not None
                else None
            ),
            "output_dir": str(output_dir.resolve()),
        },
        "report": report,
    }


def intake_report_directory(report_dir: Path, output_dir: Path) -> dict[str, Any]:
    """Intake all root-level exported phone reports without mixing run outputs."""
    source_dir = report_dir.resolve(strict=True)
    reports = sorted(path for path in source_dir.glob("*.json") if path.is_file())
    if not reports:
        raise ValueError(f"no root-level report JSON files found in {source_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    for report_path in reports:
        report_output = output_dir / report_path.stem
        result = intake_report(report_path, report_output)
        results.append(result["summary"])
    summary = {
        "status": "HARMONY_OFFLINE_BATCH_COMPLETE",
        "report_count": len(results),
        "reports": results,
        "output_dir": str(output_dir.resolve()),
    }
    dump_json(output_dir / "batch_summary.json", summary)
    return summary


def _retrieve_report(
    *,
    serial: str | None,
    bundle: str,
    remote: str | None,
    output_dir: Path,
) -> Path:
    if not remote:
        raise ValueError("--remote-report is required when --report is not supplied")
    output_dir.mkdir(parents=True, exist_ok=True)
    destination = output_dir / "phone_report.json"
    command = ["hdc"]
    if serial:
        command.extend(["-t", serial])
    command.extend(["file", "recv", "-b", bundle, remote, str(destination)])
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise RuntimeError(
            f"hdc report export failed ({completed.returncode}): "
            f"{completed.stderr.strip() or completed.stdout.strip()}"
        )
    return destination


def _verification_required(test_case: TestCaseSpec) -> bool:
    return (
        test_case.verification_runner_policy.upper() == "REQUIRED_FOR_RESULT"
        or any(item.requires_verification_runner for item in test_case.expected_results)
    )


def _normalize_resolved_references(value: Any) -> dict[str, Any]:
    """Adapt Harmony's resolved canonical form without weakening PC input rules.

    On device, a reference is retained for provenance *and* its resolved value
    is recorded for local execution.  PC source test cases intentionally reject
    that ambiguous pair.  For a phone report we accept it only when the value
    exactly equals the referenced test_data value, then replay the PC semantics
    through the reference alone.  Any mismatch remains a hard intake failure.
    """
    if not isinstance(value, Mapping):
        raise ValueError("Harmony normalizedTestCase must be an object")
    normalized = json.loads(json.dumps(value, ensure_ascii=False))
    test_data = normalized.get("test_data")
    if not isinstance(test_data, Mapping):
        raise ValueError("Harmony normalizedTestCase.test_data must be an object")

    def resolve_pair(item: Any, literal_key: str, ref_key: str, context: str) -> None:
        if not isinstance(item, dict):
            raise ValueError(f"Harmony {context} must be an object")
        literal = item.get(literal_key)
        reference = item.get(ref_key)
        if literal is None or reference is None:
            return
        if not isinstance(literal, str) or not isinstance(reference, str):
            raise ValueError(f"Harmony {context} resolved reference fields must be strings")
        if reference not in test_data or not isinstance(test_data[reference], str):
            raise ValueError(f"Harmony {context} value_ref is not present in test_data")
        if literal != test_data[reference]:
            raise ValueError(f"Harmony {context} resolved value does not match test_data reference")
        item.pop(literal_key)

    steps = normalized.get("steps")
    if not isinstance(steps, list):
        raise ValueError("Harmony normalizedTestCase.steps must be a list")
    for index, step in enumerate(steps):
        resolve_pair(step, "value", "value_ref", f"normalizedTestCase.steps[{index}]")
    expected = normalized.get("expected_results")
    if not isinstance(expected, list):
        raise ValueError("Harmony normalizedTestCase.expected_results must be a list")
    for index, assertion in enumerate(expected):
        resolve_pair(
            assertion,
            "expected_value",
            "expected_value_ref",
            f"normalizedTestCase.expected_results[{index}]",
        )
    return normalized


def _verification_execution(
    payload: Mapping[str, Any], test_case: TestCaseSpec
) -> ExecutionRecord | None:
    """Validate and adapt the isolated phone verification trace.

    Verification frames are intentionally not execution-manifest frames: the
    read-only runner may navigate away from the business result surface.  The
    trace is therefore consumed as a separate observation record.
    """
    trace = payload.get("verificationTrace")
    if trace is None:
        if _verification_required(test_case):
            raise ValueError("Harmony report requires verification evidence but verificationTrace is missing")
        return None
    if not isinstance(trace, Mapping):
        raise ValueError("Harmony verificationTrace must be an object")
    recorded_hash = trace.get("verification_trace_sha256")
    trace_body = dict(trace)
    trace_body.pop("verification_trace_sha256", None)
    if not isinstance(recorded_hash, str) or canonical_sha256(trace_body) != recorded_hash:
        raise ValueError("Harmony verification trace sha256 does not match payload")
    frames_raw = trace.get("frames")
    actions_raw = trace.get("actions")
    if not isinstance(frames_raw, list) or not isinstance(actions_raw, list):
        raise ValueError("Harmony verificationTrace requires actions and frames arrays")
    frames: list[dict[str, Any]] = []
    frame_ids: set[int] = set()
    for index, raw in enumerate(frames_raw):
        if not isinstance(raw, Mapping):
            raise ValueError(f"verificationTrace.frames[{index}] must be an object")
        frame_id = raw.get("frameId")
        if not isinstance(frame_id, int) or isinstance(frame_id, bool) or frame_id < 0:
            raise ValueError(f"verificationTrace.frames[{index}].frameId must be a non-negative integer")
        if frame_id in frame_ids:
            raise ValueError("Harmony verification trace contains duplicate frame ids")
        frame_ids.add(frame_id)
        source = raw.get("source")
        if not isinstance(source, str) or "verification" not in source.lower():
            raise ValueError("Harmony verification frame source must identify verification capture")
        hierarchy = raw.get("hierarchyJson", "")
        screenshot = raw.get("screenshotBase64", "")
        visible = raw.get("visibleTexts", [])
        signals = raw.get("successSignals", [])
        if not isinstance(hierarchy, str) or not isinstance(screenshot, str):
            raise ValueError("Harmony verification frame hierarchy/screenshot must be strings")
        if not isinstance(visible, list) or not all(isinstance(item, str) for item in visible):
            raise ValueError("Harmony verification frame visibleTexts must be a string list")
        if not isinstance(signals, list) or not all(isinstance(item, str) for item in signals):
            raise ValueError("Harmony verification frame successSignals must be a string list")
        _validate_frame_hashes(raw, f"verificationTrace.frames[{index}]")
        frames.append(
            {
                "frame_id": frame_id,
                "timestamp_ms": raw.get("capturedAt"),
                "stability": "STABLE",
                "visible_texts": visible,
                "hierarchy": hierarchy or None,
                "screenshot": f"data:image/jpeg;base64,{screenshot}" if screenshot else None,
                "source": source,
                "success_signals": signals,
            }
        )
    for index, raw in enumerate(actions_raw):
        if not isinstance(raw, Mapping):
            raise ValueError(f"verificationTrace.actions[{index}] must be an object")
        for field in ("preFrameId", "postFrameId"):
            frame_id = raw.get(field, -1)
            if not isinstance(frame_id, int) or isinstance(frame_id, bool):
                raise ValueError(f"verificationTrace.actions[{index}].{field} must be an integer")
            if frame_id >= 0 and frame_id not in frame_ids:
                raise ValueError(f"verificationTrace.actions[{index}] references an unknown frame")
    # A required runner that was itself environment-blocked can legitimately
    # have zero frames: its signed trace must still contain the failed
    # observation action.  Treat that as auditably insufficient evidence, not
    # as a malformed report.  A runner claiming completion still needs frames.
    runner_payload = payload.get("verificationRunnerResult", {})
    runner_status = (
        str(runner_payload.get("status", ""))
        if isinstance(runner_payload, Mapping)
        else ""
    ).upper()
    zero_frame_environment_block = (
        runner_status in {"ENV_BLOCKED", "UNSUPPORTED"}
        and len(actions_raw) > 0
    )
    if _verification_required(test_case) and not frames and not zero_frame_environment_block:
        raise ValueError("Harmony report requires verification evidence but verificationTrace has no frames")
    last = frames[-1] if frames else {}
    return ExecutionRecord(
        test_case_id=test_case.test_case_id,
        executor="harmony-uitest-verification-trace-v1",
        step_results=(),
        final_state=EvidenceState(
            visible_texts=tuple(last.get("visible_texts", ())),
            state_changed=None,
            success_signals=tuple(last.get("success_signals", ())),
            evidence_sufficient=bool(frames),
            notes=(
                ("isolated verification observation trace", "verification runner had no observable frame")
                if zero_frame_environment_block
                else ("isolated verification observation trace",)
            ),
        ),
        metadata={
            "frames": frames,
            "frame_visible_texts": {
                str(item["frame_id"]): list(item["visible_texts"])
                for item in frames
            },
            "verification_actions": [dict(item) for item in actions_raw],
            "trace_integrity": "VALID",
            "runner_status": runner_status,
        },
    )


def _validate_manifest_frame_hashes(
    manifest: Mapping[str, Any], context: str
) -> None:
    frames = manifest.get("frames", [])
    if not isinstance(frames, list):
        raise ValueError(f"{context}.frames must be a list")
    for index, raw in enumerate(frames):
        if not isinstance(raw, Mapping):
            raise ValueError(f"{context}.frames[{index}] must be an object")
        _validate_frame_hashes(raw, f"{context}.frames[{index}]")


def _validate_frame_hashes(frame: Mapping[str, Any], context: str) -> None:
    import hashlib

    hierarchy = frame.get("hierarchy", frame.get("hierarchyJson", ""))
    hierarchy_hash = frame.get("hierarchy_sha256", frame.get("hierarchyHash"))
    if hierarchy_hash is not None:
        if not isinstance(hierarchy, str) or not isinstance(hierarchy_hash, str):
            raise ValueError(f"{context} hierarchy hash fields must be strings")
        if hashlib.sha256(hierarchy.encode("utf-8")).hexdigest() != hierarchy_hash:
            raise ValueError(f"{context} hierarchy sha256 does not match payload")
    screenshot = frame.get("screenshot", frame.get("screenshotBase64", ""))
    screenshot_hash = frame.get("screenshot_sha256", frame.get("screenshotHash"))
    if screenshot_hash is not None:
        if not isinstance(screenshot, str) or not isinstance(screenshot_hash, str):
            raise ValueError(f"{context} screenshot hash fields must be strings")
        encoded = screenshot.split(",", 1)[1] if screenshot.startswith("data:") and "," in screenshot else screenshot
        if hashlib.sha256(encoded.encode("utf-8")).hexdigest() != screenshot_hash:
            raise ValueError(f"{context} screenshot sha256 does not match payload")


def _json_value(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, str):
        raise ValueError("Harmony report normalizedTestCase must be a JSON string")
    parsed = json.loads(value)
    if not isinstance(parsed, Mapping):
        raise ValueError("normalizedTestCase must decode to an object")
    return parsed


def _file_sha256(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_text(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode("utf-8")).hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
