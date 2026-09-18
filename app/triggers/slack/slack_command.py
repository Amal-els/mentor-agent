"""L1 trigger: the `/mentor pulse` and `/mentor prep` Slack slash-command
entrypoints (AGENT.md §1's interaction surface, docs/plans/morning-pulse.md,
app/intents/prep-meeting.md). Mounted at POST /slack/commands in
app/fast_api_app.py, which is responsible for signature verification
(app/triggers/slack_signature.py) and acking within Slack's 3s window
before handing off to run_pulse_command / run_prep_command as a background
task — a live ingest + rank + write round-trip (pulse) or a live
calendar-fetch + gather + LLM-synthesize + deliver round-trip (prep)
routinely takes longer than that. Only "pulse" and "prep" are wired;
everything else in AGENT.md's `/mentor prep|review|memory|quiet` list is
out of scope here."""

import dataclasses
import datetime
import logging
import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.clock import Clock, SystemClock
from app.core.models import User
from app.core.scope import OwnerScope
from app.delivery.cards import build_card, render_blocks, render_shortlist_blocks
from app.delivery.slack_deliverer import SlackDeliverer
from app.delivery.tts import deliver_pulse_audio
from app.ingest.base import Window
from app.ingest.live_source import (
    LiveJiraClient,
    LiveLinearClient,
    LiveNotionGoalsClient,
    LiveNotionNotesClient,
    LiveSlackClient,
)
from app.ingest.live_source_factory import calendar_client_for, google_docs_client_for
from app.ingest.seed import seed_live
from app.pipeline.pulse import _fetch_title, _fetch_url
from app.salience.pulse.pulse_context import build_pulse_context
from app.sub_agents.dossier.pull import pull_dossier
from app.sub_agents.friday_review.pull import pull_friday_review
from app.triggers.dispatch import handle_trigger
from app.triggers.pulse_trigger import build_pull_trigger

# /mentor prep has no T-15 gate (unlike app/triggers/dossier_scheduler.py's
# push path) — it's on-demand, so it looks further ahead for "the next
# qualifying event," matching app/intents/prep-meeting.md's own frontmatter
# default: "next qualifying event in the next 8h".
PREP_LOOKAHEAD_HOURS = 8

logger = logging.getLogger(__name__)

# Bounded, process-lifetime only (same "resets on restart, acceptable"
# pattern as app/triggers/slack_socket_listener.py's _seen_event_ids) —
# tracks which pulse card (keyed by dm_thread_ts, the card's own ack
# message ts) already has a full-shortlist reply posted, and that reply's
# own (channel, ts), so a repeat "See all N items" click can highlight the
# existing reply instead of posting a duplicate.
_MAX_TRACKED_SHORTLIST_REPLIES = 500
_shortlist_reply_location: dict[str, tuple[str, str]] = {}


def _remember_shortlist_reply(dm_thread_ts: str, channel: str, ts: str) -> None:
    if len(_shortlist_reply_location) >= _MAX_TRACKED_SHORTLIST_REPLIES:
        _shortlist_reply_location.pop(next(iter(_shortlist_reply_location)))
    _shortlist_reply_location[dm_thread_ts] = (channel, ts)


@dataclasses.dataclass(frozen=True)
class SlashCommandPayload:
    command: str
    text: str
    slack_user_id: str
    channel_id: str


def parse_slash_command(form: dict) -> SlashCommandPayload:
    return SlashCommandPayload(
        command=form.get("command", ""),
        text=(form.get("text") or "").strip(),
        slack_user_id=form.get("user_id", ""),
        channel_id=form.get("channel_id", ""),
    )


def resolve_owner_user_id(session: Session, slack_user_id: str) -> str | None:
    return session.execute(
        select(User.id).where(User.slack_user_id == slack_user_id)
    ).scalar_one_or_none()


