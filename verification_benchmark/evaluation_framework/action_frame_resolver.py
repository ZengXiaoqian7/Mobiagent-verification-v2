"""Conservative action-bound frame resolution layered on a frozen v1 catalog."""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Tuple

from .precomputed_evidence_cache import (
    EvidenceCacheKey,
    PrecomputedEvidenceStorage,
    RecordedProviderKind,
)
from .recording_coverage_planner import (
    AUTHORIZED_MODEL,
    REVIEW_PENDING,
    UNRESOLVED_FRAME_SELECTION,
    _canonical_string,
    _digest,
    _relative_ref,
    _safe_id,
    _safe_path,
    _sha256_string,
    _strict_json,
    load_planner_config,
    recording_composite_identity,
)


ACTION_FRAME_RESOLVER_VERSION = "harmony-eval-action-frame-resolver-v1"
ACTION_POLICY_SCHEMA_VERSION = "harmony-eval-action-frame-resolution-policy-v1"
RESOLVED_CATALOG_SCHEMA_VERSION = "harmony-eval-development-recording-catalog-v2"
RESOLVED_SHARD_SCHEMA_VERSION = "harmony-eval-development-recording-draft-shard-v2"
RESOLVED_AUDIT_SCHEMA_VERSION = "harmony-eval-action-frame-resolution-audit-v1"
RESOLVED_ACTION_BOUNDARY = "RESOLVED_ACTION_BOUNDARY_V1"
FRAME_RELATION = "POST_ACTION_SAME_INDEX"
CARDINALITY = "EXACTLY_ONE"


def _exact_keys(value: Mapping[str, Any], expected: set[str], context: str) -> None:
    if set(value) != expected:
        raise ValueError(
            f"{context} fields must be exactly {sorted(expected)}; got {sorted(value)}"
        )


@dataclass(frozen=True)
class ActionResolutionRule:
    rule_id: str
    contract_ref: str
    contract_sha256: str
    node_id: str
    checker_id: str
    provider_kind: RecordedProviderKind
    action_type: str
    require_nonempty_text: bool
    frame_relation: str
    cardinality: str

    def payload(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "contract_ref": self.contract_ref,
            "contract_sha256": self.contract_sha256,
            "node_id": self.node_id,
            "checker_id": self.checker_id,
            "provider_kind": self.provider_kind.value,
            "action_type": self.action_type,
            "require_nonempty_text": self.require_nonempty_text,
            "frame_relation": self.frame_relation,
            "cardinality": self.cardinality,
        }


@dataclass(frozen=True)
class ActionResolutionPolicy:
    base_catalog_sha256: str
    rules: Tuple[ActionResolutionRule, ...]
    schema_version: str = ACTION_POLICY_SCHEMA_VERSION

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "base_catalog_sha256": self.base_catalog_sha256,
            "rules": [rule.payload() for rule in self.rules],
        }

    @property
    def policy_sha256(self) -> str:
        return _digest(self.payload())


@dataclass(frozen=True)
class ResolvedCatalogResult:
    catalog: Mapping[str, Any]
    shards: Tuple[Mapping[str, Any], ...]
    audit: Mapping[str, Any]


