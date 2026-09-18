"""@tool functions exposed to root_agent (AGENT.md's app/tools layout).
Thin wrappers only — validate input, call one layer function, return a
typed result; no business logic here. Plain-python function tools (as
opposed to app/tools/mcp_config.py's MCP-backed connector specs)."""

import datetime
import logging

from google.adk.tools import ToolContext

from app.core.clock import Clock, SystemClock
from app.core.config import get_settings
from app.core.db import get_engine, get_session_factory
from app.core.models import User
from app.core.scope import OwnerScope
from app.delivery.cards import build_card, render_text
from app.ingest.base import Window
from app.ingest.live_source import (
    LiveJiraClient,
    LiveLinearClient,
    LiveSlackClient,
)
from app.ingest.live_source_factory import calendar_client_for, google_docs_client_for
from app.ingest.seed import seed_live
from app.triggers.dispatch import handle_trigger
from app.triggers.pulse_trigger import build_pull_trigger

logger = logging.getLogger(__name__)


def render_pulse_text_for_owner(owner_user_id: str) -> str:
    """The one layer call get_morning_pulse delegates to: seed_live ->
    build_pull_trigger -> handle_trigger -> render_text — the same path
    every other pulse trigger surface (CLI, Slack) already uses, so
    idempotency (PulseDelivery) and the "requested for someone else,
    always refuse" rule (decision 4) stay identical here too, nothing
    reimplemented for this one conversational surface. Unlike the Slack
    path there's no SlackDeliverer step — the rendered text is the
    tool's return value, relayed by whatever conversational surface
    called it."""
    engine = get_engine(get_settings().database_url)
    session = get_session_factory(engine)()
    try:
        user = session.get(User, owner_user_id)
        if user is None:
            return (
                f"No Mentor Agent user found for {owner_user_id!r} — this "
                "session's user_id must be an existing owner_user_id (e.g. "
                "from a prior `seed --fixture` or `seed --live` run)."
            )

        clock = SystemClock()
        try:
            seed_live(session, owner_user_id, clock=clock)
        except Exception:
            logger.exception(
                "get_morning_pulse: seed_live failed for owner=%s, pulsing "
                "off whatever is already ingested",
                owner_user_id,
            )

        scope = OwnerScope(owner_user_id=owner_user_id, session=session)
        source_clients = [
            calendar_client_for(session, owner_user_id),
            LiveSlackClient(viewer_slack_user_id=user.slack_user_id),
            LiveLinearClient(),
            LiveJiraClient(),
            google_docs_client_for(session, owner_user_id),
        ]
        event = build_pull_trigger(owner_user_id, owner_user_id, clock.now())
        trigger_result = handle_trigger(scope, clock, source_clients, event)
    finally:
        session.close()

    if trigger_result.idempotent_skip:
        return f"Already delivered for {trigger_result.local_date} today."

    card = build_card(trigger_result)
    return render_text(card)


# The stock `adk web` dev UI has no way to set a session's user_id from
# the browser — it hardcodes every session to this literal string. Mapping
# it to the app's single configured owner (same one app/triggers/
# webhook_router.py attributes every webhook event to — this app is
# single-tenant in practice, not a real multi-user delegation path) is
# what makes `adk web` usable for local testing at all; without it every
# session there would permanently 404 on "no user record found" no matter
# who's actually running it, since nothing in this codebase can ever
# create a real User row with id "user".
_ADK_DEV_UI_DEFAULT_USER_ID = "user"


def get_morning_pulse(tool_context: ToolContext) -> dict:
    """Gets the caller's morning pulse: today's ranked focus items, the
    day's events, and anything owed. Call this whenever the user asks for
    their pulse, briefing, or what's happening today — never for anyone
    else, there is no delegation mechanism.

    Returns:
        dict with a "pulse" key containing the rendered pulse text.
    """
    owner_user_id = tool_context.user_id
    if owner_user_id == _ADK_DEV_UI_DEFAULT_USER_ID:
        default_owner = get_settings().webhook_owner_user_id
        if default_owner:
            owner_user_id = default_owner
    text = render_pulse_text_for_owner(owner_user_id)
    return {"status": "success", "pulse": text}


DOSSIER_LOOKAHEAD_HOURS = 8  # matches app/triggers/slack_command.py's PREP_LOOKAHEAD_HOURS


