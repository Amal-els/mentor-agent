"""Suppression + budget: the only step where trigger affects L5 output
(decision 3 of docs/plans/morning-pulse.md). Pull bypasses both entirely —
never L2 permissions, only this gate. Cron applies active suppressions
(narrower scope wins the reported reason when several match), then caps to
the push budget. Every removal is named — that's what makes the cron/pull
delta accountable rather than silent drift."""

import datetime

from app.core.clock import Clock
from app.core.models import Suppression
from app.core.scope import OwnerScope
from app.salience.pulse.config import BUDGET_PUSH_MAX_ITEMS
from app.salience.pulse.types import PreGateContext, Removal, ScoredItem

_SCOPE_ORDER = {"instance": 0, "series": 1, "temporal": 2, "global": 3}


def _matches(
    item: ScoredItem, suppression: Suppression, window_date: datetime.date
) -> bool:
    if suppression.scope == "instance":
        return suppression.target_ref == item.item_id
    if suppression.scope == "series":
        return item.series_id is not None and suppression.target_ref == item.series_id
    if suppression.scope == "temporal":
        return suppression.target_ref == f"temporal:{window_date.isoformat()}"
    if suppression.scope == "global":
        return True
    return False


def _suppression_reason(
    item: ScoredItem, active: list[Suppression], window_date: datetime.date
) -> str | None:
    matches = [s for s in active if _matches(item, s, window_date)]
    if not matches:
        return None
    narrowest = min(matches, key=lambda s: _SCOPE_ORDER[s.scope])
    return f"{narrowest.scope}_suppression:{narrowest.target_ref}"


def apply_gate(
    scope: OwnerScope, clock: Clock, pre_gate: PreGateContext, trigger: str
) -> tuple[list[ScoredItem], list[Removal]]:
    if trigger == "pull":
        return list(pre_gate.shortlist), []

    active = list(
        scope.session.execute(
            scope.query(Suppression).where(Suppression.expires_at > clock.now())
        )
        .scalars()
        .all()
    )

    survivors: list[ScoredItem] = []
    removals: list[Removal] = []
    for item in pre_gate.shortlist:
        reason = _suppression_reason(item, active, pre_gate.window_date)
        if reason is not None:
            removals.append(Removal(item_id=item.item_id, reason=reason))
        else:
            survivors.append(item)

    budgeted = survivors[:BUDGET_PUSH_MAX_ITEMS]
    removals.extend(
        Removal(item_id=item.item_id, reason="budget")
        for item in survivors[BUDGET_PUSH_MAX_ITEMS:]
    )

    return budgeted, removals
