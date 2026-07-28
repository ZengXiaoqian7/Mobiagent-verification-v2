"""App-test contract compiler.

The contract is compiled only from TestCaseSpec.  It freezes execution
constraints and App behavior assertions before any trace or runner output is
examined.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping

from .offline_verifier import compile_offline_oracle_contract
from .schema import TestCaseSpec
from .step_intent import compile_step_execution_intent


APP_TEST_CONTRACT_SCHEMA_VERSION = "app-test-contract-v1"
def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


@dataclass(frozen=True)
class AppTestContract:
    test_case_id: str
    test_case_sha256: str
    execution_contract: Mapping[str, Any]
    app_oracle_contract: Mapping[str, Any]
    verification_contract: Mapping[str, Any]
    observation_policy: Mapping[str, Any]
    schema_version: str = APP_TEST_CONTRACT_SCHEMA_VERSION

    @property
    def sha256(self) -> str:
        return self.sha256_without_self_reference()

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "test_case_id": self.test_case_id,
            "test_case_sha256": self.test_case_sha256,
            "contract_sha256": self.sha256,
            "execution_contract": dict(self.execution_contract),
            "app_oracle_contract": dict(self.app_oracle_contract),
            "verification_contract": dict(self.verification_contract),
            "observation_policy": dict(self.observation_policy),
        }

    def sha256_without_self_reference(self) -> str:
        payload = {
            "schema_version": self.schema_version,
            "test_case_id": self.test_case_id,
            "test_case_sha256": self.test_case_sha256,
            "execution_contract": dict(self.execution_contract),
            "app_oracle_contract": dict(self.app_oracle_contract),
            "verification_contract": dict(self.verification_contract),
            "observation_policy": dict(self.observation_policy),
        }
        return hashlib.sha256(_canonical_bytes(payload)).hexdigest()


def compile_app_test_contract(test_case: TestCaseSpec) -> AppTestContract:
    execution_contract = {
        "step_order": [step.step_id for step in test_case.steps],
        "steps": [
            {
                "step_id": step.step_id,
                "instruction": step.instruction,
                "action_type": step.action_type,
                "step_mode": step.step_mode,
                "target": dict(step.target),
                "target_is_legacy_hint": bool(step.target),
                "runtime_intent": compile_step_execution_intent(step, test_case).as_dict(),
                "expected_value": step.resolved_value(test_case.test_data),
                "max_retries": step.max_retries,
                "timeout_seconds": step.timeout_seconds,
            }
            for step in test_case.steps
        ],
        "preconditions": [item.as_dict() for item in test_case.preconditions],
        "runner_constraints": {
            "preserve_step_order": True,
            "do_not_modify_test_data": True,
            "goal_step_allows_internal_micro_actions": True,
            "goal_step_must_not_skip_next_user_step": True,
            "runner_done_is_step_done_only": True,
            "app_result_not_decided_by_runner": True,
        },
        "runtime_generated_data": dict(test_case.runtime_generated_data),
    }
    app_oracle_contract = {
        "expected_results": [
            {
                **assertion.as_dict(),
                "resolved_expected_value": assertion.resolved_value(
                    test_case.test_data
                ),
            }
            for assertion in test_case.expected_results
        ],
        "forbidden_effects": [
            {
                **effect.as_dict(),
                "resolved_values": list(effect.resolved_values(test_case.test_data)),
            }
            for effect in test_case.forbidden_effects
        ],
        "forbidden_effects_are_required_absence_constraints": True,
        "offline_verifier_contract": {
            "schema_version": "app-test-offline-oracle-contract-v1",
            "compiled_assertions": list(compile_offline_oracle_contract(test_case)),
            "responsibility": (
                "offline verifier extracts evidence; App-test oracle maps evidence "
                "to expected_results and attribution"
            ),
            "runner_self_report_is_not_outcome_evidence": True,
        },
    }
    verification_contract = {
        "runner_policy": test_case.verification_runner_policy,
        "steps": [
            {
                "verification_step_id": step.verification_step_id,
                "action_type": step.action_type,
                "target": dict(step.target),
                "max_retries": step.max_retries,
                "timeout_seconds": step.timeout_seconds,
                "read_only_action": step.is_read_only,
            }
            for step in test_case.verification_steps
        ],
        "policy": dict(test_case.verification_policy),
        "constraints": {
            "read_only_only": True,
            "separate_from_business_steps": True,
            "app_result_not_decided_by_runner": True,
        },
    }
    return AppTestContract(
        test_case_id=test_case.test_case_id,
        test_case_sha256=test_case.sha256,
        execution_contract=execution_contract,
        app_oracle_contract=app_oracle_contract,
        verification_contract=verification_contract,
        observation_policy=dict(test_case.observation_policy),
    )
