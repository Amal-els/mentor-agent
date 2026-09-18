"""T-15-before-event trigger for F1 (pre-meeting dossier). Templated on
agenda_scheduler.py's poll loop (spec §3): a separate process/loop from
agenda's meeting-END poller, own whitelist env var, own dedup deque.

calendar_client_factory is now per-owner (session, report_user_id) ->
client, not the old zero-arg shape — the previous single shared client
returned one account's calendar to every whitelisted report_user_id
regardless of who they are. Default is app.ingest.live_source_factory's
calendar_client_for, which uses a report_user_id's own connected Google
credential when they've completed the self-service connect flow
(app.triggers.google_oauth_router) and falls back to the legacy single
shared GOOGLE_OAUTH_CREDENTIALS account otherwise — the same fallback
every other rewired call site uses, not a behavior change for anyone who
hasn't connected."""

import collections
import datetime
import logging
import os
import time
from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session

from app.core.clock import Clock, SystemClock
from app.core.config import get_settings
from app.core.db import get_engine, get_session_factory
from app.ingest.base import Window
from app.ingest.live_source_factory import calendar_client_for

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 60
T_MINUS_WINDOW_SECONDS = 900  # 15 minutes
_seen_event_ids: collections.deque = collections.deque(maxlen=2000)


def _get_dossier_scheduled_report_user_ids() -> list[str]:
    raw = os.environ.get("DOSSIER_SCHEDULED_REPORT_USER_IDS", "")
    if not raw:
        raise RuntimeError(
            "DOSSIER_SCHEDULED_REPORT_USER_IDS must be set to run the dossier scheduler"
        )
    return [part.strip() for part in raw.split(",") if part.strip()]


def _parse_starts_at(starts_at: str | None) -> datetime.datetime | None:
    if starts_at is None:
        return None
    parsed = datetime.datetime.fromisoformat(starts_at)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.UTC)
    return parsed


def _is_t_minus_15(starts_at: str | None, now: datetime.datetime) -> bool:
    parsed = _parse_starts_at(starts_at)
    if parsed is None:
        return False
    delta = (parsed - now).total_seconds()
    return 0 <= delta <= T_MINUS_WINDOW_SECONDS


def poll_dossier_window_once(
    session_factory: Callable[[], Any],
    report_user_ids: list[str],
    clock: Clock | None = None,
    calendar_client_factory: Callable[[Session, str], Any] = calendar_client_for,
    on_qualifying_event: Callable[[str, dict], None] | None = None,
) -> None:
    clock = clock or SystemClock()
    now = clock.now()
    window = Window(start=now, end=now + datetime.timedelta(minutes=15))

    for report_user_id in report_user_ids:
        session = session_factory()
        try:
            client = calendar_client_factory(session, report_user_id)
        finally:
            session.close()
        events = client.fetch(window, report_user_id)
        for event in events:
            external_id = event.get("external_id")
            if external_id is None:
                continue
            dedup_key = (report_user_id, external_id)
            if dedup_key in _seen_event_ids:
                continue
            if not _is_t_minus_15(event.get("starts_at"), now):
                continue
            if on_qualifying_event is not None:
                try:
                    on_qualifying_event(report_user_id, event)
                except Exception:
                    # A transient failure (e.g. synthesize_dossier's bare
                    # state[OUTPUT_KEY] lookup on a flaky LLM call) must not
                    # abort the whole poll call — every other qualifying
                    # event/owner in this cycle still needs to run. Also:
                    # do NOT mark this event "seen" on failure (see below)
                    # so it gets retried on the next 60s poll instead of
                    # being silently dropped forever.
                    logger.exception(
                        "dossier scheduler: on_qualifying_event failed for "
                        "report_user_id=%s event=%s, will retry next poll",
                        report_user_id,
                        external_id,
                    )
                    continue
            _seen_event_ids.append(dedup_key)


def main() -> None:
    from dotenv import load_dotenv

    from app.core.models import User
    from app.core.scope import OwnerScope
    from app.ingest.live_source import (
        LiveLinearClient,
        LiveNotionGoalsClient,
        LiveNotionNotesClient,
        LiveSlackClient,
    )
    from app.sub_agents.dossier.agent import run_dossier_flow

    load_dotenv()
    report_user_ids = _get_dossier_scheduled_report_user_ids()
    engine = get_engine(get_settings().database_url)
    session_factory = get_session_factory(engine)

    def _on_qualifying_event(report_user_id: str, event: dict) -> None:
        session = session_factory()
        try:
            scope = OwnerScope(owner_user_id=report_user_id, session=session)
            user = session.get(User, report_user_id)
            notion_owner_email = user.notion_owner_email if user else None
            connectors = {
                "slack": LiveSlackClient(),
                "linear": LiveLinearClient(),
                "notion_goals": LiveNotionGoalsClient(
                    notion_owner_email=notion_owner_email
                ),
                "notion_notes": LiveNotionNotesClient(
                    notion_owner_email=notion_owner_email
                ),
            }
            run_dossier_flow(event, scope, SystemClock(), connectors)
        finally:
            session.close()

    while True:
        try:
            poll_dossier_window_once(
                session_factory,
                report_user_ids,
                on_qualifying_event=_on_qualifying_event,
            )
        except Exception:
            logger.exception(
                "dossier scheduler: poll failed, will retry next interval"
            )
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()