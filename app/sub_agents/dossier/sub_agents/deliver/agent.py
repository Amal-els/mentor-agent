"""Layer 7: renders the card, sends it, records the DossierDelivery row."""

import logging
import uuid

from sqlalchemy import select

from app.core.clock import Clock, SystemClock
from app.core.models import DossierDelivery
from app.core.scope import OwnerScope
from app.delivery.cards import build_dossier_card
from app.delivery.slack_deliverer import SlackDeliverer
from app.delivery.tts import deliver_dossier_audio
from app.sub_agents.dossier.sub_agents.synthesize.agent import DossierCard

logger = logging.getLogger(__name__)


def deliver_dossier(
    card: DossierCard,
    event_external_id: str,
    owner_scope: OwnerScope,
    slack_user_id: str,
    prompt_version: str,
    talking_points_source: str,
    deliverer: SlackDeliverer | None = None,
    clock: Clock | None = None,
    candidate_score: float | None = None,
    event_title: str | None = None,
    event_starts_at: str | None = None,
) -> DossierDelivery:
    # Check-then-send, scoped to this function so every caller (run_
    # dossier_flow's push path, pull_dossier's on-demand path) gets the fix
    # for free: (owner_user_id, event_external_id) is UNIQUE on
    # dossier_deliveries, and a second /mentor prep (or a push/pull race)
    # for the same still-upcoming event previously sent a second real Slack
    # DM and THEN crashed on the commit's IntegrityError. Returning the
    # existing row instead means no duplicate DM and no crash — the caller
    # gets a real DossierDelivery back either way.
    existing = owner_scope.session.execute(
        select(DossierDelivery).where(
            DossierDelivery.owner_user_id == owner_scope.owner_user_id,
            DossierDelivery.event_external_id == event_external_id,
        )
    ).scalar_one_or_none()
    if existing is not None:
        # Previously silent: a repeat /mentor prep for an already-delivered
        # event ran the whole gather/synthesize pipeline (a real LLM call)
        # and then dropped the result on the floor with no user-visible
        # signal at all — indistinguishable from a silent failure. Post a
        # normal, TOP-LEVEL message instead of a threaded reply — a thread
        # reply only shows up if the requester scrolls back up to the
        # original card and expands it, which isn't the instinctive place
        # to look; a fresh message at the bottom of the DM, immediately
        # visible, is. A clickable permalink (get_permalink) still points
        # back at the original card so it's one click away either way.
        # existing.card_ref is None only when the original send itself
        # never actually succeeded (sent_at is also None in that case) —
        # nothing to link to, so skip rather than reference a message that
        # was never sent.
        deliverer = deliverer or SlackDeliverer()
        if deliverer.enabled and existing.card_ref is not None:
            permalink = (
                deliverer.get_permalink(existing.dm_channel, existing.card_ref)
                if existing.dm_channel
                else None
            )
            text = "Nothing new since your last prep for this meeting."
            if permalink:
                text += f" <{permalink}|Jump to it>."
            deliverer.deliver(
                slack_user_id,
                blocks=[
                    {"type": "section", "text": {"type": "mrkdwn", "text": text}}
                ],
            )
        return existing

    deliverer = deliverer or SlackDeliverer()
    clock = clock or SystemClock()
    now = clock.now()
    blocks = build_dossier_card(
        card, event_title=event_title, event_starts_at=event_starts_at
    )

    result = (
        deliverer.deliver(slack_user_id, blocks)
        if deliverer.enabled
        else {"sent": False}
    )

    delivery = DossierDelivery(
        id=str(uuid.uuid4()),
        owner_user_id=owner_scope.owner_user_id,
        event_external_id=event_external_id,
        sent_at=now if result.get("sent") else None,
        prompt_version=prompt_version,
        talking_points_source=talking_points_source,
        # SlackDeliverer.deliver()'s real return shape carries the sent
        # message's ts under "dm_ts" (see its own docstring on why —
        # files_upload_v2's completeUploadExternal needs the resolved DM
        # channel/ts, not the bare "ts" key a naive reading would expect).
        # A plain "ts" key was read here before, which meant card_ref was
        # silently always None against the real deliverer — every dossier
        # test used a fake deliverer with an invented {"ts": ...} shape
        # that never matched the real interface, so nothing caught it.
        card_ref=result.get("dm_ts"),
        dm_channel=result.get("dm_channel"),
        feedback="none",
        feedback_at=None,
        created_at=now,
        who_summary="; ".join(card.who),
        why_now=card.why_now,
        candidate_score=candidate_score,
    )
    owner_scope.add(delivery)
    owner_scope.commit()

    if result.get("sent") and result.get("dm_channel") and result.get("dm_ts"):
        # Best-effort audio companion, threaded under the card that just
        # sent — never blocks or fails the delivery itself (deliver_
        # dossier_audio never raises, same "enhancement, never a
        # dependency" contract as pulse's own audio companion).
        try:
            deliver_dossier_audio(
                deliverer,
                card,
                result["dm_channel"],
                result["dm_ts"],
                event_title=event_title,
            )
        except Exception:
            logger.exception(
                "deliver_dossier: audio companion failed, text card already sent"
            )

    return delivery
