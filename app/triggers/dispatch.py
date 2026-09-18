"""Dispatches a TriggerEvent through the L6 pipeline. The only DB-touching
part of L1; TriggerEvent construction itself stays pure.

Idempotency is cron-only: a retried cron firing must not deliver twice
(docs/plans/morning-pulse.md M5). Pull is user-initiated each time and is
never blocked — pulse.md's edge case table: "Called twice in one day |
Second call is fine (pull)." Both triggers still write at most one
PulseDelivery row per (owner, ritual, local_date, trigger) — a second pull
the same day updates that row in place (M6 will use it to render "since
HH:MM" deltas) rather than violating the unique constraint or duplicating."""

import datetime
import uuid
from dataclasses import dataclass
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from app.core.clock import Clock
from app.core.models import PulseDelivery, User
from app.core.scope import OwnerScope
from app.delivery.cards import build_card, card_to_dict
from app.pipeline.pulse import RenderedPulse, context_hash, run_pulse
from app.triggers.pulse_trigger import TriggerEvent


@dataclass(frozen=True)
class TriggerResult:
    delivered: bool
    idempotent_skip: bool
    rendered: RenderedPulse | None
    local_date: datetime.date
    previous_item_ids: list[str] | None = None
    previous_delivered_at: datetime.datetime | None = None
    # None only for an idempotent-skip or a dry_run — both never touch
    # PulseDelivery. cards.py needs this to embed on each feedback button
    # (app/delivery/affordances.py's record_feedback), so a click can be
    # attributed back to the delivery it came from.
    delivery_id: str | None = None


def _local_date_for(user: User | None, event: TriggerEvent) -> datetime.date:
    if "date" in event.params:
        return datetime.date.fromisoformat(event.params["date"])
    tz = ZoneInfo(user.tz) if user and user.tz else datetime.UTC
    return event.requested_at.astimezone(tz).date()


def handle_trigger(
    scope: OwnerScope,
    clock: Clock,
    source_clients: list,
    event: TriggerEvent,
    dry_run: bool = False,
) -> TriggerResult:
    user = scope.session.get(User, event.owner_user_id)
    local_date = _local_date_for(user, event)

    existing = None
    if not dry_run:
        existing = scope.session.execute(
            scope.query(PulseDelivery).where(
                PulseDelivery.ritual == "pulse",
                PulseDelivery.local_date == local_date,
                PulseDelivery.trigger == event.trigger,
            )
        ).scalar_one_or_none()
        if event.trigger == "cron" and existing is not None:
            return TriggerResult(
                delivered=False,
                idempotent_skip=True,
                rendered=None,
                local_date=local_date,
            )

    previous_item_ids = list(existing.item_ids) if existing is not None else None
    previous_delivered_at = existing.delivered_at if existing is not None else None

    # Release the connection back to the pool before the long ranker/writer/
    # critic LLM loop inside run_pulse (routinely 20-30s, zero DB activity).
    # pool_pre_ping (app/core/db.py) only re-validates a connection at
    # checkout time — holding one checked-out-but-idle across that whole
    # gap means there's no checkout event for pre_ping to catch a dead
    # connection at, so the final scope.commit() below inherits whatever
    # connection was open before the LLM call and fails outright if it was
    # dropped in the meantime (found live: a real run's PulseDelivery write
    # failed with "consuming input failed: could not receive data from
    # server" after a ~30s LLM call). Nothing is pending here yet (existing
    # was only read, not mutated), so this commit is a plain transaction
    # boundary, not a real write.
    scope.session.commit()

    rendered = run_pulse(
        scope, clock, source_clients, event.trigger, event.requested_at
    )

    delivery_id: str | None = None
    if not dry_run:
        ctx_hash = context_hash(
            [
                {"item_id": item.item_id, "score": item.score}
                for item in rendered.context.shortlist
            ]
        )
        if existing is not None:
            existing.delivered_at = clock.now()
            existing.item_ids = rendered.ordered_item_ids
            existing.context_hash = ctx_hash
            existing.prompt_version = rendered.ranker_prompt_id
            delivery_id = existing.id
            row = existing
        else:
            delivery_id = str(uuid.uuid4())
            row = PulseDelivery(
                id=delivery_id,
                ritual="pulse",
                local_date=local_date,
                trigger=event.trigger,
                delivered_at=clock.now(),
                item_ids=rendered.ordered_item_ids,
                context_hash=ctx_hash,
                prompt_version=rendered.ranker_prompt_id,
            )
            scope.add(row)

        # Denormalized card snapshot (see PulseDelivery.card_json's own
        # docstring) — built from a throwaway SimpleNamespace rather than
        # the real TriggerResult below, since build_card only ever reads
        # .rendered/.previous_delivered_at/.delivery_id off whatever it's
        # given, and this function's own TriggerResult isn't constructed
        # until after this block. Every caller that wants Slack blocks
        # still calls build_card(trigger_result) itself afterward — this
        # is a second, cheap (pure, no I/O) call for a different consumer
        # (the /ui SSE push), not a replacement for that path.
        row.card_json = card_to_dict(
            build_card(
                SimpleNamespace(
                    rendered=rendered,
                    previous_delivered_at=previous_delivered_at,
                    delivery_id=delivery_id,
                )
            )
        )
        scope.commit()

    return TriggerResult(
        delivered=True,
        idempotent_skip=False,
        rendered=rendered,
        local_date=local_date,
        previous_item_ids=previous_item_ids,
        previous_delivered_at=previous_delivered_at,
        delivery_id=delivery_id,
    )
