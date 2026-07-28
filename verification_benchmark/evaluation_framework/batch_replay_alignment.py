"""Deterministic, development-only batch replay and historical verdict alignment."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Optional, Tuple

from .audit_envelope import (
    audit_report_envelope_json_schema,
    audit_report_envelope_payload,
    audit_report_envelope_sha256,
    build_audit_report_envelope,
)
from .contract_router import route_explicit_legacy
from .event_log import (
    DurableEventTrace,
    TerminationEvent,
    event_trace_sha256,
    trace_bundle_to_event_trace,
)
from .legacy_checker_acquisition import (
    acquire_and_evaluate_legacy_contract,
    legacy_checker_evidence_sha256,
    load_local_legacy_checker_evidence,
)
from .legacy_yaml_adapter import adapt_legacy_yaml
from .models import RunVerdict
from .precomputed_evidence_cache import (
    PrecomputedEvidenceStorage,
    RecordedLlmRequestIR,
    RecordedOcrRequestIR,
    RecordedProviderBindingIR,
    RecordedProviderContext,
    RecordedProviderKind,
    RecordedProviderPlan,
)
from .trace_adapter import load_trace_directory
from .visual_state_evidence_cache import (
    VisualStateEvidenceStorage,
    VisualStateProviderContext,
    composite_evidence_sha256,
    visual_state_provider_plan,
)


BATCH_REPLAY_MANIFEST_SCHEMA_VERSION = "harmony-eval-development-batch-manifest-v1"
BATCH_REPLAY_MANIFEST_SCHEMA_VERSION_V2 = "harmony-eval-development-batch-manifest-v2"
BATCH_REPLAY_RESULT_SCHEMA_VERSION = "harmony-eval-development-batch-result-v1"
BATCH_REPLAY_RESULT_SCHEMA_VERSION_V2 = "harmony-eval-development-batch-result-v2"
BATCH_REPLAY_ENGINE_VERSION = "harmony-eval-development-batch-replay-v1"
BATCH_REPLAY_ENGINE_VERSION_V2 = "harmony-eval-development-batch-replay-v2"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_DECIDED = frozenset((RunVerdict.PASS, RunVerdict.FAIL))


class AlignmentStatus(str, Enum):
    AGREES = "AGREES"
    DISAGREES = "DISAGREES"
    NEW_ABSTAIN = "NEW_ABSTAIN"
    NEW_INVALID_TRACE = "NEW_INVALID_TRACE"
    NEW_UNSUPPORTED = "NEW_UNSUPPORTED"
    NO_HISTORICAL_ROW = "NO_HISTORICAL_ROW"


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key is forbidden: {key}")
        result[key] = value
    return result


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
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ValueError(f"batch source is unreadable: {path}") from exc
    return digest.hexdigest()


def _exact_keys(value: Mapping[str, Any], expected: set[str], context: str) -> None:
    actual = set(value)
    if actual != expected:
        raise ValueError(
            f"{context} fields must be exactly {sorted(expected)}; got {sorted(actual)}"
        )


def _mapping(value: Any, context: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{context} must be an object")
    return value


def _canonical_id(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{context} must be a canonical non-empty string")
    return value


def _sha256(value: Any, context: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError(f"{context} must be a lowercase SHA-256")
    return value


def _reference(value: Any, context: str) -> str:
    reference = _canonical_id(value, context)
    pure = PurePosixPath(reference)
    if pure.is_absolute() or ".." in pure.parts or "\\" in reference:
        raise ValueError(f"{context} must be a repository-relative POSIX path")
    return reference


def _repo_path(root: Path, reference: str) -> Path:
    resolved_root = root.resolve()
    candidate = (resolved_root / PurePosixPath(reference)).resolve()
    try:
        candidate.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError("batch source reference escapes repository root") from exc
    return candidate


@dataclass(frozen=True)
class BoundSource:
    source_ref: str
    file_sha256: str

    def validate(self) -> None:
        _reference(self.source_ref, "source_ref")
        _sha256(self.file_sha256, "file_sha256")

    def payload(self) -> dict[str, str]:
        self.validate()
        return {"source_ref": self.source_ref, "file_sha256": self.file_sha256}


@dataclass(frozen=True)
class ContractSource(BoundSource):
    contract_sha256: str

    def validate(self) -> None:
        super().validate()
        _sha256(self.contract_sha256, "contract_sha256")

    def payload(self) -> dict[str, str]:
        payload = super().payload()
        payload["contract_sha256"] = self.contract_sha256
        return payload


@dataclass(frozen=True)
class CacheSource(BoundSource):
    storage_sha256: str
    provider_plan_sha256: str
    model_version: str

    def validate(self) -> None:
        super().validate()
        _sha256(self.storage_sha256, "storage_sha256")
        _sha256(self.provider_plan_sha256, "provider_plan_sha256")
        _canonical_id(self.model_version, "model_version")

    def payload(self) -> dict[str, str]:
        payload = super().payload()
        payload.update(
            {
                "storage_sha256": self.storage_sha256,
                "provider_plan_sha256": self.provider_plan_sha256,
                "model_version": self.model_version,
            }
        )
        return payload


@dataclass(frozen=True)
class VisualStateCacheSource(BoundSource):
    storage_sha256: str
    provider_plan_sha256: str
    detector_version: str

    def validate(self) -> None:
        super().validate()
        _sha256(self.storage_sha256, "visual storage_sha256")
        _sha256(self.provider_plan_sha256, "visual provider_plan_sha256")
        _canonical_id(self.detector_version, "visual detector_version")

    def payload(self) -> dict[str, str]:
        payload = super().payload()
        payload.update(
            {
                "storage_sha256": self.storage_sha256,
                "provider_plan_sha256": self.provider_plan_sha256,
                "detector_version": self.detector_version,
            }
        )
        return payload


@dataclass(frozen=True)
class HistoricalSystemSource(BoundSource):
    system_id: str
    expected_trace_ids: Tuple[str, ...]

    def validate(self) -> None:
        super().validate()
        _canonical_id(self.system_id, "historical system_id")
        if (
            not isinstance(self.expected_trace_ids, tuple)
            or not self.expected_trace_ids
        ):
            raise ValueError("historical expected_trace_ids must be a non-empty tuple")
        for trace_id in self.expected_trace_ids:
            _canonical_id(trace_id, "historical expected trace_id")
        if len(self.expected_trace_ids) != len(set(self.expected_trace_ids)):
            raise ValueError("historical expected trace_ids must be unique")

    def payload(self) -> dict[str, Any]:
        payload = super().payload()
        payload.update(
            {
                "system_id": self.system_id,
                "expected_trace_ids": list(self.expected_trace_ids),
            }
        )
        return payload


@dataclass(frozen=True)
class BatchReplayManifest:
    batch_id: str
    contract: ContractSource
    provenance_overlay: BoundSource
    cache: CacheSource
    trace_ids: Tuple[str, ...]
    historical_systems: Tuple[HistoricalSystemSource, ...]
    visual_state_cache: Optional[VisualStateCacheSource] = None
    schema_version: str = BATCH_REPLAY_MANIFEST_SCHEMA_VERSION
    mode: str = "AUDIT_BENCHMARK"
    data_role: str = "development"

    def validate(self) -> None:
        if self.schema_version not in {
            BATCH_REPLAY_MANIFEST_SCHEMA_VERSION,
            BATCH_REPLAY_MANIFEST_SCHEMA_VERSION_V2,
        }:
            raise ValueError("unsupported batch manifest schema")
        if self.schema_version == BATCH_REPLAY_MANIFEST_SCHEMA_VERSION:
            if self.visual_state_cache is not None:
                raise ValueError("v1 batch manifests must not bind visual_state_cache")
        elif not isinstance(self.visual_state_cache, VisualStateCacheSource):
            raise ValueError("v2 batch manifests require visual_state_cache")
        _canonical_id(self.batch_id, "batch_id")
        if self.mode != "AUDIT_BENCHMARK":
            raise ValueError("batch replay is Audit-only")
        if self.data_role != "development":
            raise ValueError("batch replay is development-only")
        self.contract.validate()
        self.provenance_overlay.validate()
        self.cache.validate()
        if self.visual_state_cache is not None:
            self.visual_state_cache.validate()
        if not isinstance(self.trace_ids, tuple) or not self.trace_ids:
            raise ValueError("trace_ids must be a non-empty immutable tuple")
        for trace_id in self.trace_ids:
            _canonical_id(trace_id, "trace_id")
        if len(self.trace_ids) != len(set(self.trace_ids)):
            raise ValueError("batch trace_ids must be unique")
        if tuple(sorted(self.trace_ids)) != self.trace_ids:
            raise ValueError("batch trace_ids must be lexicographically ordered")
        if not isinstance(self.historical_systems, tuple):
            raise ValueError("historical_systems must be an immutable tuple")
        for source in self.historical_systems:
            source.validate()
            if not set(source.expected_trace_ids).issubset(self.trace_ids):
                raise ValueError("historical expected trace_ids must be in the batch")
        system_ids = tuple(source.system_id for source in self.historical_systems)
        if len(system_ids) != len(set(system_ids)):
            raise ValueError("historical system_ids must be unique")

    def payload(self) -> dict[str, Any]:
        self.validate()
        payload = {
            "schema_version": self.schema_version,
            "batch_id": self.batch_id,
            "mode": self.mode,
            "data_role": self.data_role,
            "contract": self.contract.payload(),
            "provenance_overlay": self.provenance_overlay.payload(),
            "cache": self.cache.payload(),
            "trace_ids": list(self.trace_ids),
            "historical_systems": [item.payload() for item in self.historical_systems],
        }
        if self.visual_state_cache is not None:
            payload["visual_state_cache"] = self.visual_state_cache.payload()
        return payload

    @property
    def manifest_sha256(self) -> str:
        return _digest(self.payload())


def _parse_bound_source(value: Any, context: str) -> BoundSource:
    value = _mapping(value, context)
    _exact_keys(value, {"source_ref", "file_sha256"}, context)
    return BoundSource(
        _reference(value["source_ref"], f"{context}.source_ref"),
        _sha256(value["file_sha256"], f"{context}.file_sha256"),
    )


def load_batch_replay_manifest(path: Path | str) -> BatchReplayManifest:
    try:
        raw = json.loads(
            Path(path).read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("batch manifest is unreadable") from exc
    root = _mapping(raw, "batch manifest")
    schema_version = root.get("schema_version")
    root_fields = {
        "schema_version",
        "batch_id",
        "mode",
        "data_role",
        "contract",
        "provenance_overlay",
        "cache",
        "trace_ids",
        "historical_systems",
    }
    if schema_version == BATCH_REPLAY_MANIFEST_SCHEMA_VERSION_V2:
        root_fields.add("visual_state_cache")
    _exact_keys(root, root_fields, "batch manifest")
    contract = _mapping(root["contract"], "contract")
    _exact_keys(contract, {"source_ref", "file_sha256", "contract_sha256"}, "contract")
    cache = _mapping(root["cache"], "cache")
    _exact_keys(
        cache,
        {
            "source_ref",
            "file_sha256",
            "storage_sha256",
            "provider_plan_sha256",
            "model_version",
        },
        "cache",
    )
    visual_state_cache = None
    if "visual_state_cache" in root:
        visual = _mapping(root["visual_state_cache"], "visual_state_cache")
        _exact_keys(
            visual,
            {
                "source_ref",
                "file_sha256",
                "storage_sha256",
                "provider_plan_sha256",
                "detector_version",
            },
            "visual_state_cache",
        )
        visual_state_cache = VisualStateCacheSource(
            source_ref=_reference(
                visual["source_ref"], "visual_state_cache.source_ref"
            ),
            file_sha256=_sha256(
                visual["file_sha256"], "visual_state_cache.file_sha256"
            ),
            storage_sha256=_sha256(
                visual["storage_sha256"], "visual_state_cache.storage_sha256"
            ),
            provider_plan_sha256=_sha256(
                visual["provider_plan_sha256"],
                "visual_state_cache.provider_plan_sha256",
            ),
            detector_version=_canonical_id(
                visual["detector_version"], "visual_state_cache.detector_version"
            ),
        )
    systems = root["historical_systems"]
    if not isinstance(systems, list):
        raise ValueError("historical_systems must be a list")
    historical = []
    for index, value in enumerate(systems):
        item = _mapping(value, f"historical_systems[{index}]")
        _exact_keys(
            item,
            {"system_id", "source_ref", "file_sha256", "expected_trace_ids"},
            f"historical_systems[{index}]",
        )
        trace_ids = item["expected_trace_ids"]
        if not isinstance(trace_ids, list):
            raise ValueError("historical expected_trace_ids must be a list")
        historical.append(
            HistoricalSystemSource(
                source_ref=_reference(item["source_ref"], "historical source_ref"),
                file_sha256=_sha256(item["file_sha256"], "historical file_sha256"),
                system_id=_canonical_id(item["system_id"], "historical system_id"),
                expected_trace_ids=tuple(trace_ids),
            )
        )
    trace_ids = root["trace_ids"]
    if not isinstance(trace_ids, list):
        raise ValueError("trace_ids must be a list")
    manifest = BatchReplayManifest(
        schema_version=root["schema_version"],
        batch_id=root["batch_id"],
        mode=root["mode"],
        data_role=root["data_role"],
        contract=ContractSource(
            source_ref=_reference(contract["source_ref"], "contract.source_ref"),
            file_sha256=_sha256(contract["file_sha256"], "contract.file_sha256"),
            contract_sha256=_sha256(
                contract["contract_sha256"], "contract.contract_sha256"
            ),
        ),
        provenance_overlay=_parse_bound_source(
            root["provenance_overlay"], "provenance_overlay"
        ),
        cache=CacheSource(
            source_ref=_reference(cache["source_ref"], "cache.source_ref"),
            file_sha256=_sha256(cache["file_sha256"], "cache.file_sha256"),
            storage_sha256=_sha256(cache["storage_sha256"], "cache.storage_sha256"),
            provider_plan_sha256=_sha256(
                cache["provider_plan_sha256"], "cache.provider_plan_sha256"
            ),
            model_version=_canonical_id(cache["model_version"], "cache.model_version"),
        ),
        trace_ids=tuple(trace_ids),
        historical_systems=tuple(historical),
        visual_state_cache=visual_state_cache,
    )
    manifest.validate()
    return manifest


def _verify_source(root: Path, source: BoundSource) -> Path:
    path = _repo_path(root, source.source_ref)
    actual = _file_sha256(path)
    if not hmac.compare_digest(actual, source.file_sha256):
        raise ValueError(f"batch source hash mismatch: {source.source_ref}")
    return path


def _load_jsonl(path: Path) -> Tuple[Mapping[str, Any], ...]:
    rows = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"JSONL source is unreadable: {path}") from exc
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            raise ValueError(f"blank JSONL row is forbidden at line {line_number}")
        try:
            row = json.loads(line, object_pairs_hook=_reject_duplicate_json_keys)
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValueError(f"invalid JSONL row at line {line_number}") from exc
        rows.append(_mapping(row, f"JSONL row {line_number}"))
    return tuple(rows)


def _validate_development_lineage(
    root: Path, overlay_path: Path, trace_ids: Tuple[str, ...]
) -> None:
    wanted = set(trace_ids)
    found: dict[str, Mapping[str, Any]] = {}
    for row in _load_jsonl(overlay_path):
        trace_id = row.get("trace_id")
        if trace_id in wanted:
            if trace_id in found:
                raise ValueError(
                    f"duplicate provenance row for batch trace: {trace_id}"
                )
            found[trace_id] = row
    if set(found) != wanted:
        raise ValueError("every batch trace must have one provenance overlay row")
    for trace_id, row in found.items():
        if row.get("current_enhanced_role") != "development":
            raise ValueError(f"batch trace is not current development data: {trace_id}")
        if row.get("original_protocol_role") != "development":
            raise ValueError(
                f"batch trace is not originally development data: {trace_id}"
            )
    benchmark_root = root / "verification_benchmark"
    for heldout_path in sorted(benchmark_root.glob("labels_cross_app_heldout*.jsonl")):
        for row in _load_jsonl(heldout_path):
            if row.get("trace_id") in wanted:
                raise ValueError(
                    f"development batch trace appears in held-out labels: {row['trace_id']}"
                )


def _provider_plan(
    contract: Any, manifest: BatchReplayManifest
) -> RecordedProviderPlan:
    if contract.dag is None:
        raise ValueError("batch legacy contract requires a DAG")
    nodes = {
        node.node_id: {checker.checker_id: checker for checker in node.checkers}
        for node in contract.dag.nodes
    }
    try:
        activate_prompt = nodes["activate_search"]["llm"].parameters["prompt"]
        keyword_prompt = nodes["input_keyword"]["llm"].parameters["prompt"]
        nodes["activate_search"]["ocr"]
        nodes["results_page"]["ocr"]
    except KeyError as exc:
        raise ValueError(
            "batch provider plan targets are absent from ContractIR"
        ) from exc
    minimal_plan = RecordedProviderPlan(
        manifest.contract.contract_sha256,
        (
            RecordedProviderBindingIR(
                "activate_search",
                "llm",
                RecordedProviderKind.LLM,
                manifest.cache.model_version,
                RecordedLlmRequestIR(activate_prompt, "legacy-llm-prompt-v1"),
            ),
            RecordedProviderBindingIR(
                "results_page",
                "ocr",
                RecordedProviderKind.OCR,
                manifest.cache.model_version,
                RecordedOcrRequestIR(),
            ),
        ),
    )
    full_plan = RecordedProviderPlan(
        manifest.contract.contract_sha256,
        (
            RecordedProviderBindingIR(
                "activate_search",
                "ocr",
                RecordedProviderKind.OCR,
                manifest.cache.model_version,
                RecordedOcrRequestIR(),
            ),
            RecordedProviderBindingIR(
                "activate_search",
                "llm",
                RecordedProviderKind.LLM,
                manifest.cache.model_version,
                RecordedLlmRequestIR(activate_prompt, "legacy-llm-prompt-v1"),
            ),
            RecordedProviderBindingIR(
                "input_keyword",
                "llm",
                RecordedProviderKind.LLM,
                manifest.cache.model_version,
                RecordedLlmRequestIR(keyword_prompt, "legacy-llm-prompt-v1"),
            ),
            RecordedProviderBindingIR(
                "results_page",
                "ocr",
                RecordedProviderKind.OCR,
                manifest.cache.model_version,
                RecordedOcrRequestIR(),
            ),
        ),
    )
    for plan in (minimal_plan, full_plan):
        plan.validate_against(contract)
        if hmac.compare_digest(plan.plan_sha256, manifest.cache.provider_plan_sha256):
            return plan
    raise ValueError("recorded provider plan hash mismatch")


def project_historical_report(payload: Any) -> Mapping[str, RunVerdict]:
    """Project only historical system decisions; labels and diagnostics never cross in."""

    root = _mapping(payload, "historical report")
    results = root.get("results")
    if not isinstance(results, list):
        raise ValueError("historical report must contain a results list")
    projected: dict[str, RunVerdict] = {}
    for index, raw in enumerate(results):
        row = _mapping(raw, f"historical result {index}")
        trace_id = _canonical_id(row.get("trace_id"), "historical trace_id")
        predicted = row.get("predicted_ok")
        if type(predicted) is not bool:
            raise ValueError("historical predicted_ok must be boolean")
        if trace_id in projected:
            raise ValueError("duplicate historical trace decision is forbidden")
        projected[trace_id] = RunVerdict.PASS if predicted else RunVerdict.FAIL
    return projected


def _load_historical_decisions(
    root: Path, source: HistoricalSystemSource
) -> Mapping[str, RunVerdict]:
    path = _verify_source(root, source)
    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("historical report is unreadable") from exc
    decisions = project_historical_report(payload)
    if set(decisions) != set(source.expected_trace_ids):
        raise ValueError(
            f"historical report trace set differs from manifest: {source.system_id}"
        )
    return decisions


def _termination(trace: DurableEventTrace) -> TerminationEvent:
    events = tuple(
        event for event in trace.events if isinstance(event, TerminationEvent)
    )
    if len(events) != 1:
        raise ValueError("durable trace must contain exactly one termination event")
    return events[0]


def _alignment_status(
    new: RunVerdict, historical: Optional[RunVerdict]
) -> AlignmentStatus:
    if historical is None:
        return AlignmentStatus.NO_HISTORICAL_ROW
    if new is RunVerdict.UNSUPPORTED:
        return AlignmentStatus.NEW_UNSUPPORTED
    if new is RunVerdict.ABSTAIN:
        return AlignmentStatus.NEW_ABSTAIN
    if new is RunVerdict.INVALID_TRACE:
        return AlignmentStatus.NEW_INVALID_TRACE
    if new not in _DECIDED:
        raise ValueError(f"unsupported New Kernel verdict for alignment: {new.value}")
    return AlignmentStatus.AGREES if new is historical else AlignmentStatus.DISAGREES


def _verdict_counts(verdicts: Tuple[RunVerdict, ...]) -> dict[str, int]:
    return {
        verdict.value: sum(item is verdict for item in verdicts)
        for verdict in RunVerdict
    }


def run_batch_replay(
    repo_root: Path | str, manifest: BatchReplayManifest
) -> tuple[dict[str, Any], Tuple[DurableEventTrace, ...]]:
    """Replay local evidence only and return a canonical result plus durable traces."""

    root = Path(repo_root).resolve()
    manifest.validate()
    contract_path = _verify_source(root, manifest.contract)
    overlay_path = _verify_source(root, manifest.provenance_overlay)
    cache_path = _verify_source(root, manifest.cache)
    visual_cache_path = (
        None
        if manifest.visual_state_cache is None
        else _verify_source(root, manifest.visual_state_cache)
    )
    _validate_development_lineage(root, overlay_path, manifest.trace_ids)

    adapted = adapt_legacy_yaml(
        contract_path,
        source_ref=manifest.contract.source_ref,
        expected_contract_sha256=manifest.contract.contract_sha256,
    )
    routed = route_explicit_legacy(adapted)
    contract = routed.contract
    plan = _provider_plan(contract, manifest)
    storage = PrecomputedEvidenceStorage.from_jsonl(cache_path)
    if not hmac.compare_digest(storage.storage_sha256, manifest.cache.storage_sha256):
        raise ValueError("pre-computed evidence storage hash mismatch")
    context = RecordedProviderContext(storage, plan)
    visual_context = None
    visual_plan = None
    visual_storage = None
    if manifest.visual_state_cache is not None:
        if visual_cache_path is None:
            raise ValueError("visual-state cache path is unavailable")
        visual_plan = visual_state_provider_plan(contract)
        if not hmac.compare_digest(
            visual_plan.plan_sha256,
            manifest.visual_state_cache.provider_plan_sha256,
        ):
            raise ValueError("visual-state provider plan hash mismatch")
        detector_versions = {item.detector_version for item in visual_plan.bindings}
        if detector_versions != {manifest.visual_state_cache.detector_version}:
            raise ValueError("visual-state detector version mismatch")
        visual_storage = VisualStateEvidenceStorage.from_jsonl(visual_cache_path)
        if not hmac.compare_digest(
            visual_storage.storage_sha256,
            manifest.visual_state_cache.storage_sha256,
        ):
            raise ValueError("visual-state evidence storage hash mismatch")
        visual_context = VisualStateProviderContext(visual_storage, visual_plan)
    historical = {
        source.system_id: _load_historical_decisions(root, source)
        for source in manifest.historical_systems
    }

    traces: list[DurableEventTrace] = []
    trace_rows: list[dict[str, Any]] = []
    verdicts: list[RunVerdict] = []
    for trace_index, trace_id in enumerate(manifest.trace_ids, 1):
        trace_root = (
            root / "verification_benchmark" / "traces" / PurePosixPath(trace_id)
        )
        bundle = load_trace_directory(trace_root, trace_ref=trace_id)
        durable = trace_bundle_to_event_trace(bundle, contract, trace_id=trace_id)
        evidence = load_local_legacy_checker_evidence(durable, trace_root)
        evaluation = acquire_and_evaluate_legacy_contract(
            contract,
            evidence,
            deadline_reached=True,
            recorded_context=context,
            visual_state_context=visual_context,
            classify_source_evidence_missing=visual_context is not None,
        )
        termination = _termination(durable)
        report = replace(
            evaluation.report,
            termination_quality=termination.quality,
            declared_done_frame=termination.declared_done_frame,
        )
        envelope = build_audit_report_envelope(
            contract,
            durable,
            report,
            selection_audit=routed.audit,
        )
        verdicts.append(envelope.outcome_verdict)
        traces.append(durable)
        trace_rows.append(
            {
                "trace_id": trace_id,
                "durable_trace_ref": f"traces/trace_{trace_index:03d}.event_trace.json",
                "event_trace_sha256": event_trace_sha256(durable),
                "checker_evidence_sha256": legacy_checker_evidence_sha256(evidence),
                "outcome_verdict": envelope.outcome_verdict.value,
                "audit_envelope_sha256": audit_report_envelope_sha256(envelope),
                "audit_envelope": audit_report_envelope_payload(envelope),
            }
        )

    alignment_rows = []
    system_summaries = []
    for source in manifest.historical_systems:
        decisions = historical[source.system_id]
        statuses = []
        comparable = 0
        agreements = 0
        for trace_id, new in zip(manifest.trace_ids, verdicts):
            old = decisions.get(trace_id)
            status = _alignment_status(new, old)
            statuses.append(status)
            if status in (AlignmentStatus.AGREES, AlignmentStatus.DISAGREES):
                comparable += 1
                agreements += status is AlignmentStatus.AGREES
            alignment_rows.append(
                {
                    "system_id": source.system_id,
                    "trace_id": trace_id,
                    "historical_verdict": None if old is None else old.value,
                    "new_kernel_verdict": new.value,
                    "status": status.value,
                }
            )
        system_summaries.append(
            {
                "system_id": source.system_id,
                "historical_rows": len(decisions),
                "comparable_rows": comparable,
                "agreements": agreements,
                "agreement_rate": None if comparable == 0 else agreements / comparable,
                "status_counts": {
                    status.value: sum(item is status for item in statuses)
                    for status in AlignmentStatus
                },
            }
        )

    provider_plan_sha256 = plan.plan_sha256
    evidence_storage_sha256 = storage.storage_sha256
    if visual_plan is not None and visual_storage is not None:
        provider_plan_sha256 = composite_evidence_sha256(
            plan.plan_sha256,
            visual_plan.plan_sha256,
            identity_kind="PROVIDER_CONFIGURATION",
        )
        evidence_storage_sha256 = composite_evidence_sha256(
            storage.storage_sha256,
            visual_storage.storage_sha256,
            identity_kind="EVIDENCE_STORAGE",
        )
    result: dict[str, Any] = {
        "schema_version": (
            BATCH_REPLAY_RESULT_SCHEMA_VERSION_V2
            if visual_context is not None
            else BATCH_REPLAY_RESULT_SCHEMA_VERSION
        ),
        "engine_version": (
            BATCH_REPLAY_ENGINE_VERSION_V2
            if visual_context is not None
            else BATCH_REPLAY_ENGINE_VERSION
        ),
        "batch_id": manifest.batch_id,
        "manifest_sha256": manifest.manifest_sha256,
        "mode": manifest.mode,
        "data_role": manifest.data_role,
        "contract_sha256": manifest.contract.contract_sha256,
        "provider_plan_sha256": provider_plan_sha256,
        "evidence_storage_sha256": evidence_storage_sha256,
        "summary": {
            "trace_count": len(trace_rows),
            "formal_envelope_count": len(trace_rows),
            "verdict_counts": _verdict_counts(tuple(verdicts)),
            "new_kernel_decided_count": sum(item in _DECIDED for item in verdicts),
            "new_kernel_coverage": sum(item in _DECIDED for item in verdicts)
            / len(verdicts),
            "accuracy": None,
        },
        "traces": trace_rows,
        "historical_alignment": {
            "systems": system_summaries,
            "rows": alignment_rows,
        },
        "claim_boundary": {
            "ground_truth_consumed_by_new_kernel": False,
            "historical_diagnostics_consumed_by_new_kernel": False,
            "historical_reports_are_decision_only_alignment_inputs": True,
            "heldout_performance_claimed": False,
            "accuracy_claimed": False,
            "unsupported_is_not_failure": True,
        },
    }
    if visual_plan is not None and visual_storage is not None:
        result["evidence_sources"] = {
            "recorded_ocr_llm": {
                "provider_plan_sha256": plan.plan_sha256,
                "storage_sha256": storage.storage_sha256,
            },
            "visual_state": {
                "provider_plan_sha256": visual_plan.plan_sha256,
                "storage_sha256": visual_storage.storage_sha256,
                "detector_version": manifest.visual_state_cache.detector_version,
            },
        }
    result["result_sha256"] = _digest(result)
    return result, tuple(traces)


def batch_result_json_bytes(result: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            result, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False
        )
        + "\n"
    ).encode("utf-8")


def batch_replay_result_json_schema(
    schema_version: str = BATCH_REPLAY_RESULT_SCHEMA_VERSION,
) -> dict[str, Any]:
    """Return a closed Draft 2020-12 schema for the generated batch artifact."""

    if schema_version not in {
        BATCH_REPLAY_RESULT_SCHEMA_VERSION,
        BATCH_REPLAY_RESULT_SCHEMA_VERSION_V2,
    }:
        raise ValueError("unsupported batch result schema version")
    visual_v2 = schema_version == BATCH_REPLAY_RESULT_SCHEMA_VERSION_V2

    string = {"type": "string", "minLength": 1}
    sha = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
    nonnegative = {"type": "integer", "minimum": 0}
    nullable_number = {"anyOf": [{"type": "number"}, {"type": "null"}]}
    verdicts = [item.value for item in RunVerdict]
    statuses = [item.value for item in AlignmentStatus]
    verdict_counts = {
        "type": "object",
        "additionalProperties": False,
        "required": verdicts,
        "properties": {item: nonnegative for item in verdicts},
    }
    status_counts = {
        "type": "object",
        "additionalProperties": False,
        "required": statuses,
        "properties": {item: nonnegative for item in statuses},
    }
    trace = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "trace_id",
            "durable_trace_ref",
            "event_trace_sha256",
            "checker_evidence_sha256",
            "outcome_verdict",
            "audit_envelope_sha256",
            "audit_envelope",
        ],
        "properties": {
            "trace_id": string,
            "durable_trace_ref": string,
            "event_trace_sha256": sha,
            "checker_evidence_sha256": sha,
            "outcome_verdict": {"enum": verdicts},
            "audit_envelope_sha256": sha,
            "audit_envelope": audit_report_envelope_json_schema(),
        },
    }
    system = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "system_id",
            "historical_rows",
            "comparable_rows",
            "agreements",
            "agreement_rate",
            "status_counts",
        ],
        "properties": {
            "system_id": string,
            "historical_rows": nonnegative,
            "comparable_rows": nonnegative,
            "agreements": nonnegative,
            "agreement_rate": nullable_number,
            "status_counts": status_counts,
        },
    }
    alignment = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "system_id",
            "trace_id",
            "historical_verdict",
            "new_kernel_verdict",
            "status",
        ],
        "properties": {
            "system_id": string,
            "trace_id": string,
            "historical_verdict": {
                "anyOf": [
                    {"enum": [RunVerdict.PASS.value, RunVerdict.FAIL.value]},
                    {"type": "null"},
                ]
            },
            "new_kernel_verdict": {"enum": verdicts},
            "status": {"enum": statuses},
        },
    }
    required = [
        "schema_version",
        "engine_version",
        "batch_id",
        "manifest_sha256",
        "mode",
        "data_role",
        "contract_sha256",
        "provider_plan_sha256",
        "evidence_storage_sha256",
        "summary",
        "traces",
        "historical_alignment",
        "claim_boundary",
        "result_sha256",
    ]
    if visual_v2:
        required.append("evidence_sources")
    properties: dict[str, Any] = {
        "schema_version": {"const": schema_version},
        "engine_version": {
            "const": (
                BATCH_REPLAY_ENGINE_VERSION_V2
                if visual_v2
                else BATCH_REPLAY_ENGINE_VERSION
            )
        },
        "batch_id": string,
        "manifest_sha256": sha,
        "mode": {"const": "AUDIT_BENCHMARK"},
        "data_role": {"const": "development"},
        "contract_sha256": sha,
        "provider_plan_sha256": sha,
        "evidence_storage_sha256": sha,
        "summary": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "trace_count",
                "formal_envelope_count",
                "verdict_counts",
                "new_kernel_decided_count",
                "new_kernel_coverage",
                "accuracy",
            ],
            "properties": {
                "trace_count": nonnegative,
                "formal_envelope_count": nonnegative,
                "verdict_counts": verdict_counts,
                "new_kernel_decided_count": nonnegative,
                "new_kernel_coverage": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 1,
                },
                "accuracy": {"type": "null"},
            },
        },
        "traces": {"type": "array", "minItems": 1, "items": trace},
        "historical_alignment": {
            "type": "object",
            "additionalProperties": False,
            "required": ["systems", "rows"],
            "properties": {
                "systems": {"type": "array", "items": system},
                "rows": {"type": "array", "items": alignment},
            },
        },
        "claim_boundary": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "ground_truth_consumed_by_new_kernel",
                "historical_diagnostics_consumed_by_new_kernel",
                "historical_reports_are_decision_only_alignment_inputs",
                "heldout_performance_claimed",
                "accuracy_claimed",
                "unsupported_is_not_failure",
            ],
            "properties": {
                "ground_truth_consumed_by_new_kernel": {"const": False},
                "historical_diagnostics_consumed_by_new_kernel": {"const": False},
                "historical_reports_are_decision_only_alignment_inputs": {
                    "const": True
                },
                "heldout_performance_claimed": {"const": False},
                "accuracy_claimed": {"const": False},
                "unsupported_is_not_failure": {"const": True},
            },
        },
        "result_sha256": sha,
    }
    if visual_v2:
        properties["evidence_sources"] = {
            "type": "object",
            "additionalProperties": False,
            "required": ["recorded_ocr_llm", "visual_state"],
            "properties": {
                "recorded_ocr_llm": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["provider_plan_sha256", "storage_sha256"],
                    "properties": {
                        "provider_plan_sha256": sha,
                        "storage_sha256": sha,
                    },
                },
                "visual_state": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "provider_plan_sha256",
                        "storage_sha256",
                        "detector_version",
                    ],
                    "properties": {
                        "provider_plan_sha256": sha,
                        "storage_sha256": sha,
                        "detector_version": string,
                    },
                },
            },
        }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Harmony Evaluation Development Batch Replay Result",
        "type": "object",
        "additionalProperties": False,
        "required": required,
        "properties": properties,
    }


__all__ = [
    "AlignmentStatus",
    "BATCH_REPLAY_ENGINE_VERSION",
    "BATCH_REPLAY_ENGINE_VERSION_V2",
    "BATCH_REPLAY_MANIFEST_SCHEMA_VERSION",
    "BATCH_REPLAY_MANIFEST_SCHEMA_VERSION_V2",
    "BATCH_REPLAY_RESULT_SCHEMA_VERSION",
    "BATCH_REPLAY_RESULT_SCHEMA_VERSION_V2",
    "BatchReplayManifest",
    "VisualStateCacheSource",
    "batch_result_json_bytes",
    "batch_replay_result_json_schema",
    "load_batch_replay_manifest",
    "project_historical_report",
    "run_batch_replay",
]
