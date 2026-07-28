"""Task-family catalog for the upgraded verifier's built-in contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence, Tuple


PHASE5_CROSS_APP_SELECTION_KEY = "phase5.cross-app-realism@1"


@dataclass(frozen=True)
class ContractCatalogEntry:
    """A conservative mapping from acquisition metadata to a Contract key."""

    selection_key: str
    task_families: Tuple[str, ...] = ()
    experiment_ids: Tuple[str, ...] = ()
    task_id_prefixes: Tuple[str, ...] = ()
    required_task_text_terms: Tuple[str, ...] = ()

    def matches(self, metadata: Mapping[str, Any]) -> bool:
        task_family = str(metadata.get("task_family") or "")
        experiment_id = str(metadata.get("experiment_id") or "")
        task_id = str(metadata.get("task_id") or "")
        selected = bool(
            (task_family and task_family in self.task_families)
            or (experiment_id and experiment_id in self.experiment_ids)
            or any(task_id.startswith(prefix) for prefix in self.task_id_prefixes)
        )
        task_text = str(metadata.get("task_text") or "")
        return selected and all(term in task_text for term in self.required_task_text_terms)


BUILTIN_CONTRACT_CATALOG: Tuple[ContractCatalogEntry, ...] = (
    ContractCatalogEntry(
        selection_key=PHASE5_CROSS_APP_SELECTION_KEY,
        task_families=(
            "cross_app_ranked_product_research_read_only",
            "cross_app_realism_final_read_only",
        ),
        experiment_ids=(
            "phase5-cross-app-realism-pilot-v3",
            "phase5-cross-app-realism-cohort-v1",
        ),
        required_task_text_terms=("淘宝", "销量", "小红书"),
    ),
)


def task_family_from_run_manifest(run: Mapping[str, Any]) -> str:
    """Read an explicit family or the family segment of ``trace_relpath``."""

    explicit = run.get("task_family")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    trace_ref = run.get("trace_relpath")
    if isinstance(trace_ref, str):
        parts = tuple(part for part in trace_ref.replace("\\", "/").split("/") if part)
        if len(parts) >= 3:
            return parts[-2]
    return ""


def contract_metadata(run: Mapping[str, Any]) -> Mapping[str, str]:
    return {
        "task_id": str(run.get("task_id") or ""),
        "task_family": task_family_from_run_manifest(run),
        "experiment_id": str(run.get("experiment_id") or ""),
        "task_text": str(run.get("task_text") or ""),
    }


def resolve_catalog_selection_key(
    run: Mapping[str, Any],
    catalog: Sequence[ContractCatalogEntry] = BUILTIN_CONTRACT_CATALOG,
) -> str | None:
    metadata = contract_metadata(run)
    matches = tuple(entry.selection_key for entry in catalog if entry.matches(metadata))
    if not matches:
        return None
    unique = tuple(dict.fromkeys(matches))
    if len(unique) != 1:
        raise ValueError(f"ambiguous Contract catalog match: {unique}")
    return unique[0]


__all__ = [
    "BUILTIN_CONTRACT_CATALOG",
    "ContractCatalogEntry",
    "PHASE5_CROSS_APP_SELECTION_KEY",
    "contract_metadata",
    "resolve_catalog_selection_key",
    "task_family_from_run_manifest",
]
