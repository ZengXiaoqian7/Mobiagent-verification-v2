"""Verification-benchmark intake adapter for App-test execution manifests."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from app_test_agent.contract import AppTestContract, compile_app_test_contract
from app_test_agent.executor import ExecutionRecord
from app_test_agent.manifest import ManifestIntakeError, TestExecutionManifest
from app_test_agent.schema import TestCaseSpec

from .phase5_intake import file_sha256, semantic_sha256, strict_json_bytes
from .trace_adapter import TraceEvidenceBundle, load_trace_directory


@dataclass(frozen=True)
class AppTestManifestEvidenceCase:
    test_case: TestCaseSpec
    contract: AppTestContract
    manifest: TestExecutionManifest
    execution_record: ExecutionRecord
    manifest_file_sha256: str
    manifest_semantic_sha256: str
    trace_bundle: TraceEvidenceBundle | None = None

    def as_intake_summary(self) -> dict[str, Any]:
        return {
            "adapter": "verification_benchmark.app_test_manifest_intake",
            "test_case_id": self.test_case.test_case_id,
            "test_case_sha256": self.test_case.sha256,
            "contract_sha256": self.contract.sha256,
            "manifest_sha256": self.manifest.sha256,
            "manifest_file_sha256": self.manifest_file_sha256,
            "manifest_semantic_sha256": self.manifest_semantic_sha256,
            "trace_capability": (
                {
                    "trace_ref": self.trace_bundle.trace_ref,
                    "integrity": self.trace_bundle.capability_profile.integrity.value,
                    "screenshot_frames": list(
                        self.trace_bundle.capability_profile.screenshot_frames
                    ),
                    "hierarchy_xml_frames": list(
                        self.trace_bundle.capability_profile.hierarchy_xml_frames
                    ),
                    "hierarchy_raw_json_frames": list(
                        self.trace_bundle.capability_profile.hierarchy_raw_json_frames
                    ),
                }
                if self.trace_bundle is not None
                else None
            ),
        }


def load_app_test_manifest_evidence(
    *,
    test_case: TestCaseSpec,
    manifest_path: Path,
) -> AppTestManifestEvidenceCase:
    source = manifest_path.resolve(strict=True)
    payload: Mapping[str, Any] = strict_json_bytes(
        source.read_bytes(),
        context="App-test execution manifest",
    )
    contract = compile_app_test_contract(test_case)
    manifest = TestExecutionManifest.from_json(payload)
    try:
        manifest.validate_against(test_case, contract.sha256)
    except ManifestIntakeError:
        raise
    except Exception as exc:  # noqa: BLE001 - external adapter must fail closed.
        raise ManifestIntakeError(f"App-test manifest intake failed: {exc}") from exc
    record = manifest.to_execution_record(test_case, contract.sha256)
    trace_bundle = None
    if manifest.raw_trace_dir:
        trace_bundle = load_trace_directory(manifest.raw_trace_dir)
    return AppTestManifestEvidenceCase(
        test_case=test_case,
        contract=contract,
        manifest=manifest,
        execution_record=record,
        manifest_file_sha256=file_sha256(source),
        manifest_semantic_sha256=semantic_sha256(payload),
        trace_bundle=trace_bundle,
    )


__all__ = [
    "AppTestManifestEvidenceCase",
    "load_app_test_manifest_evidence",
]