def run_pulse_command(
    session: Session,
    owner_user_id: str,
    channel_id: str | None,
    clock: Clock | None = None,
    thread_ts: str | None = None,
    dm_thread_ts: str | None = None,
) -> None:
    """The actual pulse work, run after Slack has already been acked.
    Failures are logged, not raised — there is no request left to fail by
    the time this runs."""
    clock = clock or SystemClock()
    command_started = time.perf_counter()
    try:
        seed_live(session, owner_user_id, clock=clock)
    except Exception:
        logger.exception(
            "slack /mentor pulse: seed_live failed for owner=%s, pulsing "
            "off whatever is already ingested",
            owner_user_id,
        )
    logger.info(
        "slack /mentor pulse: seed_live phase done latency_ms=%.1f owner=%s",
        (time.perf_counter() - command_started) * 1000,
        owner_user_id,
    )

    try:
        scope = OwnerScope(owner_user_id=owner_user_id, session=session)
        user = session.get(User, owner_user_id)
        source_clients = [
            calendar_client_for(session, owner_user_id),
            LiveSlackClient(
                viewer_slack_user_id=user.slack_user_id if user else None
            ),
            LiveLinearClient(),
            LiveJiraClient(),
            google_docs_client_for(session, owner_user_id),
        ]
        event = build_pull_trigger(owner_user_id, owner_user_id, clock.now())
        trigger_started = time.perf_counter()
        trigger_result = handle_trigger(scope, clock, source_clients, event)
        logger.info(
            "slack /mentor pulse: handle_trigger phase done latency_ms=%.1f owner=%s",
            (time.perf_counter() - trigger_started) * 1000,
            owner_user_id,
        )

        deliverer = SlackDeliverer()
        if not deliverer.enabled:
            logger.warning(
                "slack /mentor pulse: SLACK_BOT_TOKEN not configured, "
                "owner=%s got no card",
                owner_user_id,
            )
            return

        card = build_card(trigger_result)
        deliver_started = time.perf_counter()
        delivery = deliverer.deliver(
            user.slack_user_id,
            blocks=render_blocks(card),
            channel_id=channel_id,
            thread_ts=thread_ts,
            dm_thread_ts=dm_thread_ts,
        )
        logger.info(
            "slack /mentor pulse: deliver phase done latency_ms=%.1f total_latency_ms=%.1f owner=%s",
            (time.perf_counter() - deliver_started) * 1000,
            (time.perf_counter() - command_started) * 1000,
            owner_user_id,
        )
        if delivery.get("sent") and delivery.get("dm_ts") and delivery.get("dm_channel"):
            deliver_pulse_audio(
                deliverer, card, delivery["dm_channel"], delivery["dm_ts"]
            )
    except Exception:
        logger.exception(
            "slack /mentor pulse: failed to render/deliver for owner=%s",
            owner_user_id,
        )


def _parse_starts_at_for_sort(starts_at: str) -> datetime.datetime:
    """Sort key for run_prep_command's event list. LiveCalendarClient can
    return either a timed dateTime string or an all-day date-only string
    (app/ingest/live_source.py's _adapt_calendar_event) — sorting the raw
    strings lexicographically only happens to match chronological order
    for same-format timed events, not once an all-day event's bare date
    string is mixed in. Parse to a real datetime instead, normalizing a
    naive result to UTC (matches dossier_scheduler.py's _parse_starts_at
    treatment of the same ambiguity) so every key is comparable."""
    parsed = datetime.datetime.fromisoformat(starts_at)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.UTC)
    return parsed


def run_prep_command(
    session: Session,
    owner_user_id: str,
    clock: Clock | None = None,
) -> None:
    """The `/mentor prep` work, run after Slack has already been acked
    (same 3s-window constraint as run_pulse_command). Fetches the caller's
    soonest upcoming calendar event in the next PREP_LOOKAHEAD_HOURS (no
    T-15 gate — pull is on-demand, unlike app/triggers/dossier_scheduler.py's
    push path) and pulls a dossier for it via pull_dossier, which enters at
    gather directly and always ships a card regardless of salience (spec
    §2: "the gate decides whether to push, never whether the dossier can
    be pulled").

    channel_id is deliberately not threaded through to pull_dossier/
    deliver_dossier: a dossier is private by construction
    (app/intents/prep-meeting.md's own Rules) — it only ever goes to the
    owner's DM, the same way app/sub_agents/dossier/sub_agents/deliver/
    agent.py's deliver_dossier already calls deliverer.deliver(slack_user_id,
    blocks) with no channel_id, unlike run_pulse_command's card.

    Failures are logged, not raised — there is no request left to fail by
    the time this runs (same contract as run_pulse_command)."""
    clock = clock or SystemClock()
    command_started = time.perf_counter()
    try:
        user = session.get(User, owner_user_id)
        if user is None:
            logger.warning(
                "slack /mentor prep: no User row for owner=%s", owner_user_id
            )
            return

        scope = OwnerScope(owner_user_id=owner_user_id, session=session)
        now = clock.now()
        window = Window(
            start=now, end=now + datetime.timedelta(hours=PREP_LOOKAHEAD_HOURS)
        )
        events = calendar_client_for(session, owner_user_id).fetch(window, owner_user_id)
        events = sorted(
            (e for e in events if e.get("starts_at")),
            key=lambda e: _parse_starts_at_for_sort(e["starts_at"]),
        )

        deliverer = SlackDeliverer()
        if not deliverer.enabled:
            logger.warning(
                "slack /mentor prep: SLACK_BOT_TOKEN not configured, "
                "owner=%s got no card",
                owner_user_id,
            )
            return

        if not events:
            # app/intents/prep-meeting.md's own edge-case table: "No
            # matching event -> Say so ... Do not brief on nothing."
            deliverer.send_text_message(
                user.slack_user_id,
                f"No upcoming event found in the next {PREP_LOOKAHEAD_HOURS}h to prep for.",
            )
            return

        event = events[0]
        connectors = {
            "slack": LiveSlackClient(viewer_slack_user_id=user.slack_user_id),
            "linear": LiveLinearClient(),
            "notion_goals": LiveNotionGoalsClient(
                notion_owner_email=user.notion_owner_email
            ),
            "notion_notes": LiveNotionNotesClient(
                notion_owner_email=user.notion_owner_email
            ),
        }
        pull_dossier(event, scope, clock, connectors, deliverer=deliverer)
        logger.info(
            "slack /mentor prep: done latency_ms=%.1f owner=%s",
            (time.perf_counter() - command_started) * 1000,
            owner_user_id,
        )
    except Exception:
        logger.exception(
            "slack /mentor prep: failed to gather/synthesize/deliver for owner=%s",
            owner_user_id,
        )