def load_action_resolution_policy(path: Path | str) -> ActionResolutionPolicy:
    raw = _strict_json(Path(path))
    if not isinstance(raw, Mapping):
        raise ValueError("action resolution policy must be an object")
    _exact_keys(raw, {"schema_version", "base_catalog_sha256", "rules"}, "policy")
    if raw["schema_version"] != ACTION_POLICY_SCHEMA_VERSION:
        raise ValueError("unsupported action resolution policy schema")
    base_hash = _sha256_string(raw["base_catalog_sha256"], "base_catalog_sha256")
    raw_rules = raw["rules"]
    if not isinstance(raw_rules, list) or not raw_rules:
        raise ValueError("action resolution policy requires at least one rule")
    rules = []
    for index, item in enumerate(raw_rules):
        if not isinstance(item, Mapping):
            raise ValueError(f"action resolution rule {index} must be an object")
        _exact_keys(
            item,
            {
                "rule_id",
                "contract_ref",
                "contract_sha256",
                "node_id",
                "checker_id",
                "provider_kind",
                "action_type",
                "require_nonempty_text",
                "frame_relation",
                "cardinality",
            },
            f"action resolution rule {index}",
        )
        try:
            kind = RecordedProviderKind(item["provider_kind"])
        except (TypeError, ValueError) as exc:
            raise ValueError("action resolution provider_kind is invalid") from exc
        if not isinstance(item["require_nonempty_text"], bool):
            raise ValueError("require_nonempty_text must be boolean")
        if item["frame_relation"] != FRAME_RELATION:
            raise ValueError(f"frame_relation must be exactly {FRAME_RELATION}")
        if item["cardinality"] != CARDINALITY:
            raise ValueError(f"cardinality must be exactly {CARDINALITY}")
        rule = ActionResolutionRule(
            rule_id=_canonical_string(item["rule_id"], "rule_id"),
            contract_ref=_relative_ref(item["contract_ref"], "contract_ref"),
            contract_sha256=_sha256_string(
                item["contract_sha256"], "contract_sha256"
            ),
            node_id=_canonical_string(item["node_id"], "node_id"),
            checker_id=_canonical_string(item["checker_id"], "checker_id"),
            provider_kind=kind,
            action_type=_canonical_string(item["action_type"], "action_type"),
            require_nonempty_text=item["require_nonempty_text"],
            frame_relation=item["frame_relation"],
            cardinality=item["cardinality"],
        )
        if (rule.checker_id == "llm") != (kind is RecordedProviderKind.LLM):
            raise ValueError("action rule checker_id and provider_kind differ")
        if (rule.checker_id == "ocr") != (kind is RecordedProviderKind.OCR):
            raise ValueError("action rule checker_id and provider_kind differ")
        rules.append(rule)
    keys = [rule.rule_id for rule in rules]
    targets = [
        (rule.contract_ref, rule.node_id, rule.checker_id) for rule in rules
    ]
    if len(keys) != len(set(keys)):
        raise ValueError("action resolution rule_id values must be unique")
    if len(targets) != len(set(targets)):
        raise ValueError("action resolution rule targets must be unique")
    return ActionResolutionPolicy(base_hash, tuple(rules))


def _action_projection(path: Path) -> tuple[Tuple[dict[str, Any], ...], str]:
    raw = _strict_json(path)
    if not isinstance(raw, Mapping):
        raise ValueError("actions source must be an object")
    action_count = raw.get("action_count")
    actions = raw.get("actions")
    if (
        not isinstance(action_count, int)
        or isinstance(action_count, bool)
        or action_count < 0
        or not isinstance(actions, list)
        or len(actions) != action_count
    ):
        raise ValueError("actions source count is invalid")
    projection = []
    for expected_index, action in enumerate(actions, 1):
        if not isinstance(action, Mapping):
            raise ValueError("action must be an object")
        action_index = action.get("action_index")
        action_type = action.get("type")
        if action_index != expected_index:
            raise ValueError("action indices must be contiguous and one-based")
        projection.append(
            {
                "action_index": action_index,
                "action_type": _canonical_string(action_type, "action type"),
                "has_nonempty_text": (
                    isinstance(action.get("text"), str)
                    and bool(action.get("text").strip())
                ),
            }
        )
    payload = {
        "projection_schema": "reasoning-free-action-boundary-v1",
        "action_count": action_count,
        "actions": projection,
    }
    return tuple(projection), _digest(payload)


def _rule_for_candidate(
    rules: Tuple[ActionResolutionRule, ...], candidate: Mapping[str, Any]
) -> Optional[ActionResolutionRule]:
    matches = [
        rule
        for rule in rules
        if rule.contract_ref == candidate["contract_ref"]
        and rule.contract_sha256 == candidate["contract_sha256"]
        and rule.node_id == candidate["node_id"]
        and rule.checker_id == candidate["checker_id"]
        and rule.provider_kind.value == candidate["provider_kind"]
    ]
    if len(matches) > 1:
        raise ValueError("multiple action rules match the same candidate")
    return matches[0] if matches else None


