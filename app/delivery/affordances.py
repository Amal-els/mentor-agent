"""Card affordances (docs/plans/morning-pulse.md M6): Snooze/Not now writes
a Suppression (silence, not deletion — L5's gate picks it up next run);
👍/👎 appends a FeedbackEvent — the instant half of AGENT.md L5's
three-clock weight design, folded into a Weight by the nightly batch
(app/salience/weight_consolidation.py), and also kept as the eval dataset.
Both are simple, direct writes; no ranking or judgment happens here."""

import datetime
import uuid

from app.core.clock import Clock
from app.core.models import FeedbackEvent, Suppression
from app.core.scope import OwnerScope
from app.delivery.config import SNOOZE_TTL_DAYS, VALID_FEEDBACK_SIGNALS


def snooze_item(
    scope: OwnerScope,
    clock: Clock,
    item_id: str,
    reason: str = "snoozed by user",
) -> Suppression:
    now = clock.now()
    row = Suppression(
        id=str(uuid.uuid4()),
        scope="instance",
        target_ref=item_id,
        reason=reason,
        created_at=now,
        expires_at=now + datetime.timedelta(days=SNOOZE_TTL_DAYS),
        created_by="user",
    )
    scope.add(row)
    scope.commit()
    return row


def record_feedback(
    scope: OwnerScope,
    clock: Clock,
    pulse_delivery_id: str,
    item_id: str,
    item_type: str,
    signal: str,
) -> FeedbackEvent:
    """item_type ("event" | "work_item" | "message") is captured here, not
    re-derived later — the nightly consolidation batch (app/salience/
    weight_consolidation.py) groups by it to know which type:{item_type}
    Weight a signal belongs to, and re-deriving it at batch time would mean
    joining item_id back across Event/WorkItem/Message, which breaks if the
    item is gone by the time the batch runs."""
    if signal not in VALID_FEEDBACK_SIGNALS:
        raise ValueError(
            f"invalid feedback signal {signal!r}, must be one of {sorted(VALID_FEEDBACK_SIGNALS)}"
        )
    row = FeedbackEvent(
        id=str(uuid.uuid4()),
        pulse_delivery_id=pulse_delivery_id,
        item_id=item_id,
        item_type=item_type,
        signal=signal,
        created_at=clock.now(),
    )
    scope.add(row)
    scope.commit()
    return row
