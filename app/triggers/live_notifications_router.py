"""Live "agent bubble" push channel for the /ui dashboard: an SSE stream
that tells the browser when a morning pulse, dossier, or Friday review has
just landed for this owner (so the popup can appear without the user
refreshing), plus three on-demand audio endpoints the bubble's Play button
hits.

Also carries the "something changed, go refetch" signals the main
dashboard panels (agenda, goals, notes, checklist) listen for — REAL
CHANGE (requested: "why isn't the ui in sync ... it doesn't happen real
time"). Those panels used to rely purely on app.js's own setInterval
timers (4s for the agenda, a full 20s for goals/notes/dossiers/checklist)
— worst case, a 20s-stale dashboard even though the server-side data was
already current. This relay polls the DB every POLL_INTERVAL_SECONDS
(same as the bubble events above) and pushes a lightweight *_changed
event the instant it sees something new, so the browser's own refetch
fires immediately instead of waiting out its timer. The setInterval
timers in app.js are NOT removed — they stay as a fallback safety net for
a dropped/reconnecting SSE connection (see EventSource.onerror's own
comment there); this relay just makes the common case feel push-driven
instead of "wait for the next poll."

Query-param auth, not the header-based X-Agenda-Token/X-Acting-User-Secret
every other webhook route uses (app/triggers/agenda_router.py's
_token_is_valid/_acting_user_secret_is_valid): browser EventSource and
<audio src="..."> both issue plain GETs with no way to set custom
headers, so the same two credentials travel as query params here instead.
Same values, same verification (hmac.compare_digest via
verify_shared_token / a direct compare against the user's own
agenda_client_secret) — just a different transport, required by the two
browser APIs this router serves, not a weaker check.

Polling, not a real cross-process event bus: app.triggers.dossier_
scheduler / friday_review_scheduler run as separate OS processes from
this FastAPI app (own `while True: poll; sleep()` loops, per their own
module docstrings), so an in-memory Python callback from this process
can't observe their writes directly. This endpoint instead polls
DossierDelivery/FridayReviewDelivery itself, every few seconds, for rows
newer than when the SSE connection opened, and re-publishes those to the
browser over SSE — the same "poll the DB" pattern every trigger module in
this codebase already uses, just relayed to the browser instead of Slack."""

import asyncio
import datetime
import hmac
import json
import logging

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import or_, select

from app.agenda.models import AgendaItem, AgendaItemHistory
from app.agenda.scope import get_active_pairs_for
from app.core.config import get_settings
from app.core.db import get_engine, get_session_factory
from app.core.models import (
    ChecklistCompletion,
    DossierDelivery,
    FridayReviewDelivery,
    Goal,
    OneOnOneNote,
    PulseDelivery,
    User,
)
from app.delivery.tts import synthesize_speech
from app.triggers.webhooks.webhook_signature import verify_shared_token

logger = logging.getLogger(__name__)

router = APIRouter()

POLL_INTERVAL_SECONDS = 3


def _open_session():
    return get_session_factory(get_engine(get_settings().database_url))()


def _token_is_valid_qs(settings, request: Request) -> bool:
    return bool(settings.agenda_webhook_token) and verify_shared_token(
        settings.agenda_webhook_token, request.query_params.get("token")
    )