def _resolve_candidate(
    root: Path,
    traces_root: str,
    candidate: Mapping[str, Any],
    rule: ActionResolutionRule,
) -> tuple[Optional[dict[str, Any]], dict[str, Any]]:
    action_ref = f"{traces_root}/{candidate['trace_id']}/actions.json"
    action_path = _safe_path(root, action_ref)
    projection, projection_hash = _action_projection(action_path)
    matches = [
        action
        for action in projection
        if action["action_type"] == rule.action_type
        and (not rule.require_nonempty_text or action["has_nonempty_text"])
    ]
    record = {
        "trace_id": candidate["trace_id"],
        "contract_ref": candidate["contract_ref"],
        "contract_sha256": candidate["contract_sha256"],
        "node_id": candidate["node_id"],
        "checker_id": candidate["checker_id"],
        "rule_id": rule.rule_id,
        "action_projection_ref": action_ref,
        "action_projection_sha256": projection_hash,
        "matching_action_count": len(matches),
    }
    if len(matches) != 1:
        return None, {
            **record,
            "status": UNRESOLVED_FRAME_SELECTION,
            "reason": "ACTION_CARDINALITY_NOT_EXACTLY_ONE",
        }
    frame_index = matches[0]["action_index"]
    screenshots = [
        item
        for item in candidate["candidate_screenshots"]
        if item["frame_index"] == frame_index
    ]
    if len(screenshots) != 1:
        return None, {
            **record,
            "status": UNRESOLVED_FRAME_SELECTION,
            "reason": "POST_ACTION_SCREENSHOT_NOT_EXACTLY_ONE",
            "action_index": frame_index,
        }
    selected = {
        **screenshots[0],
        "selection_source_ref": action_ref,
        "selection_source_kind": "REASONING_FREE_ACTION_PROJECTION_V1",
        "selection_source_projection_sha256": projection_hash,
        "resolver_version": ACTION_FRAME_RESOLVER_VERSION,
        "resolution_rule_id": rule.rule_id,
        "frame_relation": FRAME_RELATION,
        "resolution_rationale": (
            "Exactly one allowed process action establishes the Contract node boundary; "
            "the immutable screenshot with the same action index is selected."
        ),
    }
    return selected, {
        **record,
        "status": RESOLVED_ACTION_BOUNDARY,
        "reason": "EXACT_PROCESS_ACTION_BOUNDARY",
        "action_index": frame_index,
        "screenshot_ref": selected["screenshot_ref"],
        "screenshot_sha256": selected["screenshot_sha256"],
    }


def _job_id(consumer: Mapping[str, Any], key: Mapping[str, str]) -> str:
    return (
        f"{_safe_id(consumer['contract_ref'])}-{_safe_id(consumer['node_id'])}-"
        f"{consumer['checker_id']}-{key['screenshot_sha256'][:12]}-"
        f"{key['request_sha256'][:12]}"
    )


def _jobs_from_candidates(
    candidates: list[dict[str, Any]],
    app_by_contract: Mapping[str, str],
    storages: Tuple[tuple[str, PrecomputedEvidenceStorage], ...],
) -> Tuple[dict[str, Any], ...]:
    jobs: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for candidate in candidates:
        selected = candidate["selected_screenshot"]
        if selected is None:
            continue
        kind = RecordedProviderKind(candidate["provider_kind"])
        key = EvidenceCacheKey(
            screenshot_sha256=selected["screenshot_sha256"],
            model_version=candidate["model_version"],
            request_sha256=candidate["request_sha256"],
        )
        identity = recording_composite_identity(
            kind, key.screenshot_sha256, key.model_version, key.request_sha256
        )
        hits = [
            reference
            for reference, storage in storages
            if storage.lookup(kind, key) is not None
        ]
        if len(hits) > 1:
            raise ValueError("exact entry appears in multiple configured caches")
        cache_status = "CACHED" if hits else "MISS"
        cache_ref = hits[0] if hits else None
        candidate["composite_key"] = key.payload()
        candidate["cache_status"] = cache_status
        candidate["cache_ref"] = cache_ref
        consumer = {
            "trace_id": candidate["trace_id"],
            "contract_ref": candidate["contract_ref"],
            "contract_sha256": candidate["contract_sha256"],
            "node_id": candidate["node_id"],
            "checker_id": candidate["checker_id"],
        }
        job = jobs.get(identity)
        if job is None:
            job = {
                "job_id": _job_id(consumer, key.payload()),
                "provider_kind": kind.value,
                "app_id": app_by_contract[candidate["contract_ref"]],
                "contract_ref": candidate["contract_ref"],
                "model_version": candidate["model_version"],
                "screenshot_ref": selected["screenshot_ref"],
                "screenshot_sha256": selected["screenshot_sha256"],
                "image_bytes": selected["image_bytes"],
                "request": candidate["request"],
                "request_sha256": candidate["request_sha256"],
                "composite_key": key.payload(),
                "cache_status": cache_status,
                "cache_ref": cache_ref,
                "review_status": REVIEW_PENDING,
                "consumers": [],
            }
            jobs[identity] = job
        job["consumers"].append(consumer)
    return tuple(
        sorted(
            (
                {
                    **job,
                    "consumers": sorted(
                        job["consumers"],
                        key=lambda item: (
                            item["contract_ref"],
                            item["trace_id"],
                            item["node_id"],
                            item["checker_id"],
                        ),
                    ),
                }
                for job in jobs.values()
            ),
            key=lambda item: item["job_id"],
        )
    )


