"""Dossier-specific salience gate. New module rather than a call into
app.salience.pulse.gate.apply_gate: that function operates on pulse's
PreGateContext/ScoredItem types and pulse-only config, per the design
spec §4's correction — not a generic cross-ritual gate."""

from typing import Literal

from app.core.clock import Clock
from app.core.models import Suppression
from app.core.scope import OwnerScope

DOSSIER_PUSH_BUDGET_MAX_PER_DAY = 3
TOP_PERCENTILE_THRESHOLD = 0.80  # top 20% of history

GateDecision = Literal["push", "queue", "drop"]


def _is_suppressed(scope: OwnerScope, clock: Clock, event_external_id: str) -> bool:
    now = clock.now()
    rows = (
        scope.session.execute(
            scope.query(Suppression).where(Suppression.target_ref == event_external_id)
        )
        .scalars()
        .all()
    )
    for row in rows:
        if row.expires_at > now:
            return True
    return False


def _is_top_percentile(candidate_score: float, history_scores: list[float]) -> bool:
    if not history_scores:
        return True
    sorted_history = sorted(history_scores)
    cutoff_index = int(len(sorted_history) * TOP_PERCENTILE_THRESHOLD)
    cutoff_index = min(cutoff_index, len(sorted_history) - 1)
    threshold = sorted_history[cutoff_index]
    return candidate_score >= threshold

def apply_dossier_gate(
    scope: OwnerScope,
    clock: Clock,
    candidate_score: float,
    event_external_id: str,
    is_manager: bool,
    history_scores: list[float],
) -> GateDecision:
    if _is_suppressed(scope, clock, event_external_id):
        return "drop"
    if is_manager:
        return "push"
    if not _is_top_percentile(candidate_score, history_scores):
        return "drop"
    return "push"
