"""Live Calendar SourceClient (app/ingest/base.py) backed by
@cocal/google-calendar-mcp. Split out of the former app/ingest/
live_source.py — see app/ingest/live_source/__init__.py's own docstring
for the split's full reasoning; every class/adapter here is re-exported
from there so `from app.ingest.live_source import LiveCalendarClient`
still works unchanged."""

import datetime
import os
import time

from app.ingest.base import Healthy, Unauthorized, Window
from app.tools.mcp_config import McpSession, calendar_mcp_spec, call_tool


def _adapt_calendar_event(raw: dict) -> dict:
    start = raw.get("start", {})
    end = raw.get("end", {})
    organizer_email = raw.get("organizer", {}).get("email")
    external_id = raw.get("id", "")
    return {
        "external_id": external_id,
        "source": "calendar",
        "title": raw.get("summary", ""),
        "starts_at": start.get("dateTime") or start.get("date"),
        "ends_at": end.get("dateTime") or end.get("date"),
        "status": raw.get("status", "confirmed"),
        "url": raw.get("htmlLink"),
        "series_id": raw.get("recurringEventId"),
        "actor_reference_key": f"calendar:{organizer_email or external_id}",
        # Real (if usually empty) content, not fabricated: whatever free
        # text the organizer put in the event's own description field —
        # meeting notes, an agenda, sometimes a pasted transcript. This
        # codebase has no real transcription service integrated (see
        # app.sub_agents.agenda.sub_agents.capture.agent's own docstring)
        # — the calendar event's description is the only actual content
        # a real meeting_end trigger has any access to today. Threaded
        # through to run_post_meeting_flow's transcript_text by app.
        # triggers.agenda_scheduler._process_meeting_end. None (not "")
        # when absent, matching fetch_transcript's own "nothing found"
        # contract.
        "description": raw.get("description") or None,
        # Previously never mapped at all — app.sub_agents.dossier.sub_
        # agents.gather.agent.gather_dossier_context reads event.get(
        # "attendees", []) to resolve who a dossier is even about, and
        # every real dossier silently got zero attendees regardless of who
        # was actually invited. Google Calendar's real event resource
        # shape (confirmed: @cocal/google-calendar-mcp passes the raw API
        # response through) is attendees: [{"email", "displayName",
        # "self", "organizer", "responseStatus", ...}] — "self" excluded
        # here since the dossier owner attending their own meeting isn't
        # a person to resolve an identity for. external_id is the email:
        # RawReference/build_reference_key (app/identity/normalize.py)
        # both accept external_id as the primary match key, and an email
        # is exactly what resolve()'s match_primary_email tier compares
        # against a Person's primary_email.
        "attendees": [
            {
                "source": "calendar",
                "external_id": a.get("email"),
                "email": a.get("email"),
                "display_name": a.get("displayName"),
            }
            for a in raw.get("attendees", [])
            if not a.get("self") and a.get("email")
        ],
    }