def _acting_user_secret_is_valid_qs(session, owner_user_id: str, request: Request) -> bool:
    provided = request.query_params.get("secret")
    if provided is None:
        return False
    user = session.get(User, owner_user_id)
    if user is None or user.agenda_client_secret is None:
        return False
    return hmac.compare_digest(user.agenda_client_secret, provided)


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _poll_once_sync(owner_user_id: str, since: datetime.datetime) -> list[tuple[str, dict]]:
    """The blocking DB half of one poll cycle — deliberately plain sync
    code, run off the event loop via run_in_threadpool by the caller.
    A long-lived SSE generator awaiting asyncio.sleep() then calling
    blocking session.execute() *directly* holds the single asyncio event
    loop hostage for however long that query takes, every
    POLL_INTERVAL_SECONDS, for the entire lifetime of the connection —
    found live: with the shared DB under real concurrent load, this
    froze the *entire server* (every other request, not just this one's
    own poll) for several seconds at a time, and the same stalls were
    why notifications themselves arrived late. run_in_threadpool moves
    this work to a worker thread so the event loop stays free to serve
    other requests while it runs."""
    events: list[tuple[str, dict]] = []
    session = _open_session()
    try:
        # Every report_user_id this owner has any real reason to see
        # agenda/goals/notes for: themselves (as a report), plus every
        # current report they manage — get_active_pairs_for is the same
        # helper app.triggers.slack.slack_socket_listener already uses for this
        # exact "which report_user_id(s) does this owner concern" question
        # (see its own docstring). A manager's dashboard can have a report
        # switched in-tab (commit b410bab) without reopening this SSE
        # connection, so this deliberately covers the whole team rather
        # than trying to track which single report_user_id is currently
        # selected — an extra refetch for a tab that isn't open is free;
        # a missed one is a stale dashboard, the exact bug being fixed.
        relevant_report_user_ids = {
            pair.report_user_id for pair in get_active_pairs_for(session, owner_user_id)
        } or {owner_user_id}

        agenda_changed = session.execute(
            select(AgendaItemHistory.id)
            .join(AgendaItem, AgendaItem.id == AgendaItemHistory.agenda_item_id)
            .where(
                AgendaItem.report_user_id.in_(relevant_report_user_ids),
                AgendaItemHistory.changed_at > since,
            )
            .limit(1)
        ).first()
        if agenda_changed is not None:
            events.append(("agenda_changed", {}))

        goals_changed = session.execute(
            select(Goal.id)
            .where(
                Goal.owner_user_id.in_(relevant_report_user_ids),
                # updated_at is only ever set on an in-place edit
                # (Goal.updated_at's own docstring) — OR created_at
                # so a brand-new Key Result (never edited, updated_at
                # still NULL) is caught too.
                or_(Goal.updated_at > since, Goal.created_at > since),
            )
            .limit(1)
        ).first()
        if goals_changed is not None:
            events.append(("goals_changed", {}))

        notes_changed = session.execute(
            select(OneOnOneNote.id)
            .where(
                OneOnOneNote.owner_user_id.in_(relevant_report_user_ids),
                OneOnOneNote.created_at > since,
            )
            .limit(1)
        ).first()
        if notes_changed is not None:
            events.append(("notes_changed", {}))

        checklist_changed = session.execute(
            select(ChecklistCompletion.id)
            .where(
                # Self only, not the whole team — a checklist completion
                # is the acting user's own personal progress tracking
                # (app/core/models.py's ChecklistCompletion docstring),
                # not something a manager watches on a report's behalf,
                # matching fetchChecklist's own owner_user_id=self query.
                ChecklistCompletion.owner_user_id == owner_user_id,
                ChecklistCompletion.completed_at > since,
            )
            .limit(1)
        ).first()
        if checklist_changed is not None:
            events.append(("checklist_changed", {}))

        new_dossiers = (
            session.execute(
                select(DossierDelivery).where(
                    DossierDelivery.owner_user_id == owner_user_id,
                    DossierDelivery.sent_at.is_not(None),
                    DossierDelivery.sent_at > since,
                )
            )
            .scalars()
            .all()
        )
        for d in new_dossiers:
            events.append(
                ("dossier_ready", {"id": d.id, "who": d.who_summary, "why_now": d.why_now})
            )

        new_reviews = (
            session.execute(
                select(FridayReviewDelivery).where(
                    FridayReviewDelivery.owner_user_id == owner_user_id,
                    FridayReviewDelivery.sent_at.is_not(None),
                    FridayReviewDelivery.sent_at > since,
                )
            )
            .scalars()
            .all()
        )
        for r in new_reviews:
            events.append(
                (
                    "friday_review_ready",
                    {
                        "id": r.id,
                        "week_start_date": r.week_start_date.isoformat(),
                        "proposed_item_count": len(r.proposed_ledger_items or []),
                    },
                )
            )

        new_pulses = (
            session.execute(
                select(PulseDelivery).where(
                    PulseDelivery.owner_user_id == owner_user_id,
                    PulseDelivery.ritual == "pulse",
                    PulseDelivery.delivered_at > since,
                )
            )
            .scalars()
            .all()
        )
        for p in new_pulses:
            events.append(
                (
                    "pulse_ready",
                    {
                        "id": p.id,
                        "local_date": p.local_date.isoformat(),
                        "item_count": len(p.item_ids or []),
                        # app/delivery/cards.py's card_to_dict shape,
                        # denormalized onto the row at write time
                        # (PulseDelivery.card_json's own docstring) — the
                        # real Focus items/why-now/action text, not just a
                        # count. {} for any delivery written before this
                        # column existed; the browser falls back to the
                        # count-only bubble when this is empty.
                        "card": p.card_json or {},
                    },
                )
            )
    finally:
        session.close()
    return events