def _parse_starts_at_for_sort(starts_at: str) -> datetime.datetime:
    parsed = datetime.datetime.fromisoformat(starts_at)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.UTC)
    return parsed


def render_dossier_text(card, event_title: str | None, event_starts_at: str | None) -> str:
    """Plain-text rendering of a DossierCard for the chat surface — same
    content build_dossier_card (app/delivery/cards.py) renders as Slack
    Block Kit, just as lines instead of blocks."""
    lines = []
    if event_title:
        lines.append(f"📋 Dossier — {event_title}")
    lines.append(f"Who: {', '.join(card.who)}")
    lines.append(f"Why now: {card.why_now}")
    if card.nothing_to_prep:
        lines.append("Nothing to prep.")
        return "\n".join(lines)
    if card.talking_points:
        lines.append("Talking points:")
        for p in card.talking_points:
            suffix = f" ({p.source_link})" if p.source_link else ""
            lines.append(f"  • {p.text}{suffix}")
    if card.promised_and_not_delivered:
        lines.append("Promised, not delivered:")
        for item in card.promised_and_not_delivered:
            lines.append(f"  • {item}")
    if card.suggested_opener:
        lines.append(f"Opener: {card.suggested_opener}")
    return "\n".join(lines)


def render_dossier_text_for_owner(owner_user_id: str, clock: Clock | None = None) -> str:
    """The one layer call get_pre_meeting_dossier delegates to. Mirrors
    render_pulse_text_for_owner's "compute fresh, render text, no Slack
    side effect" shape, but deliberately does NOT call pull_dossier/
    deliver_dossier: that path writes a DossierDelivery row, and that
    table's UNIQUE(owner_user_id, event_external_id) plus deliver_
    dossier's own check-then-return-existing dedup means a row created
    here (with no real Slack send, since this surface never sends to
    Slack) would silently shadow app/triggers/dossier_scheduler.py's real
    T-15 push for that same event forever after. Calls gather_dossier_
    context + synthesize_dossier directly instead — the same two steps
    pull_dossier itself calls, just stopping short of deliver_dossier."""
    from app.sub_agents.dossier.sub_agents.gather.agent import gather_dossier_context
    from app.sub_agents.dossier.sub_agents.synthesize.agent import (
        build_synthesize_agent,
        synthesize_dossier,
    )

    engine = get_engine(get_settings().database_url)
    session = get_session_factory(engine)()
    try:
        user = session.get(User, owner_user_id)
        if user is None:
            return (
                f"No Mentor Agent user found for {owner_user_id!r} — this "
                "session's user_id must be an existing owner_user_id."
            )

        clock = clock or SystemClock()
        now = clock.now()
        window = Window(
            start=now, end=now + datetime.timedelta(hours=DOSSIER_LOOKAHEAD_HOURS)
        )
        events = calendar_client_for(session, owner_user_id).fetch(window, owner_user_id)
        events = sorted(
            (e for e in events if e.get("starts_at")),
            key=lambda e: _parse_starts_at_for_sort(e["starts_at"]),
        )
        if not events:
            return (
                f"No upcoming event found in the next {DOSSIER_LOOKAHEAD_HOURS}h to prep for."
            )

        event = events[0]
        scope = OwnerScope(owner_user_id=owner_user_id, session=session)
        # No viewer_slack_user_id here — gather_dossier_context now does
        # its own, more specific attendee-based relevance filtering
        # (LiveSlackClient's viewer_slack_user_id would instead filter to
        # "mentions the dossier owner", a different and narrower question
        # than "about this meeting's attendees").
        connectors = {
            "slack": LiveSlackClient(),
            "linear": LiveLinearClient(),
        }
        context = gather_dossier_context(event, scope, clock, connectors)
        card = synthesize_dossier(context, build_synthesize_agent())
    finally:
        session.close()

    return render_dossier_text(card, event.get("title"), event.get("starts_at"))


