"""Orchestrates the SCHEDULED push path (design spec §3, steps 1-8, minus
the day/time gate itself — that's friday_review_scheduler.py's job):
suppression check -> gather -> synthesize -> deliver -> identity batch
commit. Mirrors app.sub_agents.dossier.agent's run_dossier_flow shape:
llm_agent built internally via build_synthesize_agent() when the caller
doesn't supply one (a None default would crash the real flow the moment
synthesize_friday_review calls run_agent_sync on it)."""

import collections
import dataclasses
import datetime
import logging
import uuid

from google.adk.agents import BaseAgent
from google.adk.events import Event
from google.adk.events.event_actions import EventActions
from sqlalchemy import select

from app.agenda.models import Accomplishment
from app.core.clock import Clock
from app.core.models import FridayReviewDelivery, Suppression, User
from app.core.scope import OwnerScope
from app.identity.friday_batch import build_batch, commit_batch
from app.sub_agents.friday_review.sub_agents.deliver.agent import deliver_friday_review
from app.sub_agents.friday_review.sub_agents.gather.agent import (
    gather_friday_review_context,
    week_start_monday,
)
from app.sub_agents.friday_review.sub_agents.synthesize.agent import (
    PROMPT_VERSION,
    build_synthesize_agent,
    format_skill_distribution_counts,
    synthesize_friday_review,
)

logger = logging.getLogger(__name__)

SUPPRESSION_TARGET_REF = "friday_review"


def _is_on_leave(owner_scope: OwnerScope, now: datetime.datetime) -> bool:
    rows = (
        owner_scope.session.execute(
            select(Suppression).where(
                Suppression.owner_user_id == owner_scope.owner_user_id,
                Suppression.target_ref == SUPPRESSION_TARGET_REF,
            )
        )
        .scalars()
        .all()
    )
    return any(row.expires_at > now for row in rows)


def _existing_scheduled_delivery(owner_scope: OwnerScope, week_start):
    return owner_scope.session.execute(
        select(FridayReviewDelivery).where(
            FridayReviewDelivery.owner_user_id == owner_scope.owner_user_id,
            FridayReviewDelivery.week_start_date == week_start,
            FridayReviewDelivery.trigger == "scheduled",
        )
    ).scalar_one_or_none()


def persist_new_skill_classifications(owner_scope: OwnerScope, card, clock: Clock) -> dict:
    """REAL BUG FOUND AND FIXED (confirmed live: a Friday reflection card
    said "Skill distribution: Not enough categorized history yet." for a
    user with real, meeting-logged accomplishments). synthesize_friday_
    review's LLM classifies a skill_category for every win missing one —
    but for an already_logged win (a real, pre-existing Accomplishment
    row, as opposed to a not-yet-confirmed proposal), that classification
    used to live only on the in-memory ScoredWin/card and was NEVER
    written back to the real row. _gather_skill_distribution only ever
    counts Accomplishment rows with skill_category already set
    (app.sub_agents.friday_review.sub_agents.gather.agent), so for anyone
    whose accomplishments come from meeting synthesis or the manual "add
    to ledger" endpoint (i.e. almost everyone — Friday review's own
    "Confirm & log" flow is a minority path), the distribution query
    found zero rows FOREVER, no matter how many weeks passed.

    Keyed by the real Accomplishment.id (ScoredWin.id, threaded through
    from WinEvidence.id — see that field's own docstring), never by
    source_reference_key: that key is deliberately NOT unique per row
    (e.g. every meeting-synthesized accomplishment for a report shares
    "agenda:{report_user_id}"), so matching on it here would risk
    stamping one win's classification onto a different, unrelated row
    that just happens to share the same key.

    Returns the classifications actually just persisted (id -> category
    is NOT what's needed downstream — only the category counts are), as
    a plain category -> count dict, so the caller can fold them into this
    week's own displayed distribution instead of only showing up starting
    next week's gather query."""
    newly_classified = collections.Counter()
    for win in card.wins:
        if not win.already_logged or not win.id or not win.skill_category:
            continue
        row = owner_scope.session.execute(
            owner_scope.query(Accomplishment).where(
                Accomplishment.id == win.id, Accomplishment.skill_category.is_(None)
            )
        ).scalar_one_or_none()
        if row is None:
            # Either already classified by a prior run (nothing new to
            # persist) or genuinely not found — either way, nothing to do.
            continue
        row.skill_category = win.skill_category
        newly_classified[win.skill_category] += 1
    if newly_classified:
        owner_scope.commit()
    return dict(newly_classified)


def refresh_card_with_this_weeks_classifications(card, context, newly_classified: dict):
    if not newly_classified:
        return card
    merged_counts = collections.Counter(context.skill_distribution.counts)
    merged_counts.update(newly_classified)
    return dataclasses.replace(
        card, skill_distribution_summary=format_skill_distribution_counts(dict(merged_counts))
    )


def run_friday_review_flow(
    owner_scope: OwnerScope, clock: Clock, llm_agent=None, deliverer=None
) -> FridayReviewDelivery | None:
    now = clock.now()
    week_start = week_start_monday(now)

    existing = _existing_scheduled_delivery(owner_scope, week_start)
    if existing is not None:
        return existing

    if _is_on_leave(owner_scope, now):
        skip_row = FridayReviewDelivery(
            id=str(uuid.uuid4()),
            owner_user_id=owner_scope.owner_user_id,
            week_start_date=week_start,
            trigger="scheduled",
            sent_at=None,
            skipped_reason="on_leave",
            prompt_version=PROMPT_VERSION,
            card_ref=None,
            proposed_ledger_items=[],
            ledger_confirmed_at=None,
            created_at=now,
        )
        owner_scope.add(skip_row)
        owner_scope.commit()
        return skip_row

    context = gather_friday_review_context(owner_scope, clock)
    agent = llm_agent if llm_agent is not None else build_synthesize_agent()
    card = synthesize_friday_review(context, agent)

    try:
        newly_classified = persist_new_skill_classifications(owner_scope, card, clock)
        card = refresh_card_with_this_weeks_classifications(card, context, newly_classified)
    except Exception:
        # Same posture as the identity-batch try/except below: a failure
        # here must never block the card that's already been synthesized
        # from sending — worst case, this week's card falls back to the
        # pre-fix "not enough categorized history yet" text, exactly like
        # before this fix existed, not a broken delivery.
        logger.exception(
            "run_friday_review_flow: skill classification persistence "
            "failed, sending the card as synthesized"
        )

    user = owner_scope.session.get(User, owner_scope.owner_user_id)
    delivery = deliver_friday_review(
        card, week_start=week_start, trigger="scheduled", owner_scope=owner_scope,
        slack_user_id=user.slack_user_id, prompt_version=PROMPT_VERSION,
        deliverer=deliverer, clock=clock,
    )

    if delivery.sent_at is not None:
        try:
            batch = build_batch(owner_scope)
            if batch:
                commit_batch(owner_scope, batch, delivery.card_ref or "", clock)
        except Exception:
            logger.exception(
                "run_friday_review_flow: identity batch commit failed, card already sent"
            )

    return delivery


class FridayReviewOrchestrator(BaseAgent):
    """ADK-invoked entry point, mirrors DossierOrchestrator."""

    async def _run_async_impl(self, ctx):
        owner_scope = ctx.session.state["owner_scope"]
        clock = ctx.session.state["clock"]
        delivery = run_friday_review_flow(owner_scope, clock)
        state_delta = {"friday_review_delivery_id": delivery.id if delivery is not None else None}
        yield Event(author=self.name, actions=EventActions(state_delta=state_delta))