def _managed_report_ids_sync(owner_user_id: str) -> frozenset[str]:
    """Every report_user_id owner_user_id currently manages, as a plain
    set — no changed-since watermark exists for this the way the other
    *_changed checks use one (Pair carries no created_at/updated_at
    column at all), so managed_reports_changed below tracks this set
    per-connection and diffs it directly instead."""
    session = _open_session()
    try:
        return frozenset(
            pair.report_user_id
            for pair in get_active_pairs_for(session, owner_user_id)
            if pair.manager_user_id == owner_user_id
        )
    finally:
        session.close()


async def _notification_stream(owner_user_id: str):
    """Yields an SSE event each time a new dossier, pulse, or Friday
    review lands for this owner. Runs entirely in-memory per connection —
    a page reload just starts listening from "now" again, no missed-event
    replay (acceptable for a live "heads up" popup, not a durable inbox)."""
    since = datetime.datetime.now(datetime.UTC)
    # REAL CHANGE (requested: fix the gap where a newly-synced Pair only
    # ever showed up in the report picker on the manager's NEXT login).
    # Captured once, before the loop starts, so a manager who already had
    # reports when this connection opened doesn't get a spurious changed
    # event on the very first poll.
    known_managed_report_ids = await run_in_threadpool(
        _managed_report_ids_sync, owner_user_id
    )
    yield ": connected\n\n"  # SSE comment line, opens the stream immediately

    while True:
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
        try:
            events = await run_in_threadpool(_poll_once_sync, owner_user_id, since)
        except Exception:
            logger.exception(
                "live notifications: poll failed for owner=%s, will retry", owner_user_id
            )
            events = []
        for event_name, data in events:
            yield _sse(event_name, data)
        since = datetime.datetime.now(datetime.UTC)

        try:
            current_managed_report_ids = await run_in_threadpool(
                _managed_report_ids_sync, owner_user_id
            )
        except Exception:
            logger.exception(
                "live notifications: managed-reports poll failed for owner=%s, "
                "will retry",
                owner_user_id,
            )
            continue
        if current_managed_report_ids != known_managed_report_ids:
            known_managed_report_ids = current_managed_report_ids
            yield _sse("managed_reports_changed", {})