def _format_calendar_timestamp(dt: datetime.datetime) -> str:
    """@cocal/google-calendar-mcp's list-events tool validates timeMin/
    timeMax with a Zod schema that rejects Python's own .isoformat()
    output for a UTC datetime (produces a "+00:00" numeric offset) —
    confirmed against a real live server: the offset form fails with
    "Must be ISO 8601 format", a literal "Z" suffix succeeds. Converts to
    UTC first in case a caller ever passes a non-UTC-aware datetime."""
    return dt.astimezone(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class LiveCalendarClient:
    """Backed by @cocal/google-calendar-mcp's `list-events` tool (real
    schema: timeMin, timeMax, and calendarId — confirmed via live
    session.list_tools() introspection, documented in
    docs/plans/morning-pulse.md). calendarId is REQUIRED, not optional as
    first assumed — a live call failed with "Invalid input at calendarId"
    until this was fixed to always pass "primary" (the account's default
    calendar; per-calendar selection is a real future feature, not
    supported here yet). Requires GOOGLE_OAUTH_CREDENTIALS to point at a
    Desktop-app OAuth client JSON *and* a one-time interactive consent flow
    (`npx @cocal/google-calendar-mcp auth`) — health() only checks the env
    var is set, not that the OAuth flow has completed."""

    source = "calendar"

    def __init__(
        self,
        oauth_credentials_path: str | None = None,
        session: McpSession | None = None,
        token_path: str | None = None,
    ):
        self.oauth_credentials_path = oauth_credentials_path or os.environ.get(
            "GOOGLE_OAUTH_CREDENTIALS"
        )
        # Per-user token cache override (GOOGLE_CALENDAR_MCP_TOKEN_PATH) —
        # see app.ingest.google_credential_store.materialize_calendar_
        # token_path. None means the legacy single-shared-account
        # behavior every caller had before per-user Google credentials
        # existed.
        self.token_path = token_path
        # Optional pre-opened, long-lived McpSession (see app.tools.
        # mcp_config's own docstring on why: call_tool() spawns a fresh
        # npx process + does a fresh OAuth handshake on EVERY call —
        # measured live at ~17s per fetch(). For a caller that polls
        # repeatedly (app.triggers.agenda.agenda_scheduler's meeting-end job,
        # every 60s), that's not just slow: overlapping/queued cold
        # spawns competing for the same on-disk token file produced real
        # "operation was aborted" failures on every poll. A caller that
        # already knows it will call fetch() many times over its own
        # lifetime should open one McpSession itself and pass it in here
        # — the process spawn + handshake then happens once, not once
        # per fetch(). Falls back to the original call_tool() behavior
        # (fresh spawn per call) when no session is given, unchanged for
        # every other caller (tests, one-off scripts, seed_live).
        self._session = session

    def _spec(self):
        return calendar_mcp_spec(self.oauth_credentials_path, self.token_path)

    def health(self):
        if not self.oauth_credentials_path:
            return Unauthorized()
        return Healthy()

    def fetch(self, window: Window, owner_user_id: str) -> list[dict]:
        arguments = {
            "calendarId": "primary",
            "timeMin": _format_calendar_timestamp(window.start),
            "timeMax": _format_calendar_timestamp(window.end),
        }
        raw = self._call_with_retry(arguments)
        events = raw.get("events", raw) if isinstance(raw, dict) else raw
        return [_adapt_calendar_event(e) for e in events]

    def _call_with_retry(self, arguments: dict):
        # "Google API error: The operation was aborted." (confirmed live,
        # repeatedly, this session) is a transient failure from a second
        # process's npx spawn colliding with this one on the shared
        # on-disk OAuth token file — not a real auth/argument problem
        # (retrying the identical call with no code change has been
        # observed live to succeed a few seconds later). One bounded
        # retry after a short pause; any OTHER error (bad argument, real
        # auth failure, etc.) is NOT retried — it fails immediately, same
        # as before this change, so a genuine bug doesn't silently get
        # masked as "just try again."
        try:
            if self._session is not None:
                # Bounded, not the McpSession default (block forever) — a
                # long-lived session (this class's whole reason for
                # accepting one) whose background driver has died or
                # whose transport broke must raise on the next poll
                # instead of hanging this caller's entire process
                # forever with no exception and no log line.
                return self._session.call("list-events", arguments, timeout=30)
            return call_tool(self._spec(), "list-events", arguments)
        except Exception as exc:
            if "operation was aborted" not in str(exc).lower():
                raise RuntimeError(f"calendar fetch failed: {exc}") from exc
            time.sleep(2)
            try:
                if self._session is not None:
                    return self._session.call("list-events", arguments, timeout=30)
                return call_tool(self._spec(), "list-events", arguments)
            except Exception as retry_exc:
                raise RuntimeError(
                    f"calendar fetch failed after retry: {retry_exc}"
                ) from retry_exc