def run_review_command(
    session: Session,
    owner_user_id: str,
    clock: Clock | None = None,
) -> None:
    """The `/mentor review` work, run after Slack has already been acked
    (same 3s-window constraint as run_prep_command). Private by
    construction — pull_friday_review never threads a channel_id through,
    same reasoning run_prep_command's own docstring gives for dossiers."""
    clock = clock or SystemClock()
    try:
        user = session.get(User, owner_user_id)
        if user is None:
            logger.warning(
                "slack /mentor review: no User row for owner=%s", owner_user_id
            )
            return

        scope = OwnerScope(owner_user_id=owner_user_id, session=session)
        deliverer = SlackDeliverer()
        if not deliverer.enabled:
            logger.warning(
                "slack /mentor review: SLACK_BOT_TOKEN not configured, "
                "owner=%s got no card",
                owner_user_id,
            )
            return

        pull_friday_review(scope, clock, deliverer=deliverer)
    except Exception:
        logger.exception(
            "slack /mentor review: failed to gather/synthesize/deliver for owner=%s",
            owner_user_id,
        )


def run_shortlist_command(
    session: Session,
    owner_user_id: str,
    dm_thread_ts: str | None = None,
    clock: Clock | None = None,
) -> None:
    """The pulse card's "See all N items" button handler — posts the full
    pre-cut L5 shortlist as a threaded follow-up under the card. Read-only:
    build_pulse_context only calls .source/.health() on source_clients for
    degraded-source reporting, never .fetch() (that happens once, in
    seed_live, when the pulse itself was requested) — so this never
    re-ingests and never touches handle_trigger/PulseDelivery idempotency,
    same approach app/cli.py's `shortlist` command uses against a fixture.

    A repeat click for the same card (same dm_thread_ts) doesn't repost
    the list — it adds a reaction to the reply already posted the first
    time, so clicking twice doesn't spam the thread with duplicates.

    Failures are logged, not raised — there is no request left to fail by
    the time this runs (same contract as run_pulse_command)."""
    clock = clock or SystemClock()
    try:
        deliverer = SlackDeliverer()
        if not deliverer.enabled:
            logger.warning(
                "slack shortlist: SLACK_BOT_TOKEN not configured, owner=%s got nothing",
                owner_user_id,
            )
            return

        user = session.get(User, owner_user_id)

        if dm_thread_ts is not None and dm_thread_ts in _shortlist_reply_location:
            channel, ts = _shortlist_reply_location[dm_thread_ts]
            deliverer.add_reaction(channel, ts, "eyes")
            return

        scope = OwnerScope(owner_user_id=owner_user_id, session=session)
        source_clients = [
            calendar_client_for(session, owner_user_id),
            LiveSlackClient(),
            LiveLinearClient(),
            LiveJiraClient(),
            google_docs_client_for(session, owner_user_id),
        ]
        context = build_pulse_context(
            scope,
            clock,
            source_clients,
            trigger="pull",
            requested_at=clock.now(),
            # shortlist_limit defaults to None (uncapped) now — the whole
            # pipeline is uncapped, not just this "full shortlist" view.
        )

        titles = {item.item_id: _fetch_title(scope, item) for item in context.shortlist}
        urls = {item.item_id: _fetch_url(scope, item) for item in context.shortlist}
        blocks = render_shortlist_blocks(context.shortlist, titles, urls)

        result = deliverer.post_thread_reply(
            user.slack_user_id, blocks, "Your full shortlist", thread_ts=dm_thread_ts
        )
        if dm_thread_ts is not None and result.get("sent") and result.get("channel"):
            _remember_shortlist_reply(dm_thread_ts, result["channel"], result["ts"])
    except Exception:
        logger.exception(
            "slack shortlist: failed to render/deliver for owner=%s", owner_user_id
        )