@router.get("/webhooks/notifications")
async def notifications_stream(request: Request) -> StreamingResponse:
    settings = get_settings()
    owner_user_id = request.query_params.get("owner_user_id")
    if owner_user_id is None or not _token_is_valid_qs(settings, request):
        return StreamingResponse(iter(()), status_code=401)

    def _check_secret() -> bool:
        session = _open_session()
        try:
            return _acting_user_secret_is_valid_qs(session, owner_user_id, request)
        finally:
            session.close()

    if not await run_in_threadpool(_check_secret):
        return StreamingResponse(iter(()), status_code=401)

    return StreamingResponse(
        _notification_stream(owner_user_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _dossier_speech_text(delivery: DossierDelivery) -> str:
    """Deterministic, not the full LLM narrator (draft_dossier_audio_script)
    — DossierDelivery only persists who_summary/why_now, not the full
    DossierCard (talking points live only transiently in the synthesize->
    deliver handoff), so there isn't enough stored content here to
    re-run the real narrator faithfully. Good enough for a click-to-play
    "hear it" affordance; not a replacement for the audio companion
    already threaded under the real Slack card."""
    if not delivery.who_summary and not delivery.why_now:
        return "Nothing to prep for this one — no real signal to brief you on."
    parts = [f"Here's your prep. You're meeting with {delivery.who_summary}."]
    if delivery.why_now:
        parts.append(delivery.why_now)
    return " ".join(parts)


def _friday_review_speech_text(delivery: FridayReviewDelivery) -> str:
    items = delivery.proposed_ledger_items or []
    if not items:
        return "It was a quiet week — not much to report, but here's what's real."
    descriptions = "; ".join(item.get("description", "") for item in items)
    return f"Here's your Friday reflection. This week: {descriptions}."


def _dossier_audio_sync(owner_user_id: str, delivery_id: str, secret: str | None) -> tuple[int, bytes | None]:
    """Blocking DB + TTS network call, run off the event loop via
    run_in_threadpool by the caller — same reasoning as _poll_once_sync's
    own docstring: a real synthesize_speech call can take a couple of
    seconds, and calling it directly inside an async route blocks every
    other request on the server for that whole window."""
    session = _open_session()
    try:
        user = session.get(User, owner_user_id)
        if user is None or user.agenda_client_secret is None or secret is None or not hmac.compare_digest(user.agenda_client_secret, secret):
            return 401, None
        delivery = session.execute(
            select(DossierDelivery).where(
                DossierDelivery.id == delivery_id,
                DossierDelivery.owner_user_id == owner_user_id,
            )
        ).scalar_one_or_none()
        if delivery is None:
            return 404, None
        audio = synthesize_speech(_dossier_speech_text(delivery))
        if audio is None:
            return 503, None
        return 200, audio
    finally:
        session.close()


@router.get("/webhooks/dossier-audio")
async def dossier_audio(request: Request) -> Response:
    settings = get_settings()
    owner_user_id = request.query_params.get("owner_user_id")
    delivery_id = request.query_params.get("delivery_id")
    if owner_user_id is None or delivery_id is None or not _token_is_valid_qs(
        settings, request
    ):
        return Response(status_code=401)

    status_code, audio = await run_in_threadpool(
        _dossier_audio_sync, owner_user_id, delivery_id, request.query_params.get("secret")
    )
    if status_code != 200:
        return Response(status_code=status_code)
    return Response(content=audio, media_type="audio/mpeg")


def _pulse_speech_text(delivery: PulseDelivery) -> str:
    """Deterministic, same limitation as _dossier_speech_text: PulseDelivery
    only persists item_ids + a context_hash, not the rendered PulseCard
    (title/why_now/action per item live only transiently in run_pulse's
    return value), so there isn't enough stored content to re-run the real
    narrator (build_pulse_speech_script/draft_audio_script) faithfully
    here. A count-only "heads up" line, not a substitute for the real
    audio companion already threaded under the Slack card."""
    count = len(delivery.item_ids or [])
    if count == 0:
        return "Good morning. Nothing cleared the bar today."
    count_word = "one thing" if count == 1 else f"{count} things"
    return f"Good morning. Your pulse is ready — you've got {count_word} today."


def _pulse_audio_sync(owner_user_id: str, delivery_id: str, secret: str | None) -> tuple[int, bytes | None]:
    session = _open_session()
    try:
        user = session.get(User, owner_user_id)
        if user is None or user.agenda_client_secret is None or secret is None or not hmac.compare_digest(user.agenda_client_secret, secret):
            return 401, None
        delivery = session.execute(
            select(PulseDelivery).where(
                PulseDelivery.id == delivery_id,
                PulseDelivery.owner_user_id == owner_user_id,
            )
        ).scalar_one_or_none()
        if delivery is None:
            return 404, None
        audio = synthesize_speech(_pulse_speech_text(delivery))
        if audio is None:
            return 503, None
        return 200, audio
    finally:
        session.close()


@router.get("/webhooks/pulse-audio")
async def pulse_audio(request: Request) -> Response:
    settings = get_settings()
    owner_user_id = request.query_params.get("owner_user_id")
    delivery_id = request.query_params.get("delivery_id")
    if owner_user_id is None or delivery_id is None or not _token_is_valid_qs(
        settings, request
    ):
        return Response(status_code=401)

    status_code, audio = await run_in_threadpool(
        _pulse_audio_sync, owner_user_id, delivery_id, request.query_params.get("secret")
    )
    if status_code != 200:
        return Response(status_code=status_code)
    return Response(content=audio, media_type="audio/mpeg")


def _friday_review_audio_sync(owner_user_id: str, delivery_id: str, secret: str | None) -> tuple[int, bytes | None]:
    session = _open_session()
    try:
        user = session.get(User, owner_user_id)
        if user is None or user.agenda_client_secret is None or secret is None or not hmac.compare_digest(user.agenda_client_secret, secret):
            return 401, None
        delivery = session.execute(
            select(FridayReviewDelivery).where(
                FridayReviewDelivery.id == delivery_id,
                FridayReviewDelivery.owner_user_id == owner_user_id,
            )
        ).scalar_one_or_none()
        if delivery is None:
            return 404, None
        audio = synthesize_speech(_friday_review_speech_text(delivery))
        if audio is None:
            return 503, None
        return 200, audio
    finally:
        session.close()


@router.get("/webhooks/friday-review-audio")
async def friday_review_audio(request: Request) -> Response:
    settings = get_settings()
    owner_user_id = request.query_params.get("owner_user_id")
    delivery_id = request.query_params.get("delivery_id")
    if owner_user_id is None or delivery_id is None or not _token_is_valid_qs(
        settings, request
    ):
        return Response(status_code=401)

    status_code, audio = await run_in_threadpool(
        _friday_review_audio_sync, owner_user_id, delivery_id, request.query_params.get("secret")
    )
    if status_code != 200:
        return Response(status_code=status_code)
    return Response(content=audio, media_type="audio/mpeg")
