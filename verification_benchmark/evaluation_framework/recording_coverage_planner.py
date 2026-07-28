"""Deterministic, zero-network planning for development evidence recording.

This module inventories hash-frozen legacy ContractIR values, joins them only to
explicitly registered development traces, and produces review-pending draft shards.
It never promotes a draft to recorder input without an explicit approval mapping.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Optional, Tuple

from PIL import Image

from .legacy_yaml_adapter import (
    AdaptedLegacyContract,
    LegacyYamlAdapterError,
    adapt_legacy_yaml,
)
from .models import ContractCheckerIR, ContractIR
from .precomputed_evidence_cache import (
    EvidenceCacheKey,
    PrecomputedEvidenceStorage,
    RecordedLlmRequestIR,
    RecordedOcrRequestIR,
    RecordedProviderKind,
)


PLANNER_VERSION = "harmony-eval-development-recording-coverage-planner-v1"
PLANNER_CONFIG_SCHEMA_VERSION = "harmony-eval-development-recording-planner-config-v1"
CATALOG_SCHEMA_VERSION = "harmony-eval-development-recording-catalog-v1"
DRAFT_SHARD_SCHEMA_VERSION = "harmony-eval-development-recording-draft-shard-v1"
AUDIT_SCHEMA_VERSION = "harmony-eval-development-recording-planner-audit-v1"
RECORDING_MANIFEST_SCHEMA_VERSION = "harmony-eval-recording-manifest-v1"
AUTHORIZED_MODEL = "gpt-5.4-mini"
LLM_PROMPT_TEMPLATE_VERSION = "legacy-llm-prompt-v1"
REVIEW_PENDING = "PENDING"
REVIEW_APPROVED = "APPROVED"
UNRESOLVED_FRAME_SELECTION = "UNRESOLVED_FRAME_SELECTION"
RESOLVED_EXISTING_AUDIT = "RESOLVED_EXISTING_AUDIT"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


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


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _strict_json(path: Path) -> Any:
    try:
        return json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"invalid strict UTF-8 JSON: {path.name}") from exc


def _strict_jsonl(path: Path) -> Tuple[Mapping[str, Any], ...]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"unreadable JSONL: {path.name}") from exc
    rows = []
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            raise ValueError(f"blank JSONL line in {path.name}:{line_number}")
        try:
            value = json.loads(line, object_pairs_hook=_reject_duplicate_json_keys)
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValueError(f"invalid JSONL row in {path.name}:{line_number}") from exc
        if not isinstance(value, Mapping):
            raise ValueError(f"JSONL row must be an object in {path.name}:{line_number}")
        rows.append(value)
    return tuple(rows)


def _exact_keys(value: Mapping[str, Any], expected: set[str], context: str) -> None:
    if set(value) != expected:
        raise ValueError(
            f"{context} fields must be exactly {sorted(expected)}; got {sorted(value)}"
        )


def _canonical_string(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{context} must be a canonical non-empty string")
    return value


def _relative_ref(value: Any, context: str) -> str:
    reference = _canonical_string(value, context)
    if "\\" in reference:
        raise ValueError(f"{context} must use POSIX separators")
    parsed = PurePosixPath(reference)
    if parsed.is_absolute() or ".." in parsed.parts:
        raise ValueError(f"{context} must stay below the repository root")
    return parsed.as_posix()


def _safe_path(root: Path, reference: str) -> Path:
    relative = _relative_ref(reference, "repository reference")
    path = (root / PurePosixPath(relative)).resolve()
    repository = root.resolve()
    if repository != path and repository not in path.parents:
        raise ValueError("repository reference escapes the repository root")
    return path


def _sha256_string(value: Any, context: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError(f"{context} must be a lowercase SHA-256")
    return value


@dataclass(frozen=True)
class TraceContractBinding:
    trace_id: str
    contract_ref: str
    mapping_basis: str

    def payload(self) -> dict[str, str]:
        return {
            "trace_id": self.trace_id,
            "contract_ref": self.contract_ref,
            "mapping_basis": self.mapping_basis,
        }


@dataclass(frozen=True)
class PlannerConfig:
    contract_roots: Tuple[str, ...]
    traces_root: str
    provenance_overlay_ref: str
    cache_refs: Tuple[str, ...]
    selector_audit_refs: Tuple[str, ...]
    trace_contract_bindings: Tuple[TraceContractBinding, ...]
    max_jobs_per_shard: int
    max_attempts: int
    model_version: str = AUTHORIZED_MODEL
    schema_version: str = PLANNER_CONFIG_SCHEMA_VERSION

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "model_version": self.model_version,
            "contract_roots": list(self.contract_roots),
            "traces_root": self.traces_root,
            "provenance_overlay_ref": self.provenance_overlay_ref,
            "cache_refs": list(self.cache_refs),
            "selector_audit_refs": list(self.selector_audit_refs),
            "trace_contract_bindings": [
                item.payload() for item in self.trace_contract_bindings
            ],
            "max_jobs_per_shard": self.max_jobs_per_shard,
            "max_attempts": self.max_attempts,
        }


def load_planner_config(path: Path | str) -> PlannerConfig:
    source = Path(path)
    raw = _strict_json(source)
    if not isinstance(raw, Mapping):
        raise ValueError("planner config must be a JSON object")
    _exact_keys(
        raw,
        {
            "schema_version",
            "model_version",
            "contract_roots",
            "traces_root",
            "provenance_overlay_ref",
            "cache_refs",
            "selector_audit_refs",
            "trace_contract_bindings",
            "max_jobs_per_shard",
            "max_attempts",
        },
        "planner config",
    )
    if raw["schema_version"] != PLANNER_CONFIG_SCHEMA_VERSION:
        raise ValueError("unsupported planner config schema")
    if raw["model_version"] != AUTHORIZED_MODEL:
        raise ValueError(f"planner model must be exactly {AUTHORIZED_MODEL}")

    def refs(name: str) -> Tuple[str, ...]:
        values = raw[name]
        if not isinstance(values, list):
            raise ValueError(f"{name} must be a JSON array")
        result = tuple(_relative_ref(item, name) for item in values)
        if len(result) != len(set(result)):
            raise ValueError(f"{name} must not contain duplicates")
        return result

    bindings = []
    raw_bindings = raw["trace_contract_bindings"]
    if not isinstance(raw_bindings, list):
        raise ValueError("trace_contract_bindings must be a JSON array")
    for index, item in enumerate(raw_bindings):
        if not isinstance(item, Mapping):
            raise ValueError(f"trace binding {index} must be an object")
        _exact_keys(
            item,
            {"trace_id", "contract_ref", "mapping_basis"},
            f"trace binding {index}",
        )
        bindings.append(
            TraceContractBinding(
                trace_id=_relative_ref(item["trace_id"], "trace_id"),
                contract_ref=_relative_ref(item["contract_ref"], "contract_ref"),
                mapping_basis=_canonical_string(item["mapping_basis"], "mapping_basis"),
            )
        )
    trace_ids = [item.trace_id for item in bindings]
    if len(trace_ids) != len(set(trace_ids)):
        raise ValueError("trace_contract_bindings must have unique trace_id values")
    for name in ("max_jobs_per_shard", "max_attempts"):
        value = raw[name]
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ValueError(f"{name} must be a positive integer")
    if raw["max_jobs_per_shard"] > 10:
        raise ValueError("development shard cap must not exceed 10 jobs")
    if raw["max_attempts"] > 3:
        raise ValueError("max_attempts must not exceed recorder hard limit 3")
    config = PlannerConfig(
        contract_roots=refs("contract_roots"),
        traces_root=_relative_ref(raw["traces_root"], "traces_root"),
        provenance_overlay_ref=_relative_ref(
            raw["provenance_overlay_ref"], "provenance_overlay_ref"
        ),
        cache_refs=refs("cache_refs"),
        selector_audit_refs=refs("selector_audit_refs"),
        trace_contract_bindings=tuple(bindings),
        max_jobs_per_shard=raw["max_jobs_per_shard"],
        max_attempts=raw["max_attempts"],
    )
    return config


@dataclass(frozen=True)
class TraceProvenance:
    trace_id: str
    origin: str
    current_role: str
    original_role: str
    parent_trace_id: Optional[str]
    transformation: Any


@dataclass(frozen=True)
class ScreenshotCandidate:
    screenshot_ref: str
    screenshot_sha256: str
    image_bytes: int
    frame_index: int

    def payload(self) -> dict[str, Any]:
        return {
            "frame_index": self.frame_index,
            "screenshot_ref": self.screenshot_ref,
            "screenshot_sha256": self.screenshot_sha256,
            "image_bytes": self.image_bytes,
        }


@dataclass(frozen=True)
class SelectorRecord:
    trace_id: str
    contract_ref: str
    contract_sha256: str
    node_id: str
    checker_id: str
    provider_kind: RecordedProviderKind
    screenshot_ref: str
    screenshot_sha256: str
    source_ref: str
    source_sha256: str


@dataclass(frozen=True)
class ContractRecord:
    source_ref: str
    adapted: AdaptedLegacyContract

    @property
    def contract(self) -> ContractIR:
        return self.adapted.contract


@dataclass(frozen=True)
class PlannerResult:
    catalog: Mapping[str, Any]
    shards: Tuple[Mapping[str, Any], ...]
    audit: Mapping[str, Any]


def _load_provenance(path: Path) -> Tuple[TraceProvenance, ...]:
    records = []
    for index, raw in enumerate(_strict_jsonl(path)):
        required = {
            "trace_id",
            "origin",
            "current_enhanced_role",
            "original_protocol_role",
            "parent_trace_id",
            "transformation",
        }
        if not required.issubset(raw):
            raise ValueError(f"provenance row {index} lacks required routing fields")
        parent = raw["parent_trace_id"]
        if parent is not None:
            parent = _relative_ref(parent, "parent_trace_id")
        records.append(
            TraceProvenance(
                trace_id=_relative_ref(raw["trace_id"], "trace_id"),
                origin=_canonical_string(raw["origin"], "origin"),
                current_role=_canonical_string(
                    raw["current_enhanced_role"], "current_enhanced_role"
                ),
                original_role=_canonical_string(
                    raw["original_protocol_role"], "original_protocol_role"
                ),
                parent_trace_id=parent,
                transformation=raw["transformation"],
            )
        )
    ids = [item.trace_id for item in records]
    if len(ids) != len(set(ids)):
        raise ValueError("provenance overlay contains duplicate trace_id values")
    return tuple(records)


def _heldout_source_refs(root: Path) -> Tuple[str, ...]:
    base = root / "verification_benchmark"
    refs = []
    for path in base.rglob("*"):
        if not path.is_file() or path.suffix.casefold() not in {".json", ".jsonl"}:
            continue
        relative = path.relative_to(root).as_posix()
        folded = relative.casefold()
        name = path.name.casefold()
        if "heldout" not in folded:
            continue
        if name.startswith("labels") or "manifest" in name or "/frozen/" in f"/{folded}":
            refs.append(relative)
    return tuple(sorted(set(refs)))


def _lineage_values(value: Any) -> Iterable[str]:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key in {"trace_id", "parent_trace_id"} and isinstance(item, str):
                yield item
            elif isinstance(item, (Mapping, list)):
                yield from _lineage_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from _lineage_values(item)


def _heldout_lineage(root: Path) -> tuple[set[str], Tuple[dict[str, Any], ...]]:
    identities: set[str] = set()
    sources = []
    for reference in _heldout_source_refs(root):
        path = _safe_path(root, reference)
        values: Iterable[Any]
        if path.suffix.casefold() == ".jsonl":
            values = _strict_jsonl(path)
        else:
            values = (_strict_json(path),)
        before = len(identities)
        for value in values:
            for identity in _lineage_values(value):
                try:
                    identities.add(_relative_ref(identity, "held-out lineage identity"))
                except ValueError:
                    continue
        sources.append(
            {
                "source_ref": reference,
                "source_sha256": _file_sha256(path),
                "lineage_identity_count": len(identities) - before,
            }
        )
    return identities, tuple(sources)


def _expand_heldout_ancestors(
    heldout: set[str], provenance: Tuple[TraceProvenance, ...]
) -> set[str]:
    parents = {item.trace_id: item.parent_trace_id for item in provenance}
    expanded = set(heldout)
    changed = True
    while changed:
        changed = False
        for identity in tuple(expanded):
            parent = parents.get(identity)
            if parent is not None and parent not in expanded:
                expanded.add(parent)
                changed = True
    return expanded


def _discover_contracts(
    root: Path, contract_roots: Tuple[str, ...]
) -> tuple[Tuple[ContractRecord, ...], Tuple[dict[str, str], ...]]:
    paths: dict[str, Path] = {}
    for reference in contract_roots:
        contract_root = _safe_path(root, reference)
        if not contract_root.is_dir():
            raise ValueError(f"contract root is not a directory: {reference}")
        for path in contract_root.rglob("*.yaml"):
            source_ref = path.relative_to(root).as_posix()
            paths[source_ref] = path
    contracts = []
    failures = []
    for source_ref in sorted(paths):
        try:
            adapted = adapt_legacy_yaml(paths[source_ref], source_ref=source_ref)
        except LegacyYamlAdapterError as exc:
            failures.append({"source_ref": source_ref, "failure_code": exc.code.value})
        else:
            contracts.append(ContractRecord(source_ref, adapted))
    return tuple(contracts), tuple(failures)


def _frame_index(path: Path) -> Optional[int]:
    if not path.stem.isdigit():
        return None
    value = int(path.stem)
    return value if value >= 0 else None


def _trace_screenshots(
    root: Path, traces_root: str, trace_id: str
) -> Tuple[ScreenshotCandidate, ...]:
    directory = _safe_path(root, f"{traces_root}/{trace_id}")
    if not directory.is_dir():
        return ()
    values = []
    for path in directory.iterdir():
        if not path.is_file() or path.suffix.casefold() not in {".jpg", ".jpeg"}:
            continue
        frame = _frame_index(path)
        if frame is None:
            continue
        try:
            with Image.open(path) as image:
                if image.format != "JPEG":
                    raise ValueError("numeric screenshot is not a JPEG")
                image.verify()
        except (OSError, ValueError) as exc:
            raise ValueError(f"invalid JPEG screenshot: {path.name}") from exc
        values.append(
            ScreenshotCandidate(
                screenshot_ref=path.relative_to(_safe_path(root, traces_root)).as_posix(),
                screenshot_sha256=_file_sha256(path),
                image_bytes=path.stat().st_size,
                frame_index=frame,
            )
        )
    return tuple(sorted(values, key=lambda item: (item.frame_index, item.screenshot_ref)))


def _discover_trace_directories(root: Path, traces_root: str) -> Tuple[str, ...]:
    base = _safe_path(root, traces_root)
    trace_ids = set()
    for path in base.rglob("*"):
        if path.is_file() and path.suffix.casefold() in {".jpg", ".jpeg"}:
            if _frame_index(path) is not None:
                trace_ids.add(path.parent.relative_to(base).as_posix())
    return tuple(sorted(trace_ids))


def _load_selector_records(root: Path, refs: Tuple[str, ...]) -> Tuple[SelectorRecord, ...]:
    records = []
    for reference in refs:
        path = _safe_path(root, reference)
        raw = _strict_json(path)
        if not isinstance(raw, Mapping):
            raise ValueError("selector audit must be an object")
        try:
            trace = raw["trace"]
            contract = raw["contract"]
            plan = raw["provider_plan"]
            trace_id = _relative_ref(trace["trace_id"], "selector trace_id")
            contract_ref = _relative_ref(contract["source_ref"], "selector contract_ref")
            contract_hash = _sha256_string(
                contract["contract_sha256"], "selector contract_sha256"
            )
            bindings = plan["bindings"]
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"selector audit lacks required evidence fields: {reference}") from exc
        if not isinstance(bindings, list):
            raise ValueError("selector provider bindings must be an array")
        for item in bindings:
            if not isinstance(item, Mapping):
                raise ValueError("selector binding must be an object")
            try:
                kind = RecordedProviderKind(item["provider_kind"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("selector provider kind is invalid") from exc
            records.append(
                SelectorRecord(
                    trace_id=trace_id,
                    contract_ref=contract_ref,
                    contract_sha256=contract_hash,
                    node_id=_canonical_string(item["node_id"], "selector node_id"),
                    checker_id=_canonical_string(
                        item["checker_id"], "selector checker_id"
                    ),
                    provider_kind=kind,
                    screenshot_ref=_relative_ref(
                        item["screenshot_ref"], "selector screenshot_ref"
                    ),
                    screenshot_sha256=_sha256_string(
                        item["screenshot_sha256"], "selector screenshot_sha256"
                    ),
                    source_ref=reference,
                    source_sha256=_file_sha256(path),
                )
            )
    keys = [(x.trace_id, x.contract_ref, x.node_id, x.checker_id) for x in records]
    if len(keys) != len(set(keys)):
        raise ValueError("selector audits contain duplicate contract checker selections")
    return tuple(records)


def _external_occurrences(contract: ContractIR) -> Tuple[tuple[str, ContractCheckerIR], ...]:
    if contract.dag is None:
        return ()
    return tuple(
        (node.node_id, checker)
        for node in contract.dag.nodes
        for checker in node.checkers
        if checker.checker_id in {"ocr", "llm"}
    )


def _request(checker: ContractCheckerIR) -> tuple[RecordedProviderKind, Any, dict[str, Any]]:
    if checker.checker_id == "ocr":
        request = RecordedOcrRequestIR()
        return (
            RecordedProviderKind.OCR,
            request,
            {
                "kind": "OCR_ROI",
                "coordinate_space": request.roi.coordinate_space.value,
                "bounds": list(request.roi.bounds),
                "reference_size": None,
                "selection_source": "TYPED_FULL_SCREEN_DEFAULT_NO_CONTRACT_CROP",
            },
        )
    if checker.checker_id == "llm":
        prompt = checker.parameters.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("ContractIR LLM checker lacks an exact prompt")
        request = RecordedLlmRequestIR(prompt, LLM_PROMPT_TEMPLATE_VERSION)
        return (
            RecordedProviderKind.LLM,
            request,
            {
                "kind": "LLM_PROMPT",
                "prompt_template_version": request.prompt_template_version,
                "prompt": request.prompt,
                "selection_source": "HASH_FROZEN_CONTRACT_IR",
            },
        )
    raise ValueError("planner request supports only OCR and LLM")


def _origin_caveat(origin: str, transformation: Any) -> Optional[str]:
    if origin in {"unknown", "semireal"} or transformation is not None:
        return (
            "Compatibility-only development evidence; origin/transformation does not "
            "establish unmodified real-device quality."
        )
    return None


def _selector_map(records: Tuple[SelectorRecord, ...]) -> dict[tuple[str, str, str, str], SelectorRecord]:
    return {
        (item.trace_id, item.contract_ref, item.node_id, item.checker_id): item
        for item in records
    }


def _cache_lookup(
    storages: Tuple[tuple[str, PrecomputedEvidenceStorage], ...],
    kind: RecordedProviderKind,
    key: EvidenceCacheKey,
) -> tuple[str, Optional[str]]:
    hits = [reference for reference, storage in storages if storage.lookup(kind, key)]
    if len(hits) > 1:
        raise ValueError("the same exact cache entry appears in multiple configured storages")
    return ("CACHED", hits[0]) if hits else ("MISS", None)


def recording_composite_identity(
    provider_kind: RecordedProviderKind,
    screenshot_sha256: str,
    model_version: str,
    request_sha256: str,
) -> tuple[str, str, str, str]:
    """Return the exact deduplication identity; none of its fields are fuzzy."""

    if not isinstance(provider_kind, RecordedProviderKind):
        raise ValueError("composite identity provider_kind is invalid")
    _sha256_string(screenshot_sha256, "composite identity screenshot_sha256")
    _canonical_string(model_version, "composite identity model_version")
    _sha256_string(request_sha256, "composite identity request_sha256")
    return provider_kind.value, screenshot_sha256, model_version, request_sha256


def _safe_id(value: str) -> str:
    folded = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return folded or "item"


def _build_shards(
    unique_jobs: Tuple[dict[str, Any], ...], config: PlannerConfig
) -> Tuple[Mapping[str, Any], ...]:
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for job in unique_jobs:
        group = (job["provider_kind"], job["app_id"], job["contract_ref"])
        groups.setdefault(group, []).append(job)
    shards = []
    for group in sorted(groups):
        jobs = sorted(groups[group], key=lambda item: item["job_id"])
        for offset in range(0, len(jobs), config.max_jobs_per_shard):
            chunk = jobs[offset : offset + config.max_jobs_per_shard]
            uncached = [item for item in chunk if item["cache_status"] == "MISS"]
            unique_images = {
                (item["screenshot_ref"], item["screenshot_sha256"]): item["image_bytes"]
                for item in chunk
            }
            shard_id = (
                f"{_safe_id(group[0])}-{_safe_id(group[1])}-"
                f"{_safe_id(PurePosixPath(group[2]).stem)}-{offset // config.max_jobs_per_shard + 1:02d}"
            )
            payload = {
                "schema_version": DRAFT_SHARD_SCHEMA_VERSION,
                "planner_version": PLANNER_VERSION,
                "shard_id": shard_id,
                "development_only": True,
                "provider_kind": group[0],
                "app_id": group[1],
                "contract_ref": group[2],
                "model_version": config.model_version,
                "review_status": REVIEW_PENDING,
                "executable": False,
                "promotion_blockers": ["REVIEW_PENDING"],
                "max_attempts": config.max_attempts,
                "job_count": len(chunk),
                "cached_unique_jobs": len(chunk) - len(uncached),
                "uncached_unique_jobs": len(uncached),
                "worst_case_requests": len(uncached) * config.max_attempts,
                "input_image_bytes": sum(unique_images.values()),
                "worst_case_request_image_bytes": sum(
                    item["image_bytes"] * config.max_attempts for item in uncached
                ),
                "jobs": chunk,
                "recorder_manifest": None,
                "recorder_manifest_sha256": None,
            }
            payload["draft_shard_sha256"] = _digest(payload)
            shards.append(payload)
    return tuple(shards)


def plan_development_recording_coverage(
    repository_root: Path | str, config_path: Path | str
) -> PlannerResult:
    root = Path(repository_root).resolve()
    config_source = Path(config_path)
    if not config_source.is_absolute():
        config_source = root / config_source
    config = load_planner_config(config_source)
    contracts, contract_failures = _discover_contracts(root, config.contract_roots)
    contract_by_ref = {item.source_ref: item for item in contracts}
    if len(contract_by_ref) != len(contracts):
        raise ValueError("contract source references must be unique")

    provenance = _load_provenance(_safe_path(root, config.provenance_overlay_ref))
    provenance_by_id = {item.trace_id: item for item in provenance}
    heldout, heldout_sources = _heldout_lineage(root)
    heldout = _expand_heldout_ancestors(heldout, provenance)
    selectors = _selector_map(_load_selector_records(root, config.selector_audit_refs))
    storages = tuple(
        (reference, PrecomputedEvidenceStorage.from_jsonl(_safe_path(root, reference)))
        for reference in config.cache_refs
    )

    all_trace_dirs = _discover_trace_directories(root, config.traces_root)
    binding_by_trace = {item.trace_id: item for item in config.trace_contract_bindings}
    unregistered = sorted(set(all_trace_dirs) - set(provenance_by_id))
    unmapped = sorted(
        trace_id
        for trace_id in all_trace_dirs
        if trace_id in provenance_by_id
        and provenance_by_id[trace_id].current_role == "development"
        and provenance_by_id[trace_id].original_role == "development"
        and trace_id not in binding_by_trace
    )

    eligible: list[tuple[TraceContractBinding, TraceProvenance, Tuple[ScreenshotCandidate, ...]]] = []
    exclusions = []
    for trace_id in all_trace_dirs:
        trace = provenance_by_id.get(trace_id)
        if trace is None:
            exclusions.append({"trace_id": trace_id, "reason": "UNREGISTERED_PROVENANCE"})
            continue
        reasons = []
        if trace.current_role != "development" or trace.original_role != "development":
            reasons.append("NOT_DEVELOPMENT_IN_BOTH_PROVENANCE_ROLES")
        lineage = []
        cursor: Optional[str] = trace.trace_id
        seen = set()
        while cursor is not None and cursor not in seen:
            seen.add(cursor)
            lineage.append(cursor)
            cursor = provenance_by_id.get(cursor).parent_trace_id if cursor in provenance_by_id else None
        if any(identity in heldout for identity in lineage):
            reasons.append("HELD_OUT_OR_PARENT_LINEAGE_MATCH")
        binding = binding_by_trace.get(trace_id)
        if binding is None:
            reasons.append("NO_EXPLICIT_CONTRACT_BINDING")
        elif binding.contract_ref not in contract_by_ref:
            raise ValueError(f"trace binding references an invalid contract: {binding.contract_ref}")
        screenshots = _trace_screenshots(root, config.traces_root, trace_id)
        if not screenshots:
            reasons.append("NO_NUMERIC_JPEG_SCREENSHOTS")
        if reasons:
            exclusions.append({"trace_id": trace_id, "reason": "+".join(sorted(reasons))})
        else:
            assert binding is not None
            eligible.append((binding, trace, screenshots))

    candidate_records = []
    selected_occurrences = []
    contract_trace_counts: dict[str, int] = {}
    for binding, trace, screenshots in sorted(eligible, key=lambda item: item[0].trace_id):
        record = contract_by_ref[binding.contract_ref]
        contract_trace_counts[binding.contract_ref] = contract_trace_counts.get(binding.contract_ref, 0) + 1
        for node_id, checker in _external_occurrences(record.contract):
            kind, request, request_payload = _request(checker)
            selection = selectors.get(
                (trace.trace_id, binding.contract_ref, node_id, checker.checker_id)
            )
            selected_payload = None
            cache_status = None
            cache_ref = None
            composite_key = None
            selection_status = UNRESOLVED_FRAME_SELECTION
            if selection is not None:
                if selection.contract_sha256 != record.adapted.contract_sha256:
                    raise ValueError("selector Contract SHA-256 differs from current ContractIR")
                if selection.provider_kind is not kind:
                    raise ValueError("selector provider kind differs from ContractIR checker")
                selected = next(
                    (
                        item
                        for item in screenshots
                        if item.screenshot_ref == selection.screenshot_ref
                        and item.screenshot_sha256 == selection.screenshot_sha256
                    ),
                    None,
                )
                if selected is None:
                    raise ValueError("selector screenshot is absent or has drifted")
                key = EvidenceCacheKey(
                    selected.screenshot_sha256,
                    config.model_version,
                    request.request_sha256,
                )
                cache_status, cache_ref = _cache_lookup(storages, kind, key)
                composite_key = key.payload()
                selected_payload = {
                    **selected.payload(),
                    "selection_source_ref": selection.source_ref,
                    "selection_source_sha256": selection.source_sha256,
                }
                selection_status = RESOLVED_EXISTING_AUDIT
                selected_occurrences.append(
                    {
                        "consumer": {
                            "trace_id": trace.trace_id,
                            "contract_ref": binding.contract_ref,
                            "contract_sha256": record.adapted.contract_sha256,
                            "node_id": node_id,
                            "checker_id": checker.checker_id,
                        },
                        "provider_kind": kind.value,
                        "app_id": record.adapted.provenance.app_id,
                        "model_version": config.model_version,
                        "request": request_payload,
                        "request_sha256": request.request_sha256,
                        "screenshot_ref": selected.screenshot_ref,
                        "screenshot_sha256": selected.screenshot_sha256,
                        "image_bytes": selected.image_bytes,
                        "composite_key": composite_key,
                        "cache_status": cache_status,
                        "cache_ref": cache_ref,
                    }
                )
            candidate_records.append(
                {
                    "trace_id": trace.trace_id,
                    "trace_origin": trace.origin,
                    "parent_trace_id": trace.parent_trace_id,
                    "transformation": trace.transformation,
                    "origin_caveat": _origin_caveat(trace.origin, trace.transformation),
                    "contract_ref": binding.contract_ref,
                    "contract_sha256": record.adapted.contract_sha256,
                    "contract_source_id": record.adapted.provenance.task_id,
                    "contract_mapping_basis": binding.mapping_basis,
                    "node_id": node_id,
                    "checker_id": checker.checker_id,
                    "provider_kind": kind.value,
                    "model_version": config.model_version,
                    "request": request_payload,
                    "request_sha256": request.request_sha256,
                    "candidate_screenshots": [item.payload() for item in screenshots],
                    "selection_status": selection_status,
                    "selected_screenshot": selected_payload,
                    "review_status": REVIEW_PENDING,
                    "executable": False,
                    "composite_key": composite_key,
                    "cache_status": cache_status,
                    "cache_ref": cache_ref,
                }
            )

    deduplicated: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for occurrence in selected_occurrences:
        key = occurrence["composite_key"]
        identity = recording_composite_identity(
            RecordedProviderKind(occurrence["provider_kind"]),
            key["screenshot_sha256"],
            key["model_version"],
            key["request_sha256"],
        )
        current = deduplicated.get(identity)
        if current is None:
            job_id = (
                f"{_safe_id(occurrence['consumer']['contract_ref'])}-"
                f"{_safe_id(occurrence['consumer']['node_id'])}-"
                f"{occurrence['consumer']['checker_id']}-"
                f"{key['screenshot_sha256'][:12]}-{key['request_sha256'][:12]}"
            )
            current = {
                "job_id": job_id,
                "provider_kind": occurrence["provider_kind"],
                "app_id": occurrence["app_id"],
                "contract_ref": occurrence["consumer"]["contract_ref"],
                "model_version": occurrence["model_version"],
                "screenshot_ref": occurrence["screenshot_ref"],
                "screenshot_sha256": occurrence["screenshot_sha256"],
                "image_bytes": occurrence["image_bytes"],
                "request": occurrence["request"],
                "request_sha256": occurrence["request_sha256"],
                "composite_key": occurrence["composite_key"],
                "cache_status": occurrence["cache_status"],
                "cache_ref": occurrence["cache_ref"],
                "review_status": REVIEW_PENDING,
                "consumers": [],
            }
            deduplicated[identity] = current
        current["consumers"].append(occurrence["consumer"])
    unique_jobs = tuple(
        sorted(
            (
                {**job, "consumers": sorted(job["consumers"], key=lambda x: tuple(x.values()))}
                for job in deduplicated.values()
            ),
            key=lambda item: item["job_id"],
        )
    )

    external_inventory = []
    for record in contracts:
        for node_id, checker in _external_occurrences(record.contract):
            kind, request, request_payload = _request(checker)
            external_inventory.append(
                {
                    "contract_ref": record.source_ref,
                    "contract_sha256": record.adapted.contract_sha256,
                    "contract_source_id": record.adapted.provenance.task_id,
                    "app_id": record.adapted.provenance.app_id,
                    "task_type": record.adapted.provenance.task_type,
                    "node_id": node_id,
                    "checker_id": checker.checker_id,
                    "provider_kind": kind.value,
                    "model_version": config.model_version,
                    "request": request_payload,
                    "request_sha256": request.request_sha256,
                    "eligible_development_trace_count": contract_trace_counts.get(
                        record.source_ref, 0
                    ),
                }
            )
    external_inventory.sort(
        key=lambda x: (x["contract_ref"], x["node_id"], x["checker_id"])
    )
    provider_counts = {
        kind: sum(1 for item in external_inventory if item["provider_kind"] == kind)
        for kind in ("OCR", "LLM")
    }
    unresolved_count = sum(
        item["selection_status"] == UNRESOLVED_FRAME_SELECTION
        for item in candidate_records
    )
    cached_count = sum(item["cache_status"] == "CACHED" for item in unique_jobs)
    miss_count = sum(item["cache_status"] == "MISS" for item in unique_jobs)
    shards = _build_shards(unique_jobs, config)

    catalog = {
        "schema_version": CATALOG_SCHEMA_VERSION,
        "planner_version": PLANNER_VERSION,
        "development_only": True,
        "model_version": config.model_version,
        "review_default": REVIEW_PENDING,
        "config_sha256": _digest(config.payload()),
        "input_boundary": {
            "contract_roots": list(config.contract_roots),
            "provenance_overlay_ref": config.provenance_overlay_ref,
            "heldout_source_count": len(heldout_sources),
            "heldout_lineage_identity_count": len(heldout),
            "heldout_sources_sha256": _digest(list(heldout_sources)),
            "cache_storage": [
                {
                    "cache_ref": reference,
                    "entry_count": storage.entry_count,
                    "storage_sha256": storage.storage_sha256,
                }
                for reference, storage in storages
            ],
            "selector_audit_refs": list(config.selector_audit_refs),
        },
        "contract_audit": {
            "yaml_count": len(contracts) + len(contract_failures),
            "valid_contract_count": len(contracts),
            "invalid_contract_count": len(contract_failures),
            "invalid_contracts": list(contract_failures),
            "external_occurrence_count": len(external_inventory),
            "provider_occurrence_counts": provider_counts,
        },
        "trace_audit": {
            "registered_provenance_count": len(provenance),
            "trace_directories_with_numeric_jpeg": len(all_trace_dirs),
            "eligible_development_trace_count": len(eligible),
            "unregistered_trace_directories": unregistered,
            "unmapped_development_trace_directories": unmapped,
            "exclusions": sorted(exclusions, key=lambda item: item["trace_id"]),
        },
        "summary": {
            "candidate_record_count": len(candidate_records),
            "resolved_selection_count": len(selected_occurrences),
            "unresolved_selection_count": unresolved_count,
            "selected_occurrence_count": len(selected_occurrences),
            "unique_job_count": len(unique_jobs),
            "duplicate_elimination_count": len(selected_occurrences) - len(unique_jobs),
            "cached_unique_job_count": cached_count,
            "uncached_unique_job_count": miss_count,
            "draft_shard_count": len(shards),
            "worst_case_requests": miss_count * config.max_attempts,
            "real_http_requests": 0,
        },
        "external_checker_inventory": external_inventory,
        "candidate_records": sorted(
            candidate_records,
            key=lambda item: (
                item["contract_ref"],
                item["trace_id"],
                item["node_id"],
                item["checker_id"],
            ),
        ),
        "unique_jobs": list(unique_jobs),
        "claim_boundary": {
            "permitted": (
                "Deterministic development-only planning, exact cache reuse, and review/budget accounting."
            ),
            "forbidden": [
                "model accuracy",
                "held-out performance",
                "real-device quality for unknown or transformed origins",
                "executable recorder input before explicit review approval",
                "Contract PASS or global checker coverage",
            ],
        },
    }
    catalog["catalog_sha256"] = _digest(catalog)
    audit = {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "planner_version": PLANNER_VERSION,
        "catalog_sha256": catalog["catalog_sha256"],
        "draft_shards": [
            {
                "shard_id": item["shard_id"],
                "draft_shard_sha256": item["draft_shard_sha256"],
                "review_status": item["review_status"],
                "executable": item["executable"],
                "worst_case_requests": item["worst_case_requests"],
            }
            for item in shards
        ],
        "heldout_exclusion_enforced": True,
        "unresolved_never_materialized_as_job": all(
            item["selection_status"] != UNRESOLVED_FRAME_SELECTION
            or item["composite_key"] is None
            for item in candidate_records
        ),
        "review_pending_never_executable": all(not item["executable"] for item in shards),
        "network_requests": 0,
        "external_cost": 0,
    }
    audit["audit_sha256"] = _digest(audit)
    return PlannerResult(catalog=catalog, shards=shards, audit=audit)


def promote_draft_shard(
    shard: Mapping[str, Any], review_status_by_job: Mapping[str, str]
) -> Mapping[str, Any]:
    """Build recorder-compatible JSON only after every exact job is approved."""

    if shard.get("schema_version") != DRAFT_SHARD_SCHEMA_VERSION:
        raise ValueError("unsupported draft shard schema")
    jobs = shard.get("jobs")
    if not isinstance(jobs, list) or not jobs:
        raise ValueError("draft shard jobs are invalid")
    expected_ids = {item.get("job_id") for item in jobs if isinstance(item, Mapping)}
    if None in expected_ids or set(review_status_by_job) != expected_ids:
        raise ValueError("review decisions must cover every exact draft job")
    if any(review_status_by_job[job_id] != REVIEW_APPROVED for job_id in expected_ids):
        raise ValueError("all draft jobs must be explicitly APPROVED")
    manifest_jobs = []
    for item in jobs:
        request = dict(item["request"])
        request.pop("selection_source", None)
        manifest_jobs.append(
            {
                "job_id": item["job_id"],
                "provider_kind": item["provider_kind"],
                "screenshot_ref": item["screenshot_ref"],
                "screenshot_sha256": item["screenshot_sha256"],
                "request": request,
            }
        )
    manifest = {
        "schema_version": RECORDING_MANIFEST_SCHEMA_VERSION,
        "model_version": shard["model_version"],
        "jobs": manifest_jobs,
    }
    return {**manifest, "manifest_sha256": _digest(manifest)}


def write_planner_outputs(result: PlannerResult, output_dir: Path | str) -> None:
    target = Path(output_dir)
    shards_dir = target / "shards"
    shards_dir.mkdir(parents=True, exist_ok=True)

    def write(path: Path, value: Mapping[str, Any]) -> None:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        path.write_text(text, encoding="utf-8", newline="\n")

    write(target / "catalog.json", result.catalog)
    write(target / "audit.json", result.audit)
    expected = set()
    for shard in result.shards:
        path = shards_dir / f"{shard['shard_id']}.draft.json"
        expected.add(path.name)
        write(path, shard)
    stale = sorted(
        path.name
        for path in shards_dir.glob("*.draft.json")
        if path.name not in expected
    )
    if stale:
        raise ValueError(f"stale draft shards require explicit audit: {stale}")


__all__ = [
    "AUDIT_SCHEMA_VERSION",
    "CATALOG_SCHEMA_VERSION",
    "DRAFT_SHARD_SCHEMA_VERSION",
    "PLANNER_CONFIG_SCHEMA_VERSION",
    "PLANNER_VERSION",
    "PlannerConfig",
    "PlannerResult",
    "REVIEW_APPROVED",
    "REVIEW_PENDING",
    "UNRESOLVED_FRAME_SELECTION",
    "load_planner_config",
    "plan_development_recording_coverage",
    "promote_draft_shard",
    "recording_composite_identity",
    "write_planner_outputs",
]
