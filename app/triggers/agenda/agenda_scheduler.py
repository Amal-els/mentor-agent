import collections
import datetime
import logging
import os
import time
from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session

from app.agenda.scope import resolve_pair_scope
from app.core.clock import Clock, SystemClock
from app.core.config import get_settings
from app.core.db import get_engine, get_session_factory
from app.core.scope import OwnerScope
from app.ingest.base import Healthy, Window
from app.ingest.google_credential_store import (
    is_google_connected,
    materialize_calendar_token_path,
)
from app.ingest.live_source import (
    LiveCalendarClient,
    LiveFathomClient,
    LiveNotionPairClient,
)
from app.ingest.notion_pair_sync import sync_notion_pair_edge
from app.ingest.notion_user_provision import (
    auto_provision_notion_users,
    revoke_departed_notion_users,
)
from app.sub_agents.agenda.agent import run_post_meeting_flow
from app.tools.mcp_config import McpSession, calendar_mcp_spec
from app.triggers.agenda.setup_router import issue_setup_link

logger = logging.getLogger(__name__)
ORG_DATA_SYNC_POLL_INTERVAL_SECONDS = 600
_ORG_DATA_DUMMY_OWNER_USER_ID = "org-data-sync"

def _org_data_window(clock: Clock) -> Window:
    now = clock.now()
    return Window(start=now, end=now)


def run_notion_pair_sync_once(session: Session, client, clock: Clock | None = None) -> int:
    clock = clock or SystemClock()
    edges = client.fetch(_org_data_window(clock), _ORG_DATA_DUMMY_OWNER_USER_ID)
    for raw in edges:
        sync_notion_pair_edge(session, raw, clock)
    return len(edges)


def run_notion_user_provisioning_once(
    session: Session,
    client: "LiveNotionPairClient",
    clock: Clock,
    base_url: str,
    default_tz: str = "UTC",
    default_pulse_fire_time_local: datetime.time = datetime.time(8, 30),
    default_late_cutoff_local: datetime.time = datetime.time(21, 0),
) -> int:
    directory = client.fetch_directory()
    created = auto_provision_notion_users(
        session,
        directory,
        clock,
        default_tz=default_tz,
        default_pulse_fire_time_local=default_pulse_fire_time_local,
        default_late_cutoff_local=default_late_cutoff_local,
    )
    if not created:
        return 0
    if not base_url:
        logger.warning(
            "agenda scheduler: auto-provisioned %s new user(s) but "
            "AGENDA_PUBLIC_BASE_URL is not set — no onboarding email "
            "sent; they can still be reached via `mentor link-agenda-"
            "client` or the UI's manual 'Request setup link'",
            len(created),
        )
        return len(created)
    for user in created:
        try:
            issue_setup_link(session, user, base_url)
        except Exception:
            logger.exception(
                "agenda scheduler: failed to send onboarding setup email "
                "to auto-provisioned user_id=%s",
                user.id,
            )
    return len(created)


def run_notion_member_revocation_once(
    session: Session, client: "LiveNotionPairClient", clock: Clock
) -> int:
    member_ids = client.fetch_active_member_ids()
    if member_ids is None:
        return 0
    revoked = revoke_departed_notion_users(session, member_ids, clock)
    if revoked:
        logger.info(
            "agenda scheduler: revoked access for %s user(s) no longer "
            "in the Notion workspace: %s",
            len(revoked),
            [u.id for u in revoked],
        )
    return len(revoked)

MEETING_POLL_INTERVAL_SECONDS = 60
CALENDAR_LOOKBACK_HOURS = 6
JUST_ENDED_WINDOW_SECONDS = 180
FATHOM_MATCH_WINDOW_SLACK_SECONDS = 900

_seen_meeting_ids: collections.deque = collections.deque(maxlen=2000)


def _parse_ends_at(ends_at: str | None) -> datetime.datetime | None:
    if not ends_at:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(ends_at)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.UTC)
    return parsed


def _is_just_ended(ends_at: str | None, now: datetime.datetime) -> bool:
    parsed = _parse_ends_at(ends_at)
    if parsed is None:
        return False
    delta_seconds = (now - parsed).total_seconds()
    return 0 <= delta_seconds <= JUST_ENDED_WINDOW_SECONDS


def _fathom_transcript_for_event(
    event: dict, fathom_client_factory=LiveFathomClient
) -> str | None:
    starts_at = event.get("starts_at")
    ends_at = event.get("ends_at")
    if not starts_at or not ends_at:
        return None
    try:
        start = datetime.datetime.fromisoformat(starts_at)
        end = datetime.datetime.fromisoformat(ends_at)
    except ValueError:
        return None
    slack = datetime.timedelta(seconds=FATHOM_MATCH_WINDOW_SLACK_SECONDS)
    window = Window(start=start - slack, end=end + slack)

    actor_reference_key = event.get("actor_reference_key", "")
    _source, _, rest = actor_reference_key.partition(":")
    organizer_email = rest if "@" in rest else None

    client = fathom_client_factory()
    if not isinstance(client.health(), Healthy):
        return None
    try:
        return client.find_transcript(window, organizer_email=organizer_email)
    except Exception:
        logger.exception(
            "agenda scheduler: Fathom transcript lookup failed for meeting_id=%s",
            event.get("external_id"),
        )
        return None


