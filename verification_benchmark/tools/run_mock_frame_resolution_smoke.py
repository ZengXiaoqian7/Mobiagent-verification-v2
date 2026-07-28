#!/usr/bin/env python3
"""Run one deterministic Mock-only Recorder -> cache -> offline replay smoke."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from verification_benchmark.evaluation_framework import (  # noqa: E402
    EvidenceCacheKey,
    LegacyCheckerSignal,
    PrecomputedEvidenceStorage,
    RecordedProviderBindingIR,
    RecordedProviderContext,
    RecordedProviderKind,
    RecordedProviderPlan,
    RunVerdict,
    acquire_and_evaluate_legacy_contract,
    acquire_legacy_checker_outcomes,
    adapt_legacy_yaml,
    contract_sha256,
    load_local_legacy_checker_evidence,
    load_trace_directory,
    trace_bundle_to_event_trace,
)
from verification_benchmark.evaluation_framework.action_frame_resolver import (  # noqa: E402
    RESOLVED_ACTION_BOUNDARY,
)
from verification_benchmark.tools import record_precomputed_evidence as recorder  # noqa: E402


MOCK_SMOKE_CONFIG_SCHEMA_VERSION = "harmony-eval-mock-live-smoke-config-v1"
MOCK_SMOKE_AUDIT_SCHEMA_VERSION = "harmony-eval-mock-live-smoke-audit-v1"
DEFAULT_CATALOG = "verification_benchmark/reports/recording_planner/development/v2/catalog.json"
DEFAULT_CONFIG = (
    "verification_benchmark/recording_planner/development/"
    "mock_live_smoke_v1.config.json"
)
DEFAULT_OUTPUT = (
    "verification_benchmark/reports/recording_planner/development/v2/"
    "mock_live_smoke.audit.json"
)
_CONFIG_FIELDS = {
    "schema_version",
    "resolved_catalog_sha256",
    "trace_id",
    "contract_ref",
    "contract_sha256",
    "node_id",
    "checker_id",
    "provider_kind",
    "frame_index",
    "model_version",
    "mock_decision",
    "request_budget",
    "max_attempts",
}


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _load_object(path: Path, context: str) -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicates
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"{context} is not strict UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{context} must be a JSON object")
    return value


def _digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_config(config: Mapping[str, Any], catalog: Mapping[str, Any]) -> None:
    if set(config) != _CONFIG_FIELDS:
        raise ValueError("mock smoke config fields differ from the frozen schema")
    if config["schema_version"] != MOCK_SMOKE_CONFIG_SCHEMA_VERSION:
        raise ValueError("unsupported mock smoke config schema")
    if config["resolved_catalog_sha256"] != catalog.get("catalog_sha256"):
        raise ValueError("mock smoke config is bound to a different resolved catalog")
    if config["provider_kind"] != "LLM" or config["checker_id"] != "llm":
        raise ValueError("mock smoke v1 permits exactly one LLM checker target")
    if config["model_version"] != recorder.AUTHORIZED_MODEL:
        raise ValueError("mock smoke model differs from the recorder model")
    if config["mock_decision"] is not True:
        raise ValueError("mock smoke v1 decision is frozen to true")
    for field in ("request_budget", "max_attempts"):
        if config[field] != 1:
            raise ValueError(f"mock smoke {field} must be exactly one")
    if not isinstance(config["frame_index"], int) or isinstance(
        config["frame_index"], bool
    ):
        raise ValueError("mock smoke frame_index must be an integer")


def _select_target(
    catalog: Mapping[str, Any], config: Mapping[str, Any]
) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    candidates = [
        item
        for item in catalog["candidate_records"]
        if item["trace_id"] == config["trace_id"]
        and item["contract_ref"] == config["contract_ref"]
        and item["contract_sha256"] == config["contract_sha256"]
        and item["node_id"] == config["node_id"]
        and item["checker_id"] == config["checker_id"]
        and item["provider_kind"] == config["provider_kind"]
    ]
    if len(candidates) != 1:
        raise ValueError("mock smoke target candidate is not unique")
    candidate = candidates[0]
    selected = candidate.get("selected_screenshot")
    if (
        candidate.get("selection_status") != RESOLVED_ACTION_BOUNDARY
        or not isinstance(selected, Mapping)
        or selected.get("frame_index") != config["frame_index"]
    ):
        raise ValueError("mock smoke target is not action-bound to the configured frame")
    jobs = [
        job
        for job in catalog["unique_jobs"]
        if job.get("composite_key") == candidate.get("composite_key")
        and any(
            consumer.get("trace_id") == config["trace_id"]
            and consumer.get("node_id") == config["node_id"]
            and consumer.get("checker_id") == config["checker_id"]
            for consumer in job.get("consumers", [])
        )
    ]
    if len(jobs) != 1 or jobs[0].get("cache_status") != "MISS":
        raise ValueError("mock smoke target must identify one uncached unique job")
    job = jobs[0]
    if (
        job["screenshot_ref"] != selected["screenshot_ref"]
        or job["screenshot_sha256"] != selected["screenshot_sha256"]
        or job["model_version"] != config["model_version"]
    ):
        raise ValueError("mock smoke job differs from the resolved frame")
    return candidate, job


class DeterministicMockTransport:
    """In-memory transport; it cannot open sockets or read an API key."""

    def __init__(self, *, model: str, decision: bool) -> None:
        self.model = model
        self.decision = decision
        self.calls = 0

    def complete(
        self, payload: Mapping[str, Any], *, timeout: float
    ) -> recorder.TransportResponse:
        if payload.get("model") != self.model or timeout <= 0:
            raise ValueError("mock transport request drift")
        self.calls += 1
        body = {
            "id": "mock-frame-resolution-smoke-v1",
            "model": self.model,
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {"decision": self.decision},
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                    }
                }
            ],
            "usage": {"prompt_tokens": 12, "completion_tokens": 3},
        }
        return recorder.TransportResponse(
            json.dumps(
                body,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8"),
            0.0,
        )


def run_mock_smoke(
    repository_root: Path | str,
    *,
    catalog_path: Path | str,
    config_path: Path | str,
) -> dict[str, Any]:
    root = Path(repository_root).resolve()

    def rooted(value: Path | str) -> Path:
        path = Path(value)
        return path if path.is_absolute() else root / path

    catalog = _load_object(rooted(catalog_path), "resolved catalog")
    config = _load_object(rooted(config_path), "mock smoke config")
    _validate_config(config, catalog)
    candidate, job = _select_target(catalog, config)
    request = dict(job["request"])
    request.pop("selection_source", None)
    manifest_payload = {
        "schema_version": recorder.RECORDING_MANIFEST_SCHEMA_VERSION,
        "model_version": config["model_version"],
        "jobs": [
            {
                "job_id": job["job_id"],
                "provider_kind": job["provider_kind"],
                "screenshot_ref": job["screenshot_ref"],
                "screenshot_sha256": job["screenshot_sha256"],
                "request": request,
            }
        ],
    }
    transport = DeterministicMockTransport(
        model=config["model_version"], decision=config["mock_decision"]
    )
    with tempfile.TemporaryDirectory(prefix="harmony-mock-smoke-") as temp_name:
        temp = Path(temp_name)
        manifest_path = temp / "manifest.json"
        cache_path = temp / "cache.jsonl"
        receipt_path = temp / "receipt.json"
        manifest_path.write_text(
            json.dumps(manifest_payload, ensure_ascii=False, sort_keys=True, indent=2)
            + "\n",
            encoding="utf-8",
            newline="\n",
        )
        manifest = recorder.load_recording_manifest(manifest_path)
        run = recorder.run_recording(
            manifest,
            screenshot_root=root / "verification_benchmark/traces",
            cache_path=cache_path,
            receipt_path=receipt_path,
            base_url=recorder.AUTHORIZED_BASE_URL,
            model=recorder.AUTHORIZED_MODEL,
            request_budget=config["request_budget"],
            max_attempts=config["max_attempts"],
            transport=transport,
        )
        storage = PrecomputedEvidenceStorage.from_jsonl(cache_path)
        receipt = _load_object(receipt_path, "mock recorder receipt")
        entry = storage.lookup(
            RecordedProviderKind.LLM, EvidenceCacheKey(**job["composite_key"])
        )
        if entry is None:
            raise ValueError("mock recorder cache does not contain the exact target key")

        contract_path = rooted(config["contract_ref"])
        contract = adapt_legacy_yaml(
            contract_path, source_ref=config["contract_ref"]
        ).contract
        if contract_sha256(contract) != config["contract_sha256"]:
            raise ValueError("mock smoke contract hash drift")
        binding = RecordedProviderBindingIR(
            node_id=config["node_id"],
            checker_id=config["checker_id"],
            provider_kind=RecordedProviderKind.LLM,
            model_version=manifest.model_version,
            request=manifest.jobs[0].request,
        )
        plan = RecordedProviderPlan(contract_sha256(contract), (binding,))
        plan.validate_against(contract)
        trace_root = root / "verification_benchmark/traces" / config["trace_id"]
        bundle = load_trace_directory(trace_root, trace_ref=config["trace_id"])
        durable = trace_bundle_to_event_trace(
            bundle, contract, trace_id=config["trace_id"]
        )
        evidence = load_local_legacy_checker_evidence(durable, trace_root)
        context = RecordedProviderContext(storage, plan)
        table = acquire_legacy_checker_outcomes(
            contract, evidence, recorded_context=context
        )
        matches = [
            outcome
            for outcome in table.outcomes
            if outcome.node_id == config["node_id"]
            and outcome.checker_id == config["checker_id"]
            and outcome.frame_index == config["frame_index"]
        ]
        if len(matches) != 1 or matches[0].signal is not LegacyCheckerSignal.MATCH:
            raise ValueError("mock cached evidence did not close the target checker")
        evaluation = acquire_and_evaluate_legacy_contract(
            contract,
            evidence,
            deadline_reached=True,
            recorded_context=context,
        )
        if evaluation.report.outcome_verdict is not RunVerdict.UNSUPPORTED:
            raise ValueError("single synthetic checker unexpectedly supported the contract")
        exact_key = EvidenceCacheKey(**job["composite_key"])
        drift_misses = {
            "screenshot_sha256": storage.lookup(
                RecordedProviderKind.LLM,
                EvidenceCacheKey(
                    "0" * 64, exact_key.model_version, exact_key.request_sha256
                ),
            )
            is None,
            "model_version": storage.lookup(
                RecordedProviderKind.LLM,
                EvidenceCacheKey(
                    exact_key.screenshot_sha256,
                    exact_key.model_version + "-drift",
                    exact_key.request_sha256,
                ),
            )
            is None,
            "request_sha256": storage.lookup(
                RecordedProviderKind.LLM,
                EvidenceCacheKey(
                    exact_key.screenshot_sha256,
                    exact_key.model_version,
                    "0" * 64,
                ),
            )
            is None,
        }
        if not all(drift_misses.values()):
            raise ValueError("mock cache accepted a drifted composite key")
        receipt_item = receipt["sessions"][0]["items"][0]
        audit = {
            "schema_version": MOCK_SMOKE_AUDIT_SCHEMA_VERSION,
            "mock_only": True,
            "resolved_catalog_sha256": catalog["catalog_sha256"],
            "config_sha256": _digest(config),
            "target": {
                "trace_id": config["trace_id"],
                "contract_ref": config["contract_ref"],
                "contract_sha256": config["contract_sha256"],
                "node_id": config["node_id"],
                "checker_id": config["checker_id"],
                "provider_kind": config["provider_kind"],
                "frame_index": config["frame_index"],
                "selection_status": candidate["selection_status"],
                "selection_source_projection_sha256": candidate[
                    "selected_screenshot"
                ]["selection_source_projection_sha256"],
                "screenshot_sha256": job["screenshot_sha256"],
                "request_sha256": job["request_sha256"],
            },
            "recorder": {
                "manifest_sha256": manifest.manifest_sha256,
                "model_version": manifest.model_version,
                "request_budget": config["request_budget"],
                "max_attempts": config["max_attempts"],
                "status": run.status,
                "recorded": run.recorded,
                "cached": run.cached,
                "errors": run.errors,
                "requests": run.requests,
                "mock_transport_calls": transport.calls,
                "cache_storage_sha256": storage.storage_sha256,
                "response_sha256": receipt_item["response_sha256"],
            },
            "offline_replay": {
                "provider_plan_sha256": plan.plan_sha256,
                "exact_cache_lookup": True,
                "target_signal": matches[0].signal.value,
                "whole_contract_verdict": evaluation.report.outcome_verdict.value,
                "drifted_key_misses": drift_misses,
            },
            "retention": {
                "cache_persisted": False,
                "receipt_persisted": False,
                "provider_output_persisted": False,
                "audit_only": True,
            },
            "security": {
                "real_http_requests": 0,
                "api_key_reads": 0,
                "external_cost": 0,
            },
            "claim_boundary": {
                "permitted": "Mock transport integration closure only.",
                "forbidden": [
                    "model accuracy",
                    "real provider behavior",
                    "held-out performance",
                    "Contract PASS",
                    "approval to execute review-pending draft shards",
                ],
            },
        }
    audit["audit_sha256"] = _digest(audit)
    return audit


def write_mock_smoke_audit(audit: Mapping[str, Any], path: Path | str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(audit, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _rooted(repository: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else repository / path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-root", default=str(ROOT))
    parser.add_argument("--catalog", default=DEFAULT_CATALOG)
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    repository = Path(args.repository_root).resolve()
    audit = run_mock_smoke(
        repository,
        catalog_path=_rooted(repository, args.catalog),
        config_path=_rooted(repository, args.config),
    )
    write_mock_smoke_audit(audit, _rooted(repository, args.output))
    print(
        json.dumps(
            {
                "status": "MOCK_LIVE_SMOKE_PASS",
                "audit_sha256": audit["audit_sha256"],
                "target_signal": audit["offline_replay"]["target_signal"],
                "whole_contract_verdict": audit["offline_replay"][
                    "whole_contract_verdict"
                ],
                "mock_transport_calls": audit["recorder"]["mock_transport_calls"],
                "real_http_requests": 0,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "DeterministicMockTransport",
    "MOCK_SMOKE_AUDIT_SCHEMA_VERSION",
    "MOCK_SMOKE_CONFIG_SCHEMA_VERSION",
    "run_mock_smoke",
    "write_mock_smoke_audit",
]