def _build_shards(
    jobs: Tuple[dict[str, Any], ...], max_jobs: int, max_attempts: int
) -> Tuple[Mapping[str, Any], ...]:
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for job in jobs:
        groups.setdefault(
            (job["provider_kind"], job["app_id"], job["contract_ref"]), []
        ).append(job)
    shards = []
    for group in sorted(groups):
        ordered = sorted(groups[group], key=lambda item: item["job_id"])
        for offset in range(0, len(ordered), max_jobs):
            chunk = ordered[offset : offset + max_jobs]
            misses = [item for item in chunk if item["cache_status"] == "MISS"]
            unique_images = {
                (item["screenshot_ref"], item["screenshot_sha256"]): item["image_bytes"]
                for item in chunk
            }
            shard_id = (
                f"{_safe_id(group[0])}-{_safe_id(group[1])}-"
                f"{_safe_id(Path(group[2]).stem)}-{offset // max_jobs + 1:02d}"
            )
            shard = {
                "schema_version": RESOLVED_SHARD_SCHEMA_VERSION,
                "resolver_version": ACTION_FRAME_RESOLVER_VERSION,
                "shard_id": shard_id,
                "development_only": True,
                "provider_kind": group[0],
                "app_id": group[1],
                "contract_ref": group[2],
                "model_version": AUTHORIZED_MODEL,
                "review_status": REVIEW_PENDING,
                "executable": False,
                "promotion_blockers": ["REVIEW_PENDING"],
                "max_attempts": max_attempts,
                "job_count": len(chunk),
                "cached_unique_jobs": len(chunk) - len(misses),
                "uncached_unique_jobs": len(misses),
                "worst_case_requests": len(misses) * max_attempts,
                "input_image_bytes": sum(unique_images.values()),
                "worst_case_request_image_bytes": sum(
                    item["image_bytes"] * max_attempts for item in misses
                ),
                "jobs": chunk,
                "recorder_manifest": None,
                "recorder_manifest_sha256": None,
            }
            shard["draft_shard_sha256"] = _digest(shard)
            shards.append(shard)
    return tuple(shards)


