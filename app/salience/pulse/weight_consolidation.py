"""L5 write-path (the nightly-batch clock of AGENT.md §5's three-clock
weight design; app/salience/weights.py owns the lazy-decay read-path,
app/delivery/affordances.py's record_feedback owns the instant
append-only event log). Reads every FeedbackEvent not yet folded into a
Weight, groups it by (owner_user_id, item_type), and updates that owner's
type:{item_type} Weight — the same coarse key app/salience/assemble.py
already reads via get_effective_weight.

Deliberately global, not owner-scoped: a nightly maintenance batch has no
"fire in the owner's morning" requirement the way pulse delivery does
(app/triggers/cron_scheduler.py), so it takes a bare Session and processes
every owner's unconsolidated events in one pass — wiring it into a per-run
schedule is app/triggers/cron_scheduler.py's job, not this module's."""

import uuid
from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.clock import Clock
from app.core.models import FeedbackEvent, Weight
from app.salience.pulse.config import WEIGHT_CLAMP_MAX, WEIGHT_CLAMP_MIN, WEIGHT_NEUTRAL

# How far a batch that's *entirely* one signal (net = +-1.0) nudges that
# batch's own target away from neutral, before it's folded into the
# existing weight. A mixed batch nudges proportionally less (net is a
# fraction of +-1). Deliberately smaller than the full clamp range
# ([0.2, 3.0], i.e. +-0.8/+2.0 around neutral) — one nightly batch should
# move a fresh weight noticeably, not swing it to a clamp bound outright;
# reaching the bounds is what many batches of consistent signal does, via
# the n_signals-weighted average below.
SIGNAL_TARGET_RANGE = 0.5


def _clamp(value: float) -> float:
    return max(WEIGHT_CLAMP_MIN, min(WEIGHT_CLAMP_MAX, value))


def consolidate_weights(session: Session, clock: Clock) -> dict:
    """Returns {"events_consolidated": N, "weights_updated": N} for
    logging/observability. Every FeedbackEvent row read here (regardless
    of whether its group produced a net-zero signal) is stamped
    consolidated_at, so a retried/rerun batch never double-counts it."""
    events = (
        session.execute(
            select(FeedbackEvent).where(FeedbackEvent.consolidated_at.is_(None))
        )
        .scalars()
        .all()
    )

    groups: dict[tuple[str, str], list[FeedbackEvent]] = defaultdict(list)
    for event in events:
        groups[(event.owner_user_id, event.item_type)].append(event)

    weights_updated = 0
    for (owner_user_id, item_type), group_events in groups.items():
        key = f"type:{item_type}"
        up = sum(1 for e in group_events if e.signal == "up")
        down = sum(1 for e in group_events if e.signal == "down")
        batch_n = up + down
        if batch_n == 0:
            continue
        net = (up - down) / batch_n  # -1.0 .. 1.0
        batch_target = WEIGHT_NEUTRAL + net * SIGNAL_TARGET_RANGE

        row = session.execute(
            select(Weight).where(
                Weight.owner_user_id == owner_user_id, Weight.key == key
            )
        ).scalar_one_or_none()

        if row is None:
            session.add(
                Weight(
                    id=str(uuid.uuid4()),
                    owner_user_id=owner_user_id,
                    key=key,
                    value=_clamp(batch_target),
                    n_signals=batch_n,
                    updated_at=clock.now(),
                )
            )
        else:
            # Weighted average, damped by accumulated history — AGENT.md
            # L5's "gated by n_signals": a weight with a long history barely
            # moves from one new batch, a fresh one moves close to
            # batch_target outright.
            new_n = row.n_signals + batch_n
            row.value = _clamp(
                (row.value * row.n_signals + batch_target * batch_n) / new_n
            )
            row.n_signals = new_n
            row.updated_at = clock.now()
        weights_updated += 1

    now = clock.now()
    for event in events:
        event.consolidated_at = now

    session.commit()
    return {"events_consolidated": len(events), "weights_updated": weights_updated}
