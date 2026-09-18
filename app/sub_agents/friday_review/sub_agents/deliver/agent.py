"""Layer 7 for F4: renders the card, sends it, records FridayReviewDelivery,
and — only on explicit "Confirm & log" — writes real Accomplishment rows.

deliver_friday_review's dedupe follows dispatch.py's handle_trigger shape
exactly (cron-vs-pull), not deliver_dossier's check-then-return-existing
shape: a "scheduled" trigger idempotent-skips (returns the existing row,
sends nothing) if one already exists for (owner, week); a "pull" trigger
always re-sends and updates the existing (owner, week, "pull") row in
place rather than colliding on the unique constraint — same reasoning
PulseDelivery's own (owner, ritual, local_date, trigger) uniqueness uses
to "let a same-day pull render deltas" (its own docstring).

Known, stated gap (design spec §11): if a second pull the same week
refreshes proposed_ledger_items on a row whose ledger_confirmed_at is
already set, confirm_and_log_ledger_items will still no-op on it — the
newly-refreshed proposals from that second pull won't get a fresh
confirmation opportunity this pass. Not solved here, same posture as
every other explicitly-scoped-out gap in this design."""

import logging
import uuid

from sqlalchemy import select

from app.agenda.models import Accomplishment
from app.core.clock import Clock, SystemClock
from app.core.models import FridayReviewDelivery
from app.core.scope import OwnerScope
from app.delivery.cards import build_friday_review_card
from app.delivery.slack_deliverer import SlackDeliverer
from app.sub_agents.friday_review.sub_agents.synthesize.agent import (
    FridayReviewCard,
    build_proposed_ledger_items,
)

logger = logging.getLogger(__name__)


def _existing_delivery(owner_scope: OwnerScope, week_start, trigger: str):
    return owner_scope.session.execute(
        select(FridayReviewDelivery).where(
            FridayReviewDelivery.owner_user_id == owner_scope.owner_user_id,
            FridayReviewDelivery.week_start_date == week_start,
            FridayReviewDelivery.trigger == trigger,
        )
    ).scalar_one_or_none()


def deliver_friday_review(
    card: FridayReviewCard,
    week_start,
    trigger: str,
    owner_scope: OwnerScope,
    slack_user_id: str,
    prompt_version: str,
    deliverer: SlackDeliverer | None = None,
    clock: Clock | None = None,
) -> FridayReviewDelivery:
    clock = clock or SystemClock()
    now = clock.now()
    existing = _existing_delivery(owner_scope, week_start, trigger)

    if trigger == "scheduled" and existing is not None:
        return existing

    deliverer = deliverer or SlackDeliverer()
    proposed_ledger_items = build_proposed_ledger_items(card.wins)
    delivery_id = existing.id if existing is not None else str(uuid.uuid4())
    blocks = build_friday_review_card(card, delivery_id, proposed_ledger_items)

    result = deliverer.deliver(slack_user_id, blocks) if deliverer.enabled else {"sent": False}

    if existing is not None:
        existing.sent_at = now if result.get("sent") else None
        existing.prompt_version = prompt_version
        existing.card_ref = result.get("dm_ts")
        existing.proposed_ledger_items = proposed_ledger_items
        delivery = existing
    else:
        delivery = FridayReviewDelivery(
            id=delivery_id,
            owner_user_id=owner_scope.owner_user_id,
            week_start_date=week_start,
            trigger=trigger,
            sent_at=now if result.get("sent") else None,
            skipped_reason=None,
            prompt_version=prompt_version,
            card_ref=result.get("dm_ts"),
            proposed_ledger_items=proposed_ledger_items,
            ledger_confirmed_at=None,
            created_at=now,
        )
        owner_scope.add(delivery)
    owner_scope.commit()

    if result.get("sent") and result.get("dm_channel") and result.get("dm_ts"):
        from app.delivery.tts import deliver_friday_review_audio

        try:
            deliver_friday_review_audio(deliverer, card, result["dm_channel"], result["dm_ts"])
        except Exception:
            logger.exception(
                "deliver_friday_review: audio companion failed, text card already sent"
            )

    return delivery


def confirm_and_log_ledger_items(
    owner_scope: OwnerScope, clock: Clock, delivery_id: str
) -> list:
    """The write-back (design spec §2's correction): writes directly via
    OwnerScope only — no PairScope, no agenda mirror, unlike
    app.agenda.store.append_ledger_item. Idempotent on ledger_confirmed_at."""
    delivery = owner_scope.session.execute(
        select(FridayReviewDelivery).where(
            FridayReviewDelivery.id == delivery_id,
            FridayReviewDelivery.owner_user_id == owner_scope.owner_user_id,
        )
    ).scalar_one_or_none()
    if delivery is None or delivery.ledger_confirmed_at is not None:
        return []
    if not delivery.proposed_ledger_items:
        return []

    now = clock.now()
    created: list[Accomplishment] = []
    for item in delivery.proposed_ledger_items:
        row = Accomplishment(
            id=str(uuid.uuid4()),
            owner_user_id=owner_scope.owner_user_id,
            description=item["description"],
            source_reference_key=item["source_reference_key"],
            occurred_at=now,
            skill_category=item.get("skill_category"),
        )
        owner_scope.add(row)
        created.append(row)
    delivery.ledger_confirmed_at = now
    owner_scope.commit()
    return created