def _process_meeting_end(
    session_factory,
    report_user_id: str,
    event: dict,
    clock: Clock,
    fathom_client_factory=LiveFathomClient,
    trust_event_content: bool = True,
) -> bool:
    session = session_factory()
    try:
        pair_scope = resolve_pair_scope(session, report_user_id, report_user_id)
        if pair_scope is None:
            logger.warning(
                "agenda scheduler: no resolvable PairScope for "
                "report_user_id=%s (no active Pair row yet — see Job 1 / "
                "the `mentor link-notion` CLI command), skipping meeting_end",
                report_user_id,
            )
            return False
        owner_scope = OwnerScope(owner_user_id=report_user_id, session=session)
        transcript_text = event.get("description")
        if trust_event_content:
            transcript_text = (
                _fathom_transcript_for_event(
                    event, fathom_client_factory=fathom_client_factory
                )
                or transcript_text
            )
        run_post_meeting_flow(
            meeting_id=event.get("external_id"),
            pair_scope=pair_scope,
            owner_scope=owner_scope,
            clock=clock,
            transcript_text=transcript_text,
        )
        return True
    except Exception:
        logger.exception(
            "agenda scheduler: meeting_end flow failed report_user_id=%s "
            "meeting_id=%s",
            report_user_id,
            event.get("external_id"),
        )
        return False
    finally:
        session.close()

_LEGACY_SHARED_KEY = "__legacy_shared__"
_calendar_sessions: dict[str, McpSession] = {}


def _calendar_client_for_scheduler(session: Session, report_user_id: str) -> LiveCalendarClient:
    token_path = materialize_calendar_token_path(session, report_user_id)
    key = token_path or _LEGACY_SHARED_KEY
    mcp_session = _calendar_sessions.get(key)
    if mcp_session is None:
        oauth_credentials_path = os.environ.get("GOOGLE_OAUTH_CREDENTIALS")
        pending_session = McpSession(calendar_mcp_spec(oauth_credentials_path, token_path))
        try:
            mcp_session = pending_session.__enter__()
        except Exception:
            logger.exception(
                "agenda scheduler: failed to open a persistent calendar MCP "
                "session for report_user_id=%s (key=%s) — this poll falls "
                "back to a fresh per-call session instead (slower, but not "
                "fatal); will retry the persistent open again next poll",
                report_user_id,
                key,
            )
            try:
                pending_session.__exit__(None, None, None)
            except Exception:
                logger.exception(
                    "agenda scheduler: cleanup of the failed calendar MCP "
                    "session also raised — continuing anyway"
                )
            return LiveCalendarClient(token_path=token_path)
        _calendar_sessions[key] = mcp_session
    return LiveCalendarClient(session=mcp_session)


def poll_meeting_end_once(
    session_factory,
    report_user_ids: list[str],
    clock: Clock | None = None,
    calendar_client_factory: Callable[[Session, str], Any] = _calendar_client_for_scheduler,
) -> None:
    clock = clock or SystemClock()
    now = clock.now()
    window = Window(
        start=now - datetime.timedelta(hours=CALENDAR_LOOKBACK_HOURS), end=now
    )
    trust_by_default = len(report_user_ids) == 1

    for report_user_id in report_user_ids:
        session = session_factory()
        try:
            client = calendar_client_factory(session, report_user_id)
            trust_event_content = trust_by_default or is_google_connected(
                session, report_user_id
            )
        finally:
            session.close()

        try:
            events = client.fetch(window, report_user_id)
        except Exception:
            logger.exception(
                "agenda scheduler: calendar fetch failed report_user_id=%s",
                report_user_id,
            )
            continue

        for event in events:
            external_id = event.get("external_id")
            seen_key = (report_user_id, external_id)
            if not external_id or seen_key in _seen_meeting_ids:
                continue
            if not event.get("series_id"):
                continue
            if not _is_just_ended(event.get("ends_at"), now):
                continue
            event_for_processing = event
            if not trust_event_content and event.get("description"):
                event_for_processing = {**event, "description": None}
            succeeded = _process_meeting_end(
                session_factory,
                report_user_id,
                event_for_processing,
                clock,
                trust_event_content=trust_event_content,
            )
            if succeeded:
                _seen_meeting_ids.append(seen_key)


