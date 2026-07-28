"""Executor adapter that replays a step-level execution manifest."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .executor import ExecutionRecord
from .contract import compile_app_test_contract
from .manifest import load_execution_manifest
from .schema import TestCaseSpec


@dataclass(frozen=True)
class ManifestReplayExecutor:
    manifest_path: Path
    name: str = "manifest_replay"

    def execute(self, test_case: TestCaseSpec) -> ExecutionRecord:
        contract = compile_app_test_contract(test_case)
        manifest = load_execution_manifest(self.manifest_path, test_case, contract.sha256)
        record = manifest.to_execution_record(test_case, contract.sha256)
        return ExecutionRecord(
            test_case_id=record.test_case_id,
            executor=self.name,
            step_results=record.step_results,
            final_state=record.final_state,
            raw_trace_dir=record.raw_trace_dir,
            metadata={**dict(record.metadata), "manifest_path": str(self.manifest_path)},
        )
