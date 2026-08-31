"""Verification-benchmark intake adapter for App-test execution manifests."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

from app_test_agent.contract import AppTestContract, compile_app_test_contract
from app_test_agent.executor import ExecutionRecord
from app_test_agent.manifest import ManifestIntakeError, TestExecutionManifest
from app_test_agent.raw_step_gate_replay import recompute_step_gates_from_raw_trace
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
    compatibility_migration: Mapping[str, Any] | None = None

    def as_intake_summary(self) -> dict[str, Any]:
        return {
            "adapter": "verification_benchmark.app_test_manifest_intake",
            "test_case_id": self.test_case.test_case_id,
            "test_case_sha256": self.test_case.sha256,
            "contract_sha256": self.contract.sha256,
            "manifest_sha256": self.manifest.sha256,
            "manifest_file_sha256": self.manifest_file_sha256,
            "manifest_semantic_sha256": self.manifest_semantic_sha256,
            "compatibility_migration": (
                dict(self.compatibility_migration)
                if self.compatibility_migration is not None
                else None
            ),
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
    test_case_path: Path | None = None,
    recompute_step_gates: bool = False,
) -> AppTestManifestEvidenceCase:
    source = manifest_path.resolve(strict=True)
    payload: Mapping[str, Any] = strict_json_bytes(
        source.read_bytes(),
        context="App-test execution manifest",
    )
    contract = compile_app_test_contract(test_case)
    manifest = TestExecutionManifest.from_json(payload)
    compatibility_migration: Mapping[str, Any] | None = None
    replay_test_case = test_case
    try:
        manifest.validate_against(test_case, contract.sha256)
    except ManifestIntakeError:
        if test_case_path is None:
            raise
        compatibility_migration, replay_test_case = _validate_legacy_binding(
            test_case=test_case,
            test_case_path=test_case_path,
            manifest=manifest,
            manifest_path=source,
            current_contract=contract,
        )
    except Exception as exc:  # noqa: BLE001 - external adapter must fail closed.
        raise ManifestIntakeError(f"App-test manifest intake failed: {exc}") from exc
    record = manifest.to_execution_record(
        replay_test_case,
        contract.sha256 if compatibility_migration is None else None,
    )
    if compatibility_migration is not None:
        record = replace(
            record,
            metadata={
                **dict(record.metadata),
                "manifest_compatibility_migration": dict(compatibility_migration),
                "current_contract_sha256": contract.sha256,
            },
        )
    if recompute_step_gates:
        record = recompute_step_gates_from_raw_trace(test_case, record)
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
        compatibility_migration=compatibility_migration,
    )


def _validate_legacy_binding(
    *,
    test_case: TestCaseSpec,
    test_case_path: Path,
    manifest: TestExecutionManifest,
    manifest_path: Path,
    current_contract: AppTestContract,
) -> tuple[Mapping[str, Any], TestCaseSpec]:
    """Validate an explicitly registered, hash-preserving schema migration.

    Compatibility never accepts a manifest merely because its IDs and step
    names look plausible.  The exact legacy normalized test-case payload and
    its exact frozen Contract must both match the hashes recorded by the
    manifest before a registered semantic migration is applied.
    """

    source_case = test_case_path.resolve(strict=True)
    legacy_case_payload = strict_json_bytes(
        source_case.read_bytes(),
        context="legacy normalized App-test case",
    )
    legacy_test_case_sha256 = semantic_sha256(legacy_case_payload)
    if legacy_test_case_sha256 != manifest.test_case_sha256:
        raise ManifestIntakeError(
            "legacy source test_case_sha256 does not match execution manifest"
        )

    migrated_case_payload = deepcopy(dict(legacy_case_payload))
    migration_ids: list[str] = []
    observation_policy = migrated_case_payload.get("observation_policy")
    if isinstance(observation_policy, Mapping) and "adaptive_capture" not in observation_policy:
        migrated_case_payload["observation_policy"] = {
            **dict(observation_policy),
            "adaptive_capture": False,
        }
        migration_ids.append(
            "test_case.observation_policy.adaptive_capture_default_false"
        )
    # A registered Contract-only migration can be required even when the
    # normalized test case itself did not change.  The migrated Contract is
    # still compared in full below, so accepting an unchanged case here does
    # not weaken binding or permit unregistered Contract drift.
    if migrated_case_payload != test_case.as_dict():
        raise ManifestIntakeError(
            "legacy test case does not match the current schema after a registered compatibility migration"
        )

    legacy_contract_path = manifest_path.parent / "app_test_contract.json"
    if not legacy_contract_path.is_file():
        raise ManifestIntakeError(
            "legacy manifest compatibility requires sibling app_test_contract.json"
        )
    legacy_contract_payload = strict_json_bytes(
        legacy_contract_path.read_bytes(),
        context="legacy App-test contract",
    )
    declared_contract_sha256 = legacy_contract_payload.get("contract_sha256")
    if declared_contract_sha256 != manifest.contract_sha256:
        raise ManifestIntakeError(
            "legacy contract_sha256 does not match execution manifest"
        )
    unhashed_legacy_contract = deepcopy(dict(legacy_contract_payload))
    unhashed_legacy_contract.pop("contract_sha256", None)
    if semantic_sha256(unhashed_legacy_contract) != manifest.contract_sha256:
        raise ManifestIntakeError("legacy App-test contract self-hash is invalid")
    if legacy_contract_payload.get("test_case_sha256") != manifest.test_case_sha256:
        raise ManifestIntakeError(
            "legacy App-test contract is not bound to the manifest test case"
        )

    migrated_contract = deepcopy(unhashed_legacy_contract)
    migrated_contract["test_case_sha256"] = test_case.sha256
    contract_observation = migrated_contract.get("observation_policy")
    if isinstance(contract_observation, Mapping) and "adaptive_capture" not in contract_observation:
        migrated_contract["observation_policy"] = {
            **dict(contract_observation),
            "adaptive_capture": False,
        }
    app_oracle = migrated_contract.get("app_oracle_contract")
    if (
        isinstance(app_oracle, Mapping)
        and "forbidden_effects_ignored_v1" in app_oracle
        and "forbidden_effects" not in app_oracle
    ):
        migrated_oracle = dict(app_oracle)
        migrated_oracle["forbidden_effects"] = migrated_oracle.pop(
            "forbidden_effects_ignored_v1"
        )
        migrated_oracle["forbidden_effects_are_required_absence_constraints"] = True
        migrated_contract["app_oracle_contract"] = migrated_oracle
        migration_ids.append("contract.app_oracle.forbidden_effects_v1")

    expected_current_contract = current_contract.as_dict()
    expected_current_contract.pop("contract_sha256", None)
    legacy_steps = (
        migrated_contract.get("execution_contract", {}).get("steps", [])
        if isinstance(migrated_contract.get("execution_contract"), Mapping)
        else []
    )
    current_steps = (
        expected_current_contract.get("execution_contract", {}).get("steps", [])
        if isinstance(expected_current_contract.get("execution_contract"), Mapping)
        else []
    )
    added_goal_action_policy = False
    if isinstance(legacy_steps, list) and isinstance(current_steps, list):
        current_by_id = {
            str(item.get("step_id")): item
            for item in current_steps
            if isinstance(item, Mapping)
        }
        for legacy_step in legacy_steps:
            if not isinstance(legacy_step, dict):
                continue
            current_step = current_by_id.get(str(legacy_step.get("step_id")))
            legacy_runtime = legacy_step.get("runtime_intent")
            current_runtime = current_step.get("runtime_intent") if isinstance(current_step, Mapping) else None
            legacy_intent = legacy_runtime.get("execution_intent") if isinstance(legacy_runtime, Mapping) else None
            current_intent = current_runtime.get("execution_intent") if isinstance(current_runtime, Mapping) else None
            if (
                isinstance(legacy_intent, dict)
                and isinstance(current_intent, Mapping)
                and "allowed_micro_action_families" not in legacy_intent
            ):
                legacy_intent["allowed_micro_action_families"] = deepcopy(
                    current_intent.get("allowed_micro_action_families", [])
                )
                added_goal_action_policy = True
    if added_goal_action_policy:
        migration_ids.append(
            "contract.step_runtime_intent.allowed_micro_action_families_v1"
        )
    if migrated_contract != expected_current_contract:
        raise ManifestIntakeError(
            "legacy App-test contract differs beyond a registered compatibility migration"
        )
    if not migration_ids:
        raise ManifestIntakeError(
            "legacy binding did not require a registered compatibility migration"
        )

    legacy_observation_policy = legacy_case_payload.get("observation_policy")
    if not isinstance(legacy_observation_policy, Mapping):
        raise ManifestIntakeError("legacy test case observation_policy is malformed")
    replay_test_case = replace(
        test_case,
        observation_policy=dict(legacy_observation_policy),
    )
    if replay_test_case.sha256 != manifest.test_case_sha256:
        raise ManifestIntakeError(
            "registered migration could not reconstruct the manifest test_case_sha256"
        )

    receipt = {
        "schema_version": "app-test-manifest-compatibility-receipt-v1",
        "status": "MIGRATED",
        "migration_ids": migration_ids,
        "legacy_test_case_sha256": manifest.test_case_sha256,
        "current_test_case_sha256": test_case.sha256,
        "legacy_contract_sha256": manifest.contract_sha256,
        "current_contract_sha256": current_contract.sha256,
        "legacy_test_case_file_sha256": file_sha256(source_case),
        "legacy_contract_file_sha256": file_sha256(legacy_contract_path),
        "legacy_test_case_path": str(source_case),
        "legacy_contract_path": str(legacy_contract_path.resolve()),
    }
    return receipt, replay_test_case


__all__ = [
    "AppTestManifestEvidenceCase",
    "load_app_test_manifest_evidence",
]