def resolve_action_bound_frames(
    repository_root: Path | str,
    *,
    base_catalog_path: Path | str,
    planner_config_path: Path | str,
    policy_path: Path | str,
) -> ResolvedCatalogResult:
    root = Path(repository_root).resolve()

    def rooted(path: Path | str) -> Path:
        value = Path(path)
        return value if value.is_absolute() else root / value

    base = _strict_json(rooted(base_catalog_path))
    if not isinstance(base, Mapping) or not isinstance(base.get("catalog_sha256"), str):
        raise ValueError("base catalog is invalid")
    policy = load_action_resolution_policy(rooted(policy_path))
    if base["catalog_sha256"] != policy.base_catalog_sha256:
        raise ValueError("action policy is bound to a different base catalog")
    config = load_planner_config(rooted(planner_config_path))
    candidates = copy.deepcopy(base["candidate_records"])
    if not isinstance(candidates, list):
        raise ValueError("base candidate catalog is invalid")
    resolution_records = []
    for candidate in candidates:
        if candidate["selection_status"] != UNRESOLVED_FRAME_SELECTION:
            continue
        rule = _rule_for_candidate(policy.rules, candidate)
        if rule is None:
            continue
        selected, resolution = _resolve_candidate(
            root, config.traces_root, candidate, rule
        )
        resolution_records.append(resolution)
        if selected is not None:
            candidate["selection_status"] = RESOLVED_ACTION_BOUNDARY
            candidate["selected_screenshot"] = selected

    storages = tuple(
        (
            reference,
            PrecomputedEvidenceStorage.from_jsonl(_safe_path(root, reference)),
        )
        for reference in config.cache_refs
    )
    app_by_contract = {
        item["contract_ref"]: item["app_id"]
        for item in base["external_checker_inventory"]
    }
    jobs = _jobs_from_candidates(candidates, app_by_contract, storages)
    shards = _build_shards(jobs, config.max_jobs_per_shard, config.max_attempts)
    resolved = sum(item["selected_screenshot"] is not None for item in candidates)
    unresolved = len(candidates) - resolved
    cached = sum(item["cache_status"] == "CACHED" for item in jobs)
    misses = sum(item["cache_status"] == "MISS" for item in jobs)
    selected_occurrences = resolved
    catalog = {
        **{key: copy.deepcopy(value) for key, value in base.items() if key not in {
            "schema_version", "planner_version", "summary", "candidate_records",
            "unique_jobs", "catalog_sha256", "claim_boundary"
        }},
        "schema_version": RESOLVED_CATALOG_SCHEMA_VERSION,
        "planner_version": ACTION_FRAME_RESOLVER_VERSION,
        "base_catalog_sha256": base["catalog_sha256"],
        "action_resolution_policy_sha256": policy.policy_sha256,
        "frame_resolution": {
            "resolver_version": ACTION_FRAME_RESOLVER_VERSION,
            "policy": policy.payload(),
            "rule_application_count": len(resolution_records),
            "auto_resolved_count": sum(
                item["status"] == RESOLVED_ACTION_BOUNDARY
                for item in resolution_records
            ),
            "still_unresolved_rule_application_count": sum(
                item["status"] == UNRESOLVED_FRAME_SELECTION
                for item in resolution_records
            ),
            "records": sorted(
                resolution_records,
                key=lambda item: (
                    item["contract_ref"], item["trace_id"], item["node_id"]
                ),
            ),
        },
        "summary": {
            "candidate_record_count": len(candidates),
            "resolved_selection_count": resolved,
            "unresolved_selection_count": unresolved,
            "selected_occurrence_count": selected_occurrences,
            "unique_job_count": len(jobs),
            "duplicate_elimination_count": selected_occurrences - len(jobs),
            "cached_unique_job_count": cached,
            "uncached_unique_job_count": misses,
            "draft_shard_count": len(shards),
            "worst_case_requests": misses * config.max_attempts,
            "real_http_requests": 0,
        },
        "candidate_records": sorted(
            candidates,
            key=lambda item: (
                item["contract_ref"], item["trace_id"], item["node_id"], item["checker_id"]
            ),
        ),
        "unique_jobs": list(jobs),
        "claim_boundary": {
            "permitted": (
                "Action-bound development frame resolution and Mock-only recorder planning."
            ),
            "forbidden": [
                "semantic screenshot understanding from process actions",
                "model accuracy",
                "held-out performance",
                "live execution before explicit review approval",
                "Contract PASS or global checker coverage",
            ],
        },
    }
    catalog["catalog_sha256"] = _digest(catalog)
    audit = {
        "schema_version": RESOLVED_AUDIT_SCHEMA_VERSION,
        "resolver_version": ACTION_FRAME_RESOLVER_VERSION,
        "base_catalog_sha256": base["catalog_sha256"],
        "resolved_catalog_sha256": catalog["catalog_sha256"],
        "policy_sha256": policy.policy_sha256,
        "auto_resolved_count": catalog["frame_resolution"]["auto_resolved_count"],
        "unresolved_count": unresolved,
        "cached_unique_jobs": cached,
        "uncached_unique_jobs": misses,
        "worst_case_requests": misses * config.max_attempts,
        "draft_shards": [
            {
                "shard_id": shard["shard_id"],
                "draft_shard_sha256": shard["draft_shard_sha256"],
                "review_status": shard["review_status"],
                "executable": shard["executable"],
                "worst_case_requests": shard["worst_case_requests"],
            }
            for shard in shards
        ],
        "network_requests": 0,
        "external_cost": 0,
    }
    audit["audit_sha256"] = _digest(audit)
    return ResolvedCatalogResult(catalog, shards, audit)


def write_resolved_catalog_outputs(
    result: ResolvedCatalogResult, output_dir: Path | str
) -> None:
    target = Path(output_dir)
    shards_dir = target / "shards"
    shards_dir.mkdir(parents=True, exist_ok=True)

    def write(path: Path, value: Mapping[str, Any]) -> None:
        path.write_text(
            json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )

    write(target / "catalog.json", result.catalog)
    write(target / "audit.json", result.audit)
    expected = set()
    for shard in result.shards:
        path = shards_dir / f"{shard['shard_id']}.draft.json"
        expected.add(path.name)
        write(path, shard)
    stale = sorted(
        path.name for path in shards_dir.glob("*.draft.json") if path.name not in expected
    )
    if stale:
        raise ValueError(f"stale resolved draft shards require explicit audit: {stale}")


__all__ = [
    "ACTION_FRAME_RESOLVER_VERSION",
    "ACTION_POLICY_SCHEMA_VERSION",
    "RESOLVED_ACTION_BOUNDARY",
    "RESOLVED_CATALOG_SCHEMA_VERSION",
    "ActionResolutionPolicy",
    "ActionResolutionRule",
    "ResolvedCatalogResult",
    "load_action_resolution_policy",
    "resolve_action_bound_frames",
    "write_resolved_catalog_outputs",
]