def get_pre_meeting_dossier(tool_context: ToolContext) -> dict:
    """Gets a pre-meeting dossier for the caller's soonest upcoming
    calendar event: who they're meeting, why it matters now, sourced
    talking points, and open commitments. Call this whenever the user
    asks to be prepped for a meeting, briefed on who they're about to
    talk to, or what to bring up — never for anyone else, there is no
    delegation mechanism.

    Returns:
        dict with a "dossier" key containing the rendered dossier text.
    """
    owner_user_id = tool_context.user_id
    if owner_user_id == _ADK_DEV_UI_DEFAULT_USER_ID:
        default_owner = get_settings().webhook_owner_user_id
        if default_owner:
            owner_user_id = default_owner
    text = render_dossier_text_for_owner(owner_user_id)
    return {"status": "success", "dossier": text}


def render_friday_review_text(card) -> str:
    """Plain-text rendering of a FridayReviewCard — same content
    build_friday_review_card (app/delivery/cards.py) renders as Slack
    Block Kit, just as lines instead of blocks."""
    lines = [f"Friday Reflection — week of {card.week_start.isoformat()}"]
    if card.quiet_week:
        lines.append("Quiet week — not much to show, but here's what's real.")
    if card.wins:
        lines.append("Shipped:")
        for w in card.wins:
            suffix = f" ({w.source_link})" if w.source_link else ""
            goal = f" — moved {w.moved_goal_title}" if w.moved_goal_title else ""
            lines.append(f"  • {w.text}{suffix}{goal}")
    if card.slipped_lines:
        lines.append("Slipped:")
        for line in card.slipped_lines:
            lines.append(f"  • {line}")
    if card.one_adjustment and not card.quiet_week:
        lines.append(f"One adjustment: {card.one_adjustment}")
    if card.agenda_resolved_lines or card.agenda_stuck_lines:
        lines.append("Agenda 1-on-1s:")
        for line in card.agenda_resolved_lines:
            lines.append(f"  ✅ {line}")
        for line in card.agenda_stuck_lines:
            lines.append(f"  ⏳ {line} (worth raising directly)")
    if card.okr_progress_lines:
        lines.append("OKR progress:")
        for line in card.okr_progress_lines:
            lines.append(f"  • {line}")
    if card.career_narrative:
        lines.append(f"Toward your goal: {card.career_narrative}")
    skill_text = card.skill_distribution_summary or "Not enough categorized history yet."
    lines.append(f"Skill distribution: {skill_text}")
    return "\n".join(lines)


def render_friday_review_text_for_owner(owner_user_id: str, clock: Clock | None = None) -> str:
    """The one layer call get_friday_reflection delegates to. Same
    reasoning as render_dossier_text_for_owner: calls gather_friday_
    review_context + synthesize_friday_review directly rather than pull_
    friday_review/deliver_friday_review, so a chat request never writes a
    FridayReviewDelivery row a real /mentor review or the Friday-afternoon
    scheduler would then have to reconcile with."""
    from app.sub_agents.friday_review.sub_agents.gather.agent import (
        gather_friday_review_context,
    )
    from app.sub_agents.friday_review.sub_agents.synthesize.agent import (
        build_synthesize_agent as build_friday_review_synthesize_agent,
        synthesize_friday_review,
    )

    engine = get_engine(get_settings().database_url)
    session = get_session_factory(engine)()
    try:
        user = session.get(User, owner_user_id)
        if user is None:
            return (
                f"No Mentor Agent user found for {owner_user_id!r} — this "
                "session's user_id must be an existing owner_user_id."
            )

        clock = clock or SystemClock()
        scope = OwnerScope(owner_user_id=owner_user_id, session=session)
        context = gather_friday_review_context(scope, clock)
        card = synthesize_friday_review(context, build_friday_review_synthesize_agent())
    finally:
        session.close()

    return render_friday_review_text(card)


def get_friday_reflection(tool_context: ToolContext) -> dict:
    """Gets the caller's Friday reflection: what shipped this week, what
    moved a goal, what slipped, one concrete adjustment, and current
    skill/OKR progress. Call this whenever the user asks for their weekly
    review, Friday reflection, or how their week went — never for anyone
    else, there is no delegation mechanism.

    Returns:
        dict with a "reflection" key containing the rendered review text.
    """
    owner_user_id = tool_context.user_id
    if owner_user_id == _ADK_DEV_UI_DEFAULT_USER_ID:
        default_owner = get_settings().webhook_owner_user_id
        if default_owner:
            owner_user_id = default_owner
    text = render_friday_review_text_for_owner(owner_user_id)
    return {"status": "success", "reflection": text}