def _get_scheduled_report_user_ids() -> list[str]:
    raw = os.environ.get("AGENDA_SCHEDULED_REPORT_USER_IDS", "")
    report_user_ids = [r.strip() for r in raw.split(",") if r.strip()]
    if not report_user_ids:
        raise RuntimeError(
            "AGENDA_SCHEDULED_REPORT_USER_IDS is not set — refusing to start "
            "rather than silently scanning every user in the database for one "
            "with a resolvable PairScope (this dev DB is shared with the test "
            "suite, which creates its own throwaway users/pairs). Set it to a "
            "comma-separated list of report_user_id(s), e.g. "
            "AGENDA_SCHEDULED_REPORT_USER_IDS=usr_report1"
        )
    return report_user_ids


def main() -> None:
    from dotenv import load_dotenv

    load_dotenv()

    report_user_ids = _get_scheduled_report_user_ids()
    notion_pair_sync_enabled = os.environ.get(
        "AGENDA_NOTION_PAIR_SYNC", ""
    ).strip().lower() in ("1", "true", "yes")
    notion_auto_provision_enabled = os.environ.get(
        "AGENDA_AUTO_PROVISION_NOTION_USERS", ""
    ).strip().lower() in ("1", "true", "yes")
    notion_public_base_url = os.environ.get("AGENDA_PUBLIC_BASE_URL", "")
    notion_default_tz = os.environ.get("AGENDA_DEFAULT_TZ", "UTC")
    notion_default_pulse_fire_time_local = datetime.time.fromisoformat(
        os.environ.get("AGENDA_DEFAULT_PULSE_TIME_LOCAL", "08:30")
    )
    notion_default_late_cutoff_local = datetime.time.fromisoformat(
        os.environ.get("AGENDA_DEFAULT_LATE_CUTOFF_LOCAL", "21:00")
    )

    engine = get_engine(get_settings().database_url)
    session_factory = get_session_factory(engine)

    logger.info(
        "agenda scheduler: meeting-end poll every %ss for report_user_ids=%s; "
        "notion pair sync every %ss (enabled=%s); "
        "notion user auto-provision/revoke every %ss (enabled=%s, base_url=%s)",
        MEETING_POLL_INTERVAL_SECONDS,
        report_user_ids,
        ORG_DATA_SYNC_POLL_INTERVAL_SECONDS,
        notion_pair_sync_enabled,
        ORG_DATA_SYNC_POLL_INTERVAL_SECONDS,
        notion_auto_provision_enabled,
        notion_public_base_url or "(not set)",
    )
    last_notion_pair_sync_at: datetime.datetime | None = None
    last_notion_user_sync_at: datetime.datetime | None = None
    while True:
        clock = SystemClock()

        try:
            poll_meeting_end_once(session_factory, report_user_ids, clock)
        except Exception:
            logger.exception(
                "agenda scheduler: meeting-end poll failed, will retry next " "interval"
            )

        now = clock.now()

        # Provisioning/revocation runs BEFORE pair-sync in this same tick
        # (see run_notion_user_provisioning_once's own docstring) —
        # sync_notion_pair_edge needs a User row to already exist to
        # resolve an edge.
        notion_user_sync_due = notion_auto_provision_enabled and (
            last_notion_user_sync_at is None
            or (now - last_notion_user_sync_at).total_seconds()
            >= ORG_DATA_SYNC_POLL_INTERVAL_SECONDS
        )
        if notion_user_sync_due:
            session = session_factory()
            try:
                client = LiveNotionPairClient()
                provisioned = run_notion_user_provisioning_once(
                    session,
                    client,
                    clock,
                    notion_public_base_url,
                    default_tz=notion_default_tz,
                    default_pulse_fire_time_local=notion_default_pulse_fire_time_local,
                    default_late_cutoff_local=notion_default_late_cutoff_local,
                )
                revoked = run_notion_member_revocation_once(session, client, clock)
                logger.info(
                    "agenda scheduler: notion user sync provisioned %s, "
                    "revoked %s",
                    provisioned,
                    revoked,
                )
                last_notion_user_sync_at = now
            except Exception:
                logger.exception(
                    "agenda scheduler: notion user sync failed, will retry "
                    "next interval"
                )
            finally:
                session.close()

        notion_pair_due = notion_pair_sync_enabled and (
            last_notion_pair_sync_at is None
            or (now - last_notion_pair_sync_at).total_seconds()
            >= ORG_DATA_SYNC_POLL_INTERVAL_SECONDS
        )
        if notion_pair_due:
            session = session_factory()
            try:
                edge_count = run_notion_pair_sync_once(
                    session, LiveNotionPairClient(), clock
                )
                logger.info(
                    "agenda scheduler: notion pair sync processed %s edge(s)",
                    edge_count,
                )
                last_notion_pair_sync_at = now
            except Exception:
                logger.exception(
                    "agenda scheduler: notion pair sync failed, will retry next "
                    "interval"
                )
            finally:
                session.close()

        time.sleep(MEETING_POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()