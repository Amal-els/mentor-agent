"""Live Fathom meeting-transcript client, backed by the official Fathom
remote MCP server (HTTP+OAuth). Split out of the former app/ingest/
live_source.py — see app/ingest/live_source/__init__.py's own docstring
for the split's full reasoning; every class/adapter here is re-exported
from there so `from app.ingest.live_source import LiveFathomClient` still
works unchanged."""

from app.ingest.base import Healthy, Unauthorized, Window
from app.ingest.live_source.calendar_ import _format_calendar_timestamp
from app.tools.mcp_config import (
    FileTokenStorage,
    HttpMcpSession,
    default_fathom_token_path,
    fathom_mcp_spec,
)


def _adapt_fathom_transcript(raw) -> str | None:
    """UNVERIFIED — no meeting has ever been recorded on the account used
    to confirm the OAuth flow this session, so get_meeting_transcript's
    real response shape (structured JSON vs. the prose/markdown a live
    list_meetings call returned for its EMPTY-result case: the literal
    string "Found 0 meeting(s).", confirmed live) was never actually
    observed. Written defensively against both possibilities: a
    structured {"transcript": [{"speaker": {"display_name": ...},
    "text": ..., "timestamp": ...}, ...]} shape (Fathom's documented
    public REST API shape) and a plain string (if this MCP tool, like
    list_meetings, returns LLM-oriented prose rather than strict JSON).
    Confirm against a real recorded meeting before trusting this."""
    if isinstance(raw, str):
        return raw or None
    if isinstance(raw, dict):
        segments = raw.get("transcript")
        if isinstance(segments, str):
            return segments or None
        if isinstance(segments, list):
            lines = []
            for seg in segments:
                if not isinstance(seg, dict):
                    continue
                speaker = seg.get("speaker", {})
                name = (
                    speaker.get("display_name")
                    if isinstance(speaker, dict)
                    else speaker
                )
                text = seg.get("text", "")
                lines.append(f"{name}: {text}" if name else text)
            return "\n".join(lines) if lines else None
    return None


class LiveFathomClient:
    """Backed by the official Fathom remote MCP server
    (https://api.fathom.ai/mcp, HTTP+OAuth — see HttpMcpSession/
    fathom_mcp_spec, app/tools/mcp_config.py). Real tool names and INPUT
    schemas confirmed live this session via a completed end-to-end OAuth
    round-trip (dynamic client registration, browser consent, callback):
    list_meetings (cursor, created_after, created_before, recorded_by,
    teams, include_summary, include_action_items, max_pages) and
    get_meeting_transcript (recording_id: int, required; url: optional,
    for timestamped deep links).

    UNVERIFIED: the account has zero recorded meetings, so neither
    tool's real RESPONSE shape was ever observed non-empty — see
    _adapt_fathom_transcript's own docstring. Confirm against a real
    recorded meeting before trusting this in production — written
    defensively, not yet proven.

    Not a SourceClient (app/ingest/base.py) — deliberately NOT wired into
    seed_live's fetch_plan. find_transcript() is a targeted, on-demand
    "find the transcript for one meeting" lookup (matches
    run_post_meeting_flow's existing meeting-scoped transcript_text
    param), not a day-window bulk pull like Calendar/Gmail. Wired into
    app/triggers/agenda_router.py's meeting_end handling instead.

    recording_id has no direct link to this app's own calendar event id
    (Fathom's API exposes no such cross-reference) — find_transcript
    matches by time window (the calendar event's own start/end, widened
    slightly) and, if given, the organizer's email via recorded_by. This
    is a heuristic match, not a guaranteed-correct one, same coarse-
    matching caveat this session already documented for the calendar
    description-as-transcript fallback it replaces."""

    source = "fathom"

    def __init__(self, token_storage: FileTokenStorage | None = None):
        self.token_storage = token_storage or FileTokenStorage(
            default_fathom_token_path()
        )

    def _spec(self):
        return fathom_mcp_spec(self.token_storage)

    def health(self):
        return Healthy() if self.token_storage.has_tokens() else Unauthorized()

    def find_transcript(
        self, window: Window, organizer_email: str | None = None
    ) -> str | None:
        try:
            with HttpMcpSession(self._spec()) as session:
                arguments: dict = {
                    "created_after": _format_calendar_timestamp(window.start),
                    "created_before": _format_calendar_timestamp(window.end),
                    "max_pages": 1,
                }
                if organizer_email:
                    arguments["recorded_by"] = [organizer_email]
                meetings_result = session.call("list_meetings", arguments)

                if not isinstance(meetings_result, (dict, list)):
                    # confirmed live: an empty result comes back as prose
                    # ("Found 0 meeting(s)."), not JSON — nothing to
                    # reliably parse a recording_id out of.
                    return None
                meetings = (
                    meetings_result.get("meetings", meetings_result)
                    if isinstance(meetings_result, dict)
                    else meetings_result
                )
                if not meetings:
                    return None
                first = meetings[0]
                if not isinstance(first, dict):
                    return None
                recording_id = first.get("recording_id") or first.get("id")
                if recording_id is None:
                    return None

                transcript_result = session.call(
                    "get_meeting_transcript", {"recording_id": recording_id}
                )
        except Exception:
            return None
        return _adapt_fathom_transcript(transcript_result)
