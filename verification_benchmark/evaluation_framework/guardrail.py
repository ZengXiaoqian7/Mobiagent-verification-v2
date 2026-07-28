from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, Mapping, Sequence, Tuple

from .models import CriterionStatus


@dataclass(frozen=True)
class CriterionStateSnapshot:
    intervention_index: int
    statuses: Mapping[str, CriterionStatus]

    def __post_init__(self) -> None:
        if self.intervention_index < 0:
            raise ValueError("intervention_index must be non-negative")


def state_regressions(
    before: CriterionStateSnapshot,
    after: CriterionStateSnapshot,
    *,
    protected_criteria: Iterable[str] | None = None,
) -> Tuple[str, ...]:
    """Return criteria explicitly corrupted from SATISFIED to VIOLATED.

    UNKNOWN after an intervention is evidence loss, not enough to assert state
    corruption. Callers may report it separately without inflating this metric.
    """

    protected = set(protected_criteria) if protected_criteria is not None else set(before.statuses)
    return tuple(
        sorted(
            criterion_id
            for criterion_id in protected
            if before.statuses.get(criterion_id) is CriterionStatus.SATISFIED
            and after.statuses.get(criterion_id) is CriterionStatus.VIOLATED
        )
    )


def observable_state_corruptions(
    before: CriterionStateSnapshot,
    after: CriterionStateSnapshot,
    *,
    criteria: Iterable[str] | None = None,
) -> Tuple[str, ...]:
    """Return S1-visible degradations across adjacent done candidates.

    This metric is intentionally broader than :func:`state_regressions`.
    ``SATISFIED -> UNKNOWN_EVIDENCE`` means that previously observable success
    evidence disappeared after a Guardrail intervention.  It is therefore an
    observable-state corruption for black-box A/B reporting, but it is *not*
    promoted to proven backend corruption and does not change the strict
    regression safety-stop semantics.
    """

    selected = set(criteria) if criteria is not None else set(before.statuses)
    corrupting = {
        CriterionStatus.VIOLATED,
        CriterionStatus.UNKNOWN_EVIDENCE,
    }
    return tuple(
        sorted(
            criterion_id
            for criterion_id in selected
            if before.statuses.get(criterion_id) is CriterionStatus.SATISFIED
            and after.statuses.get(criterion_id) in corrupting
        )
    )


def criterion_oscillation_counts(
    history: Sequence[CriterionStateSnapshot],
) -> Dict[str, int]:
    """Count SATISFIED<->VIOLATED flips across Guardrail interventions."""

    counts: Dict[str, int] = {}
    if len(history) < 2:
        return counts
    ordered = sorted(history, key=lambda snapshot: snapshot.intervention_index)
    criterion_ids = set().union(*(snapshot.statuses.keys() for snapshot in ordered))
    for criterion_id in criterion_ids:
        previous = None
        for snapshot in ordered:
            current = snapshot.statuses.get(criterion_id)
            if current not in {CriterionStatus.SATISFIED, CriterionStatus.VIOLATED}:
                continue
            if previous is not None and current is not previous:
                counts[criterion_id] = counts.get(criterion_id, 0) + 1
            previous = current
    return counts
