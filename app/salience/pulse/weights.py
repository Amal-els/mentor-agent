"""Weight read-path: lazy decay toward neutral, then clamp. This module
stays read-only by design — the write-path (folding FeedbackEvent signals
into a Weight's value) lives in app/salience/weight_consolidation.py's
nightly batch, kept separate so this function's contract (pure, no DB
writes, safe to call from the hot scoring path) never changes."""

from app.core.clock import Clock
from app.core.models import Weight
from app.core.scope import OwnerScope
from app.salience.pulse.config import (
    WEIGHT_CLAMP_MAX,
    WEIGHT_CLAMP_MIN,
    WEIGHT_DECAY_WINDOW_DAYS,
    WEIGHT_NEUTRAL,
)


def get_effective_weight(scope: OwnerScope, key: str, clock: Clock) -> float:
    row = scope.session.execute(
        scope.query(Weight).where(Weight.key == key)
    ).scalar_one_or_none()
    if row is None:
        return WEIGHT_NEUTRAL

    days_since_update = (clock.now() - row.updated_at).total_seconds() / 86400
    decay_factor = max(0.0, 1.0 - days_since_update / WEIGHT_DECAY_WINDOW_DAYS)
    decayed = WEIGHT_NEUTRAL + (row.value - WEIGHT_NEUTRAL) * decay_factor

    return max(WEIGHT_CLAMP_MIN, min(WEIGHT_CLAMP_MAX, decayed))
