#!/usr/bin/env python3
"""Exercise the trace-blind JIT compiler with deterministic valid/invalid mocks."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Optional


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from verification_benchmark.evaluation_framework import (  # noqa: E402
    JIT_COMPILER_VERSION,
    JIT_PROPOSAL_SCHEMA_VERSION,
    JitAppMetadata,
    JitCompilationError,
    JitCompileRequest,
    JitProposalResponse,
    JitStructuredOutputSpec,
    compile_jit_contract,
    jit_contract_proposal_json_schema,
    jit_structured_output_spec,
)


MOCK_JIT_AUDIT_SCHEMA_VERSION = "harmony-eval-mock-jit-compiler-audit-v1"
DEFAULT_SCHEMA_OUTPUT = (
    "verification_benchmark/schemas/jit_contract_proposal_v1.schema.json"
)
DEFAULT_AUDIT_OUTPUT = (
    "verification_benchmark/reports/jit_compiler/development/"
    "mock_jit_compiler_v1.audit.json"
)


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def valid_g1_proposal() -> dict[str, Any]:
    return {
        "schema_version": JIT_PROPOSAL_SCHEMA_VERSION,
        "task_family": "unknown_search",
        "justification": (
            "A persistently visible results surface directly represents completion "
            "of the requested search task."
        ),
        "required_capabilities": ["SCREENSHOT"],
        "criteria": [
            {
                "criterion_id": "outcome.results_surface",
                "temporal_semantics": "PERSISTENT_STATE",
                "required": True,
                "allow_obscured_persistence": False,
                "required_capabilities": ["SCREENSHOT"],
                "description": "The requested search results surface is persistently visible.",
            }
        ],
        "g1_bindings": [
            {
                "criterion_id": "outcome.results_surface",
                "checker": "ROI_STABILITY",
                "rois": [
                    {
                        "roi_id": "results_surface",
                        "bounds": [0.05, 0.18, 0.95, 0.94],
                        "coordinate_space": "NORMALIZED",
                        "reference_size": None,
                    }
                ],
            }
        ],
        "dag": None,
    }


def cyclic_dag_proposal() -> dict[str, Any]:
    def checker(word: str) -> dict[str, Any]:
        return {
            "checker_id": "text",
            "parameters": {
                "any": [word],
                "all": [],
                "none": [],
                "pattern": None,
                "ignore_case": None,
                "prompt": None,
                "expected_true": None,
            },
        }

    return {
        "schema_version": JIT_PROPOSAL_SCHEMA_VERSION,
        "task_family": "unknown_search",
        "justification": (
            "The declared success node represents the requested outcome state."
        ),
        "required_capabilities": [],
        "criteria": [
            {
                "criterion_id": "jit.dag_execution",
                "temporal_semantics": "EVENTUAL_STATE",
                "required": True,
                "allow_obscured_persistence": False,
                "required_capabilities": [],
                "description": "JIT DAG outcome.",
            }
        ],
        "g1_bindings": [],
        "dag": {
            "nodes": [
                {
                    "node_id": "first",
                    "condition_operator": "ANY_OF",
                    "score": 10,
                    "checkers": [checker("first")],
                },
                {
                    "node_id": "second",
                    "condition_operator": "ANY_OF",
                    "score": 10,
                    "checkers": [checker("second")],
                },
            ],
            "edges": [
                {"parent_id": "first", "child_id": "second", "kind": "NEXT_OR"},
                {"parent_id": "second", "child_id": "first", "kind": "NEXT_OR"},
            ],
            "success": {"operator": "ANY_OF", "node_ids": ["second"]},
        },
    }


class MockJITProposer:
    """In-memory proposer with no network, environment, filesystem, or trace access."""

    proposer_id = "mock-jit-proposer"
    proposer_version = "mock-jit-proposer-v1"

    def __init__(
        self,
        proposal: Optional[Mapping[str, Any]] = None,
        *,
        raw_json: Optional[bytes] = None,
        refusal: Optional[str] = None,
    ) -> None:
        if sum(item is not None for item in (proposal, raw_json, refusal)) != 1:
            raise ValueError("MockJITProposer requires exactly one response source")
        self._proposal = None if proposal is None else copy.deepcopy(dict(proposal))
        self._raw_json = raw_json
        self._refusal = refusal
        self.calls = 0
        self.last_request: Optional[dict[str, Any]] = None
        self.last_schema_sha256: Optional[str] = None

    def propose(
        self,
        request: Mapping[str, Any],
        *,
        response_format: JitStructuredOutputSpec,
    ) -> JitProposalResponse:
        if set(request) != {"task_description", "app_metadata"}:
            raise AssertionError("JIT compiler leaked a forbidden top-level input")
        if not response_format.strict:
            raise AssertionError("JIT compiler disabled strict Structured Outputs")
        rendered = response_format.openai_text_format()
        if rendered["type"] != "json_schema" or rendered["strict"] is not True:
            raise AssertionError("JIT response format is not strict json_schema")
        self.calls += 1
        self.last_request = {
            "task_description": request["task_description"],
            "app_metadata": dict(request["app_metadata"]),
        }
        self.last_schema_sha256 = response_format.schema_sha256
        if self._refusal is not None:
            return JitProposalResponse(refusal=self._refusal)
        if self._raw_json is not None:
            return JitProposalResponse(json_bytes=self._raw_json)
        return JitProposalResponse(json_bytes=_canonical_bytes(self._proposal))


def mock_request() -> JitCompileRequest:
    return JitCompileRequest(
        task_description="在未知购物应用中搜索机械键盘并进入结果页",
        app_metadata=JitAppMetadata(
            app_id="com.example.unknownshop",
            app_name="未知购物应用",
            platform="HarmonyOS",
            app_version=None,
            task_family="unknown_search",
            risk_tier="MEDIUM",
        ),
    )


def run_mock_matrix() -> dict[str, Any]:
    request = mock_request()
    valid_proposer = MockJITProposer(valid_g1_proposal())
    valid = compile_jit_contract(request, valid_proposer)
    cases: dict[str, MockJITProposer] = {}

    dangling = valid_g1_proposal()
    dangling["g1_bindings"][0]["criterion_id"] = "outcome.hallucinated"
    cases["dangling_binding"] = MockJITProposer(dangling)

    out_of_bounds = valid_g1_proposal()
    out_of_bounds["g1_bindings"][0]["rois"][0]["bounds"] = [0.1, 0.2, 1.2, 0.9]
    cases["out_of_bounds_roi"] = MockJITProposer(out_of_bounds)
    cases["cyclic_dag"] = MockJITProposer(cyclic_dag_proposal())

    unsupported = cyclic_dag_proposal()
    unsupported["dag"]["edges"] = []
    unsupported["dag"]["nodes"] = unsupported["dag"]["nodes"][:1]
    unsupported["dag"]["success"]["node_ids"] = ["first"]
    unsupported["dag"]["nodes"][0]["checkers"][0][
        "checker_id"
    ] = "hallucinated_magic"
    cases["unsupported_checker"] = MockJITProposer(unsupported)

    extra = valid_g1_proposal()
    extra["trace_summary"] = "forbidden"
    cases["unknown_field"] = MockJITProposer(extra)

    missing_justification = valid_g1_proposal()
    del missing_justification["justification"]
    cases["missing_justification"] = MockJITProposer(missing_justification)

    multi_sentence_justification = valid_g1_proposal()
    multi_sentence_justification["justification"] = (
        "The result page is visible. This is sufficient."
    )
    cases["multi_sentence_justification"] = MockJITProposer(
        multi_sentence_justification
    )
    cases["duplicate_json_key"] = MockJITProposer(
        raw_json=(
            b'{"schema_version":"harmony-eval-jit-contract-proposal-v1",'
            b'"schema_version":"duplicate"}'
        )
    )
    cases["refusal"] = MockJITProposer(refusal="mock refusal")

    rejected = {}
    for case_id, proposer in cases.items():
        try:
            compile_jit_contract(request, proposer)
        except JitCompilationError as exc:
            rejected[case_id] = {
                "status": "REJECTED",
                "failure_code": exc.code.value,
                "validation_code": (
                    None if exc.validation_code is None else exc.validation_code.value
                ),
                "proposer_calls": proposer.calls,
            }
        else:
            raise AssertionError(f"invalid mock JIT case was accepted: {case_id}")

    spec = jit_structured_output_spec()
    audit = {
        "schema_version": MOCK_JIT_AUDIT_SCHEMA_VERSION,
        "compiler_version": JIT_COMPILER_VERSION,
        "input_boundary": {
            "top_level_fields": ["app_metadata", "task_description"],
            "input_sha256": request.input_sha256,
            "selection_key": request.selection_key,
            "trace_artifacts_supplied": 0,
        },
        "structured_output": {
            "type": "json_schema",
            "name": spec.name,
            "strict": spec.strict,
            "schema_sha256": spec.schema_sha256,
        },
        "valid_case": {
            "status": "VALIDATED_AND_FROZEN",
            "contract_sha256": valid.contract_sha256,
            "proposal_sha256": valid.proposal_sha256,
            "justification": valid.justification,
            "justification_sha256": hashlib.sha256(
                valid.justification.encode("utf-8")
            ).hexdigest(),
            "justification_role": "HUMAN_AUDIT_ONLY",
            "validation_funnel_version": valid.validation_funnel_version,
            "source_type": valid.contract.compiler_provenance.source_type.value,
            "proposer_calls": valid_proposer.calls,
        },
        "invalid_cases": rejected,
        "security": {
            "real_llm_requests": 0,
            "real_http_requests": 0,
            "api_key_reads": 0,
            "screenshots_read": 0,
            "xml_files_read": 0,
            "action_files_read": 0,
            "external_cost": 0,
        },
        "claim_boundary": {
            "permitted": "Mock-only JIT compile, validate, and hash-freeze closure.",
            "forbidden": [
                "real model quality",
                "trace-conditioned contract generation",
                "held-out performance",
                "automatic live-provider authorization",
                "automatic semantic relevance certification from justification",
            ],
        },
    }
    audit["audit_sha256"] = _digest(audit)
    return audit


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schema-output", default=DEFAULT_SCHEMA_OUTPUT)
    parser.add_argument("--audit-output", default=DEFAULT_AUDIT_OUTPUT)
    args = parser.parse_args(argv)
    schema_path = Path(args.schema_output)
    audit_path = Path(args.audit_output)
    if not schema_path.is_absolute():
        schema_path = ROOT / schema_path
    if not audit_path.is_absolute():
        audit_path = ROOT / audit_path
    schema = jit_contract_proposal_json_schema()
    audit = run_mock_matrix()
    _write_json(schema_path, schema)
    _write_json(audit_path, audit)
    print(
        json.dumps(
            {
                "status": "MOCK_JIT_COMPILER_PASS",
                "contract_sha256": audit["valid_case"]["contract_sha256"],
                "schema_sha256": audit["structured_output"]["schema_sha256"],
                "invalid_cases_rejected": len(audit["invalid_cases"]),
                "real_llm_requests": 0,
                "audit_sha256": audit["audit_sha256"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "MockJITProposer",
    "cyclic_dag_proposal",
    "mock_request",
    "run_mock_matrix",
    "valid_g1_proposal",
]
