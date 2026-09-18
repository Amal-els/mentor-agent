"""Unit tests for the response-adapter functions only — pure parsing, no
transport, no credentials. The MCP tool calls themselves (spawning a real
authenticated server) are covered by tests/live/test_mcp_smoke.py, gated
and skipped until real credentials exist. See app/tools/mcp_config.py's
module docstring for the full caveat."""

import datetime
import json

from app.ingest.base import Healthy, Unauthorized, Window
from app.ingest.live_source import (
    LiveCalendarClient,
    LiveFathomClient,
    LiveGmailClient,
    LiveGoogleDocsClient,
    LiveJiraClient,
    LiveLinearClient,
    LiveNotionGoalsClient,
    LiveNotionNotesClient,
    LiveNotionPairClient,
    LiveSlackClient,
    _adapt_calendar_event,
    _adapt_fathom_transcript,
    _adapt_gmail_message,
    _adapt_google_docs_comment,
    _adapt_jira_issue,
    _adapt_linear_issue,
    _adapt_notion_career_goal,
    _adapt_notion_key_result,
    _adapt_notion_objective,
    _adapt_notion_one_on_one_note,
    _adapt_slack_message,
    _blocks_others,
    _is_gmail_noise,
    _notion_rollup_progress,
    resolve_slack_mentions_for_display,
)
from app.tools.mcp_config import FileTokenStorage


class _FakeMcpSession:
    """Stand-in for app.tools.mcp_config.McpSession — Slack/Jira/Google
    Docs (the N+1-call connectors) now open one session per fetch() and
    call .call() on it instead of the old per-call call_tool(spec,
    tool_name, arguments); these tests fake at the same granularity,
    replaying fake_call_tool's (tool_name, arguments) -> result contract
    against a fake session instead of a fake call_tool."""

    def __init__(self, spec, fake_call_tool):
        self._spec = spec
        self._fake_call_tool = fake_call_tool

    def __enter__(self):
        return self

    def call(self, tool_name, arguments):
        return self._fake_call_tool(self._spec, tool_name, arguments)

    def __exit__(self, exc_type, exc, tb):
        return False


def _fake_mcp_session(fake_call_tool):
    return lambda spec: _FakeMcpSession(spec, fake_call_tool)


def test_adapt_calendar_event_maps_standard_fields():
    raw = {
        "id": "evt123",
        "summary": "1:1 with Sarah",
        "start": {"dateTime": "2026-08-07T09:00:00+02:00"},
        "end": {"dateTime": "2026-08-07T09:30:00+02:00"},
        "status": "confirmed",
        "htmlLink": "https://calendar.google.com/evt123",
        "organizer": {"email": "sarah@acme.com"},
    }

    row = _adapt_calendar_event(raw)

    assert row["external_id"] == "evt123"
    assert row["source"] == "calendar"
    assert row["title"] == "1:1 with Sarah"
    assert row["starts_at"] == "2026-08-07T09:00:00+02:00"
    assert row["ends_at"] == "2026-08-07T09:30:00+02:00"
    assert row["status"] == "confirmed"
    assert row["actor_reference_key"] == "calendar:sarah@acme.com"
    assert row["url"] == "https://calendar.google.com/evt123"


def test_adapt_calendar_event_maps_attendees_excluding_self():
    raw = {
        "id": "evt123",
        "summary": "1:1 with Sarah",
        "start": {"dateTime": "2026-08-07T09:00:00+02:00"},
        "end": {"dateTime": "2026-08-07T09:30:00+02:00"},
        "attendees": [
            {"email": "me@acme.com", "displayName": "Me", "self": True},
            {"email": "sarah@acme.com", "displayName": "Sarah", "self": False},
            {"email": None, "displayName": "No email"},
        ],
    }

    row = _adapt_calendar_event(raw)

    assert row["attendees"] == [
        {
            "source": "calendar",
            "external_id": "sarah@acme.com",
            "email": "sarah@acme.com",
            "display_name": "Sarah",
        }
    ]


def test_adapt_calendar_event_attendees_defaults_to_empty_list():
    raw = {
        "id": "evt123",
        "summary": "No attendees field at all",
        "start": {"dateTime": "2026-08-07T09:00:00+02:00"},
        "end": {"dateTime": "2026-08-07T09:30:00+02:00"},
    }

    row = _adapt_calendar_event(raw)

    assert row["attendees"] == []


def test_adapt_calendar_event_handles_all_day_events():
    raw = {
        "id": "evt456",
        "summary": "Company holiday",
        "start": {"date": "2026-08-10"},
        "end": {"date": "2026-08-11"},
    }

    row = _adapt_calendar_event(raw)

    assert row["starts_at"] == "2026-08-10"
    assert row["ends_at"] == "2026-08-11"


def test_adapt_calendar_event_falls_back_to_external_id_actor_when_no_organizer():
    raw = {
        "id": "evt789",
        "start": {"dateTime": "2026-08-07T09:00:00+02:00"},
        "end": {"dateTime": "2026-08-07T09:30:00+02:00"},
    }

    row = _adapt_calendar_event(raw)

    assert row["actor_reference_key"] == "calendar:evt789"
    assert row["title"] == ""


def test_adapt_calendar_event_carries_recurring_series_id():
    raw = {
        "id": "evt999",
        "start": {"dateTime": "2026-08-07T10:00:00+02:00"},
        "end": {"dateTime": "2026-08-07T10:15:00+02:00"},
        "recurringEventId": "series-standup",
    }

    row = _adapt_calendar_event(raw)

    assert row["series_id"] == "series-standup"


def test_adapt_calendar_event_carries_the_description_as_real_transcript_content():
    """The only real (not fabricated) content a real meeting_end trigger
    has access to today — app.triggers.agenda.agenda_scheduler._process_
    meeting_end threads this through as run_post_meeting_flow's
    transcript_text, since Capture has no real transcript source of its
    own (confirmed live: without this, every real trigger produced zero
    agenda items)."""
    raw = {
        "id": "evt111",
        "start": {"dateTime": "2026-08-07T09:00:00+02:00"},
        "end": {"dateTime": "2026-08-07T09:30:00+02:00"},
        "description": "Discussed Q3 goals. Bob to ship the migration by Friday.",
    }

    row = _adapt_calendar_event(raw)

    assert (
        row["description"] == "Discussed Q3 goals. Bob to ship the migration by Friday."
    )


def test_adapt_calendar_event_description_is_none_not_empty_string_when_absent():
    raw = {
        "id": "evt222",
        "start": {"dateTime": "2026-08-07T09:00:00+02:00"},
        "end": {"dateTime": "2026-08-07T09:30:00+02:00"},
    }

    row = _adapt_calendar_event(raw)

    assert row["description"] is None


def test_adapt_slack_message_never_includes_raw_text():
    raw = {
        "ts": "1699999999.000100",
        "user": "U12345",
        "text": "actual message content",
    }

    row = _adapt_slack_message(raw, channel_id="D0123")

    assert "text" not in row
    assert "actual message content" not in str(row)
    assert row["body_ref"].startswith("slack://")
    assert row["external_id"] == "1699999999.000100"
    assert row["channel"] == "D0123"
    assert row["actor_reference_key"] == "slack:U12345"


def test_adapt_slack_message_flags_dm_channels():
    raw = {"ts": "1699999999.000100", "user": "U12345"}

    dm_row = _adapt_slack_message(raw, channel_id="D0123")
    channel_row = _adapt_slack_message(raw, channel_id="C0123")

    assert dm_row["is_dm"] is True
    assert channel_row["is_dm"] is False


def test_adapt_slack_message_carries_resolved_channel_and_sender_names():
    """REAL CHANGE (requested: "resolve user ids and channel ids in
    slack to the username and channel name")."""
    raw = {"ts": "1699999999.000100", "user": "U12345"}

    row = _adapt_slack_message(
        raw,
        channel_id="C0123",
        channel_name="general",
        sender_display_name="Jane Doe",
    )

    assert row["channel_name"] == "general"
    assert row["sender_display_name"] == "Jane Doe"


def test_adapt_slack_message_defaults_names_to_none_when_not_given():
    raw = {"ts": "1699999999.000100", "user": "U12345"}

    row = _adapt_slack_message(raw, channel_id="C0123")

    assert row["channel_name"] is None
    assert row["sender_display_name"] is None


def test_adapt_linear_issue_maps_standard_fields():
    raw = {
        "id": "uuid-1",
        "identifier": "MENT-214",
        "title": "Fix flaky roster test",
        "state": {"name": "blocked"},
        "assignee": {"email": "sarah@acme.com"},
        "dueDate": "2026-08-07",
        "updatedAt": "2026-08-06T16:00:00Z",
        "url": "https://linear.app/MENT-214",
    }

    row = _adapt_linear_issue(raw)

    assert row["external_id"] == "MENT-214"
    assert row["source"] == "linear"
    assert row["title"] == "Fix flaky roster test"
    assert row["status"] == "blocked"
    assert row["due_at"] == "2026-08-07"
    assert row["updated_at"] == "2026-08-06T16:00:00Z"
    assert row["actor_reference_key"] == "linear:sarah@acme.com"
    assert row["url"] == "https://linear.app/MENT-214"


def test_adapt_linear_issue_falls_back_to_id_when_no_identifier_or_assignee():
    raw = {"id": "uuid-2", "title": "Untriaged issue"}

    row = _adapt_linear_issue(raw)

    assert row["external_id"] == "uuid-2"
    assert row["actor_reference_key"] == "linear:uuid-2"


def test_adapt_jira_issue_maps_the_real_flattened_shape():
    """Real jira_search_issues response (confirmed live via create->search->
    delete round-trip): a pre-flattened shape — status/assignee/reporter as
    plain strings, no self/url field, no email or accountId."""
    raw = {
        "summary": "mentor-agent connector probe (safe to delete)",
        "issuetype": "Task",
        "created": "2026-08-11T09:21:00.735+0100",
        "description": "",
        "project": "SCRUM",
        "reporter": "Amal Bahri",
        "priority": "Medium",
        "resolution": None,
        "labels": [],
        "duedate": None,
        "assignee": "Amal Bahri",
        "updated": "2026-08-11T09:22:00.801+0100",
        "status": "To Do",
        "key": "SCRUM-1",
    }

    row = _adapt_jira_issue(raw, base_url="https://amalbahri19.atlassian.net")

    assert row["external_id"] == "SCRUM-1"
    assert row["source"] == "jira"
    assert row["title"] == "mentor-agent connector probe (safe to delete)"
    assert row["status"] == "To Do"
    assert row["due_at"] is None
    assert row["updated_at"] == "2026-08-11T09:22:00.801+0100"
    assert row["url"] == "https://amalbahri19.atlassian.net/browse/SCRUM-1"
    assert row["actor_reference_key"] == "jira:Amal Bahri"
    assert row["blocks_others"] is False


def test_adapt_jira_issue_falls_back_to_key_when_unassigned():
    raw = {"key": "SCRUM-2", "summary": "Untriaged", "assignee": None}

    row = _adapt_jira_issue(raw, base_url="https://amalbahri19.atlassian.net")

    assert row["actor_reference_key"] == "jira:SCRUM-2"


def test_adapt_jira_issue_carries_the_blocks_others_flag():
    raw = {"key": "SCRUM-2", "summary": "Blocker", "assignee": "Amal Bahri"}

    row = _adapt_jira_issue(
        raw, base_url="https://amalbahri19.atlassian.net", blocks_others=True
    )

    assert row["blocks_others"] is True


def test_blocks_others_true_for_an_outward_blocks_link():
    """Real jira_get_issue_links response shape (confirmed live: created
    SCRUM-2 Blocks SCRUM-3 in a real Jira Cloud project and inspected both
    directions)."""
    links_response = {
        "issueKey": "SCRUM-2",
        "links": [
            {
                "id": "10000",
                "type": "Blocks",
                "direction": "outward",
                "description": "blocks",
                "linkedIssue": {
                    "key": "SCRUM-3",
                    "summary": "throwaway-signal-probe-B (blocked)",
                    "status": "To Do",
                    "issueType": "Task",
                },
            }
        ],
    }

    assert _blocks_others(links_response) is True


def test_blocks_others_false_for_an_inward_is_blocked_by_link():
    """The opposite relationship — this issue is blocked, not blocking —
    must not count."""
    links_response = {
        "issueKey": "SCRUM-3",
        "links": [
            {
                "id": "10000",
                "type": "Blocks",
                "direction": "inward",
                "description": "is blocked by",
                "linkedIssue": {"key": "SCRUM-2"},
            }
        ],
    }

    assert _blocks_others(links_response) is False


def test_blocks_others_false_when_no_links():
    assert _blocks_others({"issueKey": "SCRUM-1", "links": []}) is False


def test_blocks_others_false_for_non_dict_response():
    assert _blocks_others([]) is False


def test_jira_client_health_reflects_credential_presence(monkeypatch):
    for var in ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    client = LiveJiraClient()
    assert isinstance(client.health(), Unauthorized)

    monkeypatch.setenv("JIRA_BASE_URL", "https://fake.atlassian.net")
    monkeypatch.setenv("JIRA_EMAIL", "fake@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "fake-token")
    client = LiveJiraClient()
    assert isinstance(client.health(), Healthy)


def test_jira_fetch_uses_a_bounded_jql_and_unwraps_the_issues_list(monkeypatch):
    """Real behavior (confirmed live): unbounded JQL is rejected by this
    Jira Cloud instance with a 400 ("Unbounded JQL queries are not allowed
    here") — the query must always carry a restriction clause."""
    calls = []

    def fake_call_tool(spec, tool_name, arguments):
        calls.append((tool_name, arguments))
        if tool_name == "jira_search_issues":
            return {"total": 1, "issues": [{"key": "SCRUM-1", "summary": "Only issue"}]}
        return {"issueKey": "SCRUM-1", "links": []}

    monkeypatch.setattr(
        "app.ingest.live_source.jira.McpSession", _fake_mcp_session(fake_call_tool)
    )
    monkeypatch.setenv("JIRA_BASE_URL", "https://fake.atlassian.net")
    monkeypatch.setenv("JIRA_EMAIL", "fake@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "fake-token")

    rows = LiveJiraClient().fetch(window=None, owner_user_id="usr_1")

    search_tool_name, search_arguments = calls[0]
    assert search_tool_name == "jira_search_issues"
    assert "currentUser()" in search_arguments["jql"]
    assert len(rows) == 1
    assert rows[0]["external_id"] == "SCRUM-1"
    assert rows[0]["blocks_others"] is False
    assert ("jira_get_issue_links", {"issueIdOrKey": "SCRUM-1"}) in calls


def test_jira_fetch_propagates_blocks_others_true(monkeypatch):
    def fake_call_tool(spec, tool_name, arguments):
        if tool_name == "jira_search_issues":
            return {"issues": [{"key": "SCRUM-2", "summary": "Blocker"}]}
        return {
            "issueKey": "SCRUM-2",
            "links": [{"type": "Blocks", "direction": "outward"}],
        }

    monkeypatch.setattr(
        "app.ingest.live_source.jira.McpSession", _fake_mcp_session(fake_call_tool)
    )
    monkeypatch.setenv("JIRA_BASE_URL", "https://fake.atlassian.net")
    monkeypatch.setenv("JIRA_EMAIL", "fake@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "fake-token")

    rows = LiveJiraClient().fetch(window=None, owner_user_id="usr_1")

    assert rows[0]["blocks_others"] is True


def test_jira_fetch_survives_a_links_lookup_failure_for_one_issue(monkeypatch):
    """A links lookup failing must not fail the whole fetch — the issue
    just scores as blocks_others=False, same degrade-gracefully pattern
    the rest of this connector follows."""

    def fake_call_tool(spec, tool_name, arguments):
        if tool_name == "jira_search_issues":
            return {"issues": [{"key": "SCRUM-1", "summary": "Only issue"}]}
        raise RuntimeError("jira_get_issue_links unavailable")

    monkeypatch.setattr(
        "app.ingest.live_source.jira.McpSession", _fake_mcp_session(fake_call_tool)
    )
    monkeypatch.setenv("JIRA_BASE_URL", "https://fake.atlassian.net")
    monkeypatch.setenv("JIRA_EMAIL", "fake@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "fake-token")

    rows = LiveJiraClient().fetch(window=None, owner_user_id="usr_1")

    assert len(rows) == 1
    assert rows[0]["blocks_others"] is False


def test_calendar_client_health_reflects_credential_presence(monkeypatch):
    monkeypatch.delenv("GOOGLE_OAUTH_CREDENTIALS", raising=False)
    client = LiveCalendarClient()
    assert isinstance(client.health(), Unauthorized)

    monkeypatch.setenv("GOOGLE_OAUTH_CREDENTIALS", "/path/to/creds.json")
    client = LiveCalendarClient()
    assert isinstance(client.health(), Healthy)


def test_calendar_fetch_passes_the_required_calendar_id(monkeypatch):
    """list-events real schema (confirmed live): calendarId is required,
    not optional as first assumed from the tool's own docstring hints — a
    live call failed with "Invalid input at calendarId" until this was
    added. "primary" is the account's default calendar."""
    captured = {}

    def fake_call_tool(spec, tool_name, arguments):
        captured["tool_name"] = tool_name
        captured["arguments"] = arguments
        return {"events": []}

    monkeypatch.setattr("app.ingest.live_source.calendar_.call_tool", fake_call_tool)
    monkeypatch.setenv("GOOGLE_OAUTH_CREDENTIALS", "/path/to/creds.json")

    window = Window(
        start=datetime.datetime(2026, 8, 10, 0, 0, tzinfo=datetime.UTC),
        end=datetime.datetime(2026, 8, 10, 23, 59, 59, tzinfo=datetime.UTC),
    )
    LiveCalendarClient().fetch(window, owner_user_id="usr_1")

    assert captured["tool_name"] == "list-events"
    assert captured["arguments"]["calendarId"] == "primary"


def test_calendar_fetch_uses_a_given_session_instead_of_spawning_fresh(monkeypatch):
    """A live run measured call_tool()'s fresh-spawn-per-call cost at
    ~17s (fresh npx process + fresh OAuth handshake every call), which
    on a 60s poll interval risked missing the 3-minute "just ended"
    window and produced real "operation was aborted" errors under any
    overlap. A caller holding a long-lived McpSession must have fetch()
    use it, not fall back to call_tool()."""

    def fail_if_called(*args, **kwargs):
        raise AssertionError("call_tool() should not be used when a session is given")

    monkeypatch.setattr("app.ingest.live_source.calendar_.call_tool", fail_if_called)
    monkeypatch.setenv("GOOGLE_OAUTH_CREDENTIALS", "/path/to/creds.json")

    captured = {}

    class _FakeSession:
        def call(self, tool_name, arguments, timeout=None):
            captured["tool_name"] = tool_name
            captured["arguments"] = arguments
            captured["timeout"] = timeout
            return {"events": []}

    window = Window(
        start=datetime.datetime(2026, 8, 10, 0, 0, tzinfo=datetime.UTC),
        end=datetime.datetime(2026, 8, 10, 23, 59, 59, tzinfo=datetime.UTC),
    )
    LiveCalendarClient(session=_FakeSession()).fetch(window, owner_user_id="usr_1")

    assert captured["tool_name"] == "list-events"
    assert captured["arguments"]["calendarId"] == "primary"
    # bounded, not McpSession.call()'s default (block forever) — a
    # long-lived session's driver dying must raise on the next poll,
    # not hang this process's single-threaded loop forever.
    assert captured["timeout"] is not None


def test_calendar_fetch_formats_timestamps_with_a_literal_z_suffix(monkeypatch):
    """The live server's Zod schema rejects Python's own .isoformat()
    output for a UTC datetime (produces "+00:00") with "Must be ISO 8601
    format" — confirmed against a real live server. Requires a literal
    "Z" suffix and no microseconds instead."""
    captured = {}

    def fake_call_tool(spec, tool_name, arguments):
        captured["arguments"] = arguments
        return {"events": []}

    monkeypatch.setattr("app.ingest.live_source.calendar_.call_tool", fake_call_tool)
    monkeypatch.setenv("GOOGLE_OAUTH_CREDENTIALS", "/path/to/creds.json")

    window = Window(
        start=datetime.datetime(2026, 8, 10, 0, 0, 0, 123456, tzinfo=datetime.UTC),
        end=datetime.datetime(2026, 8, 10, 23, 59, 59, tzinfo=datetime.UTC),
    )
    LiveCalendarClient().fetch(window, owner_user_id="usr_1")

    assert captured["arguments"]["timeMin"] == "2026-08-10T00:00:00Z"
    assert captured["arguments"]["timeMax"] == "2026-08-10T23:59:59Z"


def test_calendar_fetch_retries_once_on_operation_was_aborted(monkeypatch):
    """"Google API error: The operation was aborted." (confirmed live,
    repeatedly, this session) is a transient failure from a second
    process's npx spawn colliding with this one on the shared on-disk
    OAuth token file — retrying the identical call has been observed
    live to succeed a few seconds later."""
    calls = {"count": 0}

    def fake_call_tool(spec, tool_name, arguments):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError(
                "tool 'list-events' returned an error: MCP error -32600: "
                "Google API error: The operation was aborted."
            )
        return {"events": []}

    monkeypatch.setattr("app.ingest.live_source.calendar_.call_tool", fake_call_tool)
    monkeypatch.setattr("app.ingest.live_source.calendar_.time.sleep", lambda seconds: None)
    monkeypatch.setenv("GOOGLE_OAUTH_CREDENTIALS", "/path/to/creds.json")

    window = Window(
        start=datetime.datetime(2026, 8, 10, 0, 0, tzinfo=datetime.UTC),
        end=datetime.datetime(2026, 8, 10, 23, 59, 59, tzinfo=datetime.UTC),
    )
    events = LiveCalendarClient().fetch(window, owner_user_id="usr_1")

    assert events == []
    assert calls["count"] == 2


def test_calendar_fetch_does_not_retry_a_different_error(monkeypatch):
    def fake_call_tool(spec, tool_name, arguments):
        raise RuntimeError("Invalid input at calendarId")

    monkeypatch.setattr("app.ingest.live_source.calendar_.call_tool", fake_call_tool)
    monkeypatch.setenv("GOOGLE_OAUTH_CREDENTIALS", "/path/to/creds.json")

    window = Window(
        start=datetime.datetime(2026, 8, 10, 0, 0, tzinfo=datetime.UTC),
        end=datetime.datetime(2026, 8, 10, 23, 59, 59, tzinfo=datetime.UTC),
    )
    try:
        LiveCalendarClient().fetch(window, owner_user_id="usr_1")
        raise AssertionError("expected a RuntimeError")
    except RuntimeError as exc:
        assert "Invalid input at calendarId" in str(exc)
        assert "after retry" not in str(exc)


def test_calendar_fetch_raises_when_the_retry_also_fails(monkeypatch):
    def fake_call_tool(spec, tool_name, arguments):
        raise RuntimeError("Google API error: The operation was aborted.")

    monkeypatch.setattr("app.ingest.live_source.calendar_.call_tool", fake_call_tool)
    monkeypatch.setattr("app.ingest.live_source.calendar_.time.sleep", lambda seconds: None)
    monkeypatch.setenv("GOOGLE_OAUTH_CREDENTIALS", "/path/to/creds.json")

    window = Window(
        start=datetime.datetime(2026, 8, 10, 0, 0, tzinfo=datetime.UTC),
        end=datetime.datetime(2026, 8, 10, 23, 59, 59, tzinfo=datetime.UTC),
    )
    try:
        LiveCalendarClient().fetch(window, owner_user_id="usr_1")
        raise AssertionError("expected a RuntimeError")
    except RuntimeError as exc:
        assert "after retry" in str(exc)


def test_slack_client_health_reflects_credential_presence(monkeypatch):
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    client = LiveSlackClient()
    assert isinstance(client.health(), Unauthorized)

    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-fake")
    client = LiveSlackClient()
    assert isinstance(client.health(), Healthy)


def test_linear_client_health_reflects_credential_presence(monkeypatch):
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    client = LiveLinearClient()
    assert isinstance(client.health(), Unauthorized)

    monkeypatch.setenv("LINEAR_API_KEY", "lin_api_fake")
    client = LiveLinearClient()
    assert isinstance(client.health(), Healthy)


class _FakeSlackWebClient:
    """Stand-in for slack_sdk.WebClient — LiveSlackClient._permalink() calls
    the real Slack Web API directly (no MCP tool exposes chat.getPermalink;
    see LiveSlackClient's own docstring for why), so it must be mocked
    separately from call_tool or unit tests would hit the real network."""

    def __init__(self, token):
        self.token = token

    def chat_getPermalink(self, channel, message_ts):
        return {"permalink": f"https://fake.slack.com/archives/{channel}/p{message_ts}"}

    def users_info(self, user):
        return {
            "user": {
                "real_name": f"Real Name {user}",
                "profile": {"display_name": f"Display Name {user}"},
            }
        }


def test_slack_fetch_skips_channels_the_bot_cannot_read(monkeypatch):
    """Real Slack behavior: slack_list_channels can list channels the bot
    hasn't been invited to, and slack_get_channel_history for those returns
    {"ok": false, "error": "not_in_channel"} rather than raising — this must
    be skipped, not treated as a messages list."""
    calls = {"n": 0}

    def fake_call_tool(spec, tool_name, arguments):
        if tool_name == "slack_list_channels":
            return {
                "channels": [
                    {"id": "C_NO_ACCESS"},
                    {"id": "C_OK"},
                ]
            }
        calls["n"] += 1
        if arguments["channel_id"] == "C_NO_ACCESS":
            return {"ok": False, "error": "not_in_channel"}
        return {"ok": True, "messages": [{"ts": "1699999999.0001", "user": "U1"}]}

    monkeypatch.setattr(
        "app.ingest.live_source.slack.McpSession", _fake_mcp_session(fake_call_tool)
    )
    monkeypatch.setattr("app.ingest.live_source.slack.WebClient", _FakeSlackWebClient)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-fake")

    rows = LiveSlackClient().fetch(window=None, owner_user_id="usr_1")

    assert calls["n"] == 2
    assert len(rows) == 1
    assert rows[0]["channel"] == "C_OK"
    assert rows[0]["url"] == "https://fake.slack.com/archives/C_OK/p1699999999.0001"


def test_slack_fetch_only_keeps_messages_that_mention_the_viewer(monkeypatch):
    """A live run caught this for real: without filtering, fetch() returned
    every message in every channel the bot could see — a Slack DM/mention
    connector should mean "unread mentions," not "all channel history"."""

    def fake_call_tool(spec, tool_name, arguments):
        if tool_name == "slack_list_channels":
            return {"channels": [{"id": "C1"}]}
        return {
            "ok": True,
            "messages": [
                {"ts": "1.1", "user": "U1", "text": "<@U0VIEWER> can you review this?"},
                {"ts": "2.2", "user": "U2", "text": "unrelated chatter"},
            ],
        }

    monkeypatch.setattr(
        "app.ingest.live_source.slack.McpSession", _fake_mcp_session(fake_call_tool)
    )
    monkeypatch.setattr("app.ingest.live_source.slack.WebClient", _FakeSlackWebClient)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-fake")

    rows = LiveSlackClient(viewer_slack_user_id="U0VIEWER").fetch(
        window=None, owner_user_id="usr_1"
    )

    assert len(rows) == 1
    assert rows[0]["external_id"] == "1.1"


def test_slack_fetch_excludes_the_bots_own_messages_even_if_they_mention_the_viewer(
    monkeypatch,
):
    """A live run caught this for real too: the bot's own delivery/ack
    messages can themselves mention the linked user (e.g. a probe message),
    and without this exclusion those get picked up as if a human had
    tagged the user."""

    def fake_call_tool(spec, tool_name, arguments):
        if tool_name == "slack_list_channels":
            return {"channels": [{"id": "C1"}]}
        return {
            "ok": True,
            "messages": [
                {
                    "ts": "1.1",
                    "user": "UBOT",
                    "bot_id": "B0BOT",
                    "text": "<@U0VIEWER> automated notice",
                },
            ],
        }

    monkeypatch.setattr(
        "app.ingest.live_source.slack.McpSession", _fake_mcp_session(fake_call_tool)
    )
    monkeypatch.setattr("app.ingest.live_source.slack.WebClient", _FakeSlackWebClient)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-fake")

    rows = LiveSlackClient(viewer_slack_user_id="U0VIEWER").fetch(
        window=None, owner_user_id="usr_1"
    )

    assert rows == []


def test_slack_fetch_with_no_viewer_id_stays_unfiltered_for_backward_compat(
    monkeypatch,
):
    """Unfiltered here means the mention check specifically (see
    LiveSlackClient's own docstring — dossier prep deliberately
    constructs it this way, relying on its own attendee-based filtering
    downstream instead). Bot-message exclusion below is a SEPARATE,
    unconditional rule that applies regardless — see that test."""

    def fake_call_tool(spec, tool_name, arguments):
        if tool_name == "slack_list_channels":
            return {"channels": [{"id": "C1"}]}
        return {
            "ok": True,
            "messages": [{"ts": "1.1", "user": "U1", "text": "no mention here"}],
        }

    monkeypatch.setattr(
        "app.ingest.live_source.slack.McpSession", _fake_mcp_session(fake_call_tool)
    )
    monkeypatch.setattr("app.ingest.live_source.slack.WebClient", _FakeSlackWebClient)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-fake")

    rows = LiveSlackClient().fetch(window=None, owner_user_id="usr_1")

    assert len(rows) == 1


def test_slack_fetch_excludes_bot_messages_even_with_no_viewer_id(monkeypatch):
    """REAL CHANGE (requested: "remove the slack DMs and message from the
    bot itself ... they don't count as real messages"). Previously this
    exclusion only ever ran when viewer_slack_user_id was set (nested
    inside the mention check) — meaning dossier prep's two call sites,
    which deliberately construct LiveSlackClient with no viewer id (see
    that param's own docstring), got zero bot-message filtering at all.
    Now unconditional."""

    def fake_call_tool(spec, tool_name, arguments):
        if tool_name == "slack_list_channels":
            return {"channels": [{"id": "C1"}]}
        return {
            "ok": True,
            "messages": [
                {"ts": "1.1", "user": "UBOT", "bot_id": "B0BOT", "text": "automated notice"},
                {"ts": "2.2", "user": "U1", "text": "a real human message"},
            ],
        }

    monkeypatch.setattr(
        "app.ingest.live_source.slack.McpSession", _fake_mcp_session(fake_call_tool)
    )
    monkeypatch.setattr("app.ingest.live_source.slack.WebClient", _FakeSlackWebClient)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-fake")

    rows = LiveSlackClient().fetch(window=None, owner_user_id="usr_1")

    assert len(rows) == 1
    assert rows[0]["external_id"] == "2.2"


def test_slack_fetch_resolves_channel_and_sender_names(monkeypatch):
    """REAL CHANGE (requested: "resolve user ids and channel ids in
    slack to the username and channel name") — end to end through
    fetch(): the channel name comes free from slack_list_channels' own
    response; the sender name needs the extra users_info call
    _FakeSlackWebClient.users_info stands in for."""

    def fake_call_tool(spec, tool_name, arguments):
        if tool_name == "slack_list_channels":
            return {"channels": [{"id": "C1", "name": "general"}]}
        return {
            "ok": True,
            "messages": [{"ts": "1.1", "user": "U12345", "text": "hi"}],
        }

    monkeypatch.setattr(
        "app.ingest.live_source.slack.McpSession", _fake_mcp_session(fake_call_tool)
    )
    monkeypatch.setattr("app.ingest.live_source.slack.WebClient", _FakeSlackWebClient)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-fake")

    rows = LiveSlackClient().fetch(window=None, owner_user_id="usr_1")

    assert len(rows) == 1
    assert rows[0]["channel_name"] == "general"
    assert rows[0]["sender_display_name"] == "Display Name U12345"


def test_slack_fetch_caches_sender_name_lookups_within_one_fetch(monkeypatch):
    """The same person posting several messages in one poll must cost one
    real users.info call, not one per message."""
    lookups = []

    def fake_call_tool(spec, tool_name, arguments):
        if tool_name == "slack_list_channels":
            return {"channels": [{"id": "C1", "name": "general"}]}
        return {
            "ok": True,
            "messages": [
                {"ts": "1.1", "user": "U12345", "text": "one"},
                {"ts": "2.2", "user": "U12345", "text": "two"},
            ],
        }

    class _CountingFakeSlackWebClient(_FakeSlackWebClient):
        def users_info(self, user):
            lookups.append(user)
            return super().users_info(user)

    monkeypatch.setattr(
        "app.ingest.live_source.slack.McpSession", _fake_mcp_session(fake_call_tool)
    )
    monkeypatch.setattr(
        "app.ingest.live_source.slack.WebClient", _CountingFakeSlackWebClient
    )
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-fake")

    rows = LiveSlackClient().fetch(window=None, owner_user_id="usr_1")

    assert len(rows) == 2
    assert all(r["sender_display_name"] == "Display Name U12345" for r in rows)
    assert lookups == ["U12345"]  # one call, not two


def test_resolve_sender_name_returns_none_on_a_failed_lookup(monkeypatch):
    class _FailingWebClient:
        def __init__(self, token):
            pass

        def users_info(self, user):
            raise RuntimeError("simulated Slack API failure")

    monkeypatch.setattr("app.ingest.live_source.slack.WebClient", _FailingWebClient)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-fake")

    client = LiveSlackClient()
    result = client._resolve_sender_name("U12345", {})

    assert result is None


def test_resolve_slack_mentions_for_display_replaces_raw_tags(monkeypatch):
    """REAL BUG FOUND AND FIXED (reported live: "the slack mentions from
    the agent aren't eliminated") — a raw <@U12345> tag must never reach
    the user; it's replaced with a readable @DisplayName."""
    monkeypatch.setattr("app.ingest.live_source.slack.WebClient", _FakeSlackWebClient)

    result = resolve_slack_mentions_for_display(
        "<@U12345> can you review this?", "xoxb-fake"
    )

    assert result == "@Display Name U12345 can you review this?"
    assert "<@" not in result


def test_resolve_slack_mentions_for_display_resolves_each_id_once(monkeypatch):
    lookups = []

    class _CountingFakeSlackWebClient(_FakeSlackWebClient):
        def users_info(self, user):
            lookups.append(user)
            return super().users_info(user)

    monkeypatch.setattr(
        "app.ingest.live_source.slack.WebClient", _CountingFakeSlackWebClient
    )

    result = resolve_slack_mentions_for_display(
        "<@U1> and <@U1> and <@U2>", "xoxb-fake"
    )

    assert result == "@Display Name U1 and @Display Name U1 and @Display Name U2"
    assert lookups == ["U1", "U2"]  # each distinct id resolved once


def test_resolve_slack_mentions_for_display_falls_back_on_lookup_failure(monkeypatch):
    class _FailingWebClient:
        def __init__(self, token):
            pass

        def users_info(self, user):
            raise RuntimeError("simulated Slack API failure")

    monkeypatch.setattr("app.ingest.live_source.slack.WebClient", _FailingWebClient)

    result = resolve_slack_mentions_for_display("<@U12345> ping", "xoxb-fake")

    assert result == "@someone ping"
    assert "<@" not in result


def test_resolve_slack_mentions_for_display_is_a_noop_without_mentions(monkeypatch):
    def _boom(*args, **kwargs):
        raise AssertionError("should never call Slack when there's no mention")

    monkeypatch.setattr("app.ingest.live_source.slack.WebClient", _boom)

    result = resolve_slack_mentions_for_display("no mentions here", "xoxb-fake")

    assert result == "no mentions here"


def test_linear_fetch_unwraps_graphql_connection_shape(monkeypatch):
    """Real linear_search_issues response (confirmed live): {"issues":
    {"pageInfo": {...}, "nodes": [...]}} — a GraphQL connection, not a flat
    {"issues": [...]}. Must unwrap to "nodes", not iterate the connection
    dict's own keys (pageInfo, nodes) as if they were issues."""

    def fake_call_tool(spec, tool_name, arguments):
        return {
            "issues": {
                "pageInfo": {"hasNextPage": False, "endCursor": "abc"},
                "nodes": [
                    {"id": "uuid-1", "identifier": "MEN-1", "title": "Only issue"}
                ],
            }
        }

    monkeypatch.setattr("app.ingest.live_source.linear.call_tool", fake_call_tool)
    monkeypatch.setenv("LINEAR_API_KEY", "lin_api_fake")

    rows = LiveLinearClient().fetch(window=None, owner_user_id="usr_1")

    assert len(rows) == 1
    assert rows[0]["external_id"] == "MEN-1"


def test_adapt_google_docs_comment_maps_the_real_flattened_shape():
    """Real listComments response (confirmed live via an add-comment ->
    listComments/getComment -> delete round-trip): pre-flattened like
    Jira's server — author is a plain display-name string, no email or
    accountId, no per-comment permalink."""
    raw = {
        "id": "AAACFeCK9h0",
        "author": "Amal Bahri",
        "content": "mentor-agent connector probe (safe to delete)",
        "quotedText": "I",
        "resolved": False,
        "createdTime": "2026-08-11T10:16:52.060Z",
        "replyCount": 0,
    }

    row = _adapt_google_docs_comment(raw, document_id="doc-123")

    assert row["external_id"] == "doc-123:AAACFeCK9h0"
    assert row["source"] == "google_docs"
    assert row["channel"] == "doc-123"
    assert row["sent_at"] == "2026-08-11T10:16:52.060Z"
    assert row["url"] == "https://docs.google.com/document/d/doc-123/edit"
    assert row["is_dm"] is False
    assert row["body_ref"] == "gdocs://doc-123/AAACFeCK9h0"
    assert row["actor_reference_key"] == "google_docs:Amal Bahri"
    assert "content" not in row
    assert "quotedText" not in row


def test_adapt_google_docs_comment_falls_back_to_comment_id_when_no_author():
    raw = {"id": "c-1", "createdTime": "2026-08-11T10:00:00Z"}

    row = _adapt_google_docs_comment(raw, document_id="doc-9")

    assert row["actor_reference_key"] == "google_docs:c-1"


def test_google_docs_client_health_reflects_credential_presence(monkeypatch):
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_CLIENT_SECRET", raising=False)
    # No-profile client also falls back to the Desktop-client vars.
    monkeypatch.delenv("GMAIL_MCP_CLIENT_ID", raising=False)
    monkeypatch.delenv("GMAIL_MCP_CLIENT_SECRET", raising=False)
    client = LiveGoogleDocsClient()
    assert isinstance(client.health(), Unauthorized)

    monkeypatch.setenv("GOOGLE_CLIENT_ID", "fake-client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "fake-client-secret")
    client = LiveGoogleDocsClient()
    assert isinstance(client.health(), Healthy)


def test_google_docs_fetch_lists_docs_then_comments_per_doc_and_filters_resolved(
    monkeypatch,
):
    calls = []

    def fake_call_tool(spec, tool_name, arguments):
        calls.append((tool_name, arguments))
        if tool_name == "listDriveFiles":
            return {"files": [{"id": "doc-A"}, {"id": "doc-B"}]}
        if arguments["documentId"] == "doc-A":
            return {
                "comments": [
                    {"id": "c1", "author": "Sarah", "resolved": False},
                    {"id": "c2", "author": "Marc", "resolved": True},
                ]
            }
        return {"comments": [{"id": "c3", "author": "Sarah", "resolved": False}]}

    monkeypatch.setattr(
        "app.ingest.live_source.google_docs.McpSession", _fake_mcp_session(fake_call_tool)
    )
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "fake-client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "fake-client-secret")

    rows = LiveGoogleDocsClient().fetch(window=None, owner_user_id="usr_1")

    assert calls[0] == ("listDriveFiles", {"mimeType": "document", "maxResults": 6})
    assert len(rows) == 2
    assert {r["external_id"] for r in rows} == {"doc-A:c1", "doc-B:c3"}


def test_adapt_gmail_message_maps_the_real_flattened_shape():
    """Real triageInbox response (confirmed live via a real inbox round-
    trip): messages is a flat list, from is "Display Name <email>", date
    is an RFC 2822 header string, and content (subject/snippet/
    bodyExcerpt) must never survive into the normalized row (AGENT.md
    privacy rule) — fetch() also asks the server for bodyExcerptLength=0
    so it never even sends that content over MCP."""
    raw = {
        "id": "19ffaf6d4a12ffc6",
        "threadId": "19ffaf6d4a12ffc7",
        "from": "Morning Brew <crew@morningbrew.com>",
        "domain": "morningbrew.com",
        "to": "amal@example.com",
        "subject": "What about Bob",
        "date": "Thu, 13 Aug 2026 05:17:56 -0400 (EDT)",
        "snippet": "The Lakers are getting a new owner again...",
        "bodyExcerpt": "The Lakers are getting a new owner again...",
        "labels": ["UNREAD", "INBOX"],
        "isNewsletter": True,
        "containsMeetingReference": True,
        "containsQuestion": True,
        "actionRequested": True,
    }

    row = _adapt_gmail_message(raw)

    assert row["external_id"] == "19ffaf6d4a12ffc6"
    assert row["source"] == "gmail"
    assert row["channel"] == "inbox"
    assert row["sent_at"] == "2026-08-13T05:17:56-04:00"
    assert row["url"] == "https://mail.google.com/mail/u/0/#inbox/19ffaf6d4a12ffc7"
    assert row["is_dm"] is False
    assert row["body_ref"] == "gmail://19ffaf6d4a12ffc6"
    assert row["actor_reference_key"] == "gmail:crew@morningbrew.com"
    assert row["action_requested"] is True
    assert "subject" not in row
    assert "snippet" not in row
    assert "bodyExcerpt" not in row


def test_adapt_gmail_message_falls_back_to_the_raw_from_header_without_brackets():
    raw = {"id": "m-1", "threadId": "t-1", "from": "noreply@example.com"}

    row = _adapt_gmail_message(raw)

    assert row["actor_reference_key"] == "gmail:noreply@example.com"


def test_adapt_gmail_message_handles_a_missing_or_unparseable_date():
    raw = {
        "id": "m-1",
        "threadId": "t-1",
        "from": "a@example.com",
        "date": "not-a-date",
    }

    row = _adapt_gmail_message(raw)

    assert row["sent_at"] is None


def test_adapt_gmail_message_defaults_action_requested_to_false():
    raw = {"id": "m-1", "threadId": "t-1", "from": "a@example.com"}

    row = _adapt_gmail_message(raw)

    assert row["action_requested"] is False


def test_is_gmail_noise_flags_newsletters():
    assert _is_gmail_noise({"isNewsletter": True, "labels": ["INBOX"]}) is True


def test_is_gmail_noise_flags_promotions_social_and_updates_labels():
    assert _is_gmail_noise({"labels": ["INBOX", "CATEGORY_PROMOTIONS"]}) is True
    assert _is_gmail_noise({"labels": ["INBOX", "CATEGORY_SOCIAL"]}) is True
    assert _is_gmail_noise({"labels": ["INBOX", "CATEGORY_UPDATES"]}) is True


def test_is_gmail_noise_false_for_ordinary_inbox_mail():
    assert (
        _is_gmail_noise({"isNewsletter": False, "labels": ["INBOX", "UNREAD"]}) is False
    )


def test_gmail_client_health_reflects_credential_presence(monkeypatch):
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_CLIENT_SECRET", raising=False)
    # No-profile client also falls back to the Desktop-client vars.
    monkeypatch.delenv("GMAIL_MCP_CLIENT_ID", raising=False)
    monkeypatch.delenv("GMAIL_MCP_CLIENT_SECRET", raising=False)
    client = LiveGmailClient()
    assert isinstance(client.health(), Unauthorized)

    monkeypatch.setenv("GOOGLE_CLIENT_ID", "fake-client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "fake-client-secret")
    client = LiveGmailClient()
    assert isinstance(client.health(), Healthy)


def test_gmail_fetch_scopes_the_query_to_the_window_and_unwraps_messages(monkeypatch):
    calls = []

    def fake_call_tool(spec, tool_name, arguments):
        calls.append((tool_name, arguments))
        return {
            "summary": {"totalUnread": 2},
            "messages": [
                {"id": "m-1", "threadId": "t-1", "from": "a@example.com"},
                {
                    "id": "m-2",
                    "threadId": "t-2",
                    "from": "newsletter@example.com",
                    "isNewsletter": True,
                },
            ],
        }

    monkeypatch.setattr(
        "app.ingest.live_source.gmail.McpSession", _fake_mcp_session(fake_call_tool)
    )
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "fake-client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "fake-client-secret")

    window = Window(
        start=datetime.datetime(2026, 8, 13, 0, 0, 0),
        end=datetime.datetime(2026, 8, 13, 23, 59, 59),
    )
    rows = LiveGmailClient().fetch(window=window, owner_user_id="usr_1")

    assert calls[0][0] == "triageInbox"
    # after:<unix-seconds>, not newer_than:Nd — real timestamp precision,
    # not day-granularity, is what makes seed_live's narrowed incremental
    # window actually incremental (see LiveGmailClient.fetch's own
    # comment). Computed the same way the code does rather than a
    # hardcoded epoch, since window.start here is naive and .timestamp()
    # on a naive datetime is local-timezone-dependent.
    assert calls[0][1]["additionalQuery"] == f"after:{int(window.start.timestamp())}"
    # >0 on purpose — see LiveGmailClient's docstring: the installed
    # package's classification regexes only see real content when
    # bodyExcerptLength > 0, and the excerpt itself is discarded by
    # _adapt_gmail_message regardless (AGENT.md privacy rule).
    assert calls[0][1]["bodyExcerptLength"] > 0
    # m-2 is a newsletter — filtered out before it ever reaches the scorer.
    assert len(rows) == 1
    assert rows[0]["external_id"] == "m-1"


def test_clients_report_their_source_name():
    assert LiveCalendarClient().source == "calendar"
    assert LiveSlackClient().source == "slack"
    assert LiveLinearClient().source == "linear"
    assert LiveJiraClient().source == "jira"
    assert LiveGoogleDocsClient().source == "google_docs"
    assert LiveGmailClient().source == "gmail"


# --- Notion (real shapes confirmed live this session) -----------------------

_RAW_OBJECTIVE_PAGE = {
    "object": "page",
    "id": "obj-1",
    "properties": {
        "Objective Name": {
            "type": "title",
            "title": [{"plain_text": "Ship the mentor agent"}],
        },
        "Status": {"type": "select", "select": {"name": "On Track"}},
        "Quarter": {"type": "select", "select": {"name": "Q3"}},
        "Owner": {
            "type": "people",
            "people": [{"person": {"email": "mentee@example.com"}}],
        },
        "Overall Progress ": {
            "type": "rollup",
            "rollup": {
                "type": "array",
                "function": "show_original",
                "array": [
                    {"type": "formula", "formula": {"type": "number", "number": 0.4}},
                    {"type": "formula", "formula": {"type": "number", "number": 0.6}},
                ],
            },
        },
    },
}

_RAW_KEY_RESULT_PAGE = {
    "object": "page",
    "id": "kr-1",
    "properties": {
        "Key Result Name": {
            "type": "title",
            "title": [{"plain_text": "Sponsor 2 engineers"}],
        },
        "Current Value": {"type": "number", "number": 1},
        "Target Value": {"type": "number", "number": 2},
        "Progress": {"type": "formula", "formula": {"type": "number", "number": 0.5}},
        "Objective": {"type": "relation", "relation": [{"id": "obj-1"}]},
    },
}

_RAW_CAREER_GOAL_PAGE = {
    "object": "page",
    "id": "cg-1",
    "properties": {
        "Career Goal": {
            "type": "title",
            "title": [{"plain_text": "Become a tech lead"}],
        },
        "Objectives": {"type": "relation", "relation": [{"id": "obj-1"}]},
    },
}

_RAW_ONE_ON_ONE_NOTE_PAGE = {
    "object": "page",
    "id": "note-1",
    "properties": {
        "Meeting Note": {
            "type": "title",
            "title": [{"plain_text": "1:1: check-in"}],
        },
        "Key Results": {"type": "relation", "relation": [{"id": "kr-1"}]},
    },
}


def test_adapt_notion_objective_maps_fields_and_averages_rollup_array():
    """Overall Progress's rollup comes back as the underlying array of
    per-related-row formula numbers (function "show_original"), not a
    server-computed aggregate — confirmed live."""
    row = _adapt_notion_objective(_RAW_OBJECTIVE_PAGE)

    assert row["external_id"] == "obj-1"
    assert row["goal_type"] == "objective"
    assert row["title"] == "Ship the mentor agent"
    assert row["status"] == "On Track"
    assert row["quarter"] == "Q3"
    assert row["progress"] == 0.5
    assert row["parent_external_id"] is None


def test_notion_rollup_progress_returns_none_for_empty_array():
    assert _notion_rollup_progress({"rollup": {"array": []}}) is None
    assert _notion_rollup_progress(None) is None


def test_adapt_notion_key_result_maps_fields_and_parent_objective():
    row = _adapt_notion_key_result(_RAW_KEY_RESULT_PAGE)

    assert row["external_id"] == "kr-1"
    assert row["goal_type"] == "key_result"
    assert row["title"] == "Sponsor 2 engineers"
    assert row["current_value"] == 1
    assert row["target_value"] == 2
    assert row["progress"] == 0.5
    assert row["parent_external_id"] == "obj-1"


def test_adapt_notion_career_goal_maps_fields_and_parent_objective():
    row = _adapt_notion_career_goal(_RAW_CAREER_GOAL_PAGE)

    assert row["external_id"] == "cg-1"
    assert row["goal_type"] == "career_goal"
    assert row["title"] == "Become a tech lead"
    assert row["parent_external_id"] == "obj-1"


def test_adapt_notion_one_on_one_note_maps_fields_and_never_stores_content():
    row = _adapt_notion_one_on_one_note(_RAW_ONE_ON_ONE_NOTE_PAGE)

    assert row["external_id"] == "note-1"
    assert row["title"] == "1:1: check-in"
    assert json.loads(row["linked_key_result_external_ids"]) == ["kr-1"]


def test_notion_goals_client_health_reflects_credential_presence(monkeypatch):
    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    assert isinstance(LiveNotionGoalsClient().health(), Unauthorized)

    monkeypatch.setenv("NOTION_TOKEN", "fake-token")
    assert isinstance(LiveNotionGoalsClient().health(), Healthy)


def test_notion_goals_client_returns_empty_without_a_linked_owner_email(monkeypatch):
    """No `mentor link-notion` yet run for this user — nothing to
    attribute, not an error (same shape as an unset credential)."""
    monkeypatch.setenv("NOTION_TOKEN", "fake-token")
    rows = LiveNotionGoalsClient(notion_owner_email=None).fetch(
        window=None, owner_user_id="usr_1"
    )
    assert rows == []


def _fake_notion_call_tool(spec, tool_name, arguments):
    if tool_name == "API-post-search":
        by_title = {
            "Objectives": "objectives-ds",
            "Key Results": "key-results-ds",
            "Career Goals": "career-goals-ds",
        }
        ds_id = by_title.get(arguments["query"])
        if ds_id is None:
            return {"results": []}
        return {
            "results": [
                {
                    "object": "data_source",
                    "id": ds_id,
                    "title": [{"plain_text": arguments["query"]}],
                }
            ]
        }
    if tool_name == "API-query-data-source":
        by_ds = {
            "objectives-ds": [_RAW_OBJECTIVE_PAGE],
            "key-results-ds": [_RAW_KEY_RESULT_PAGE],
            "career-goals-ds": [_RAW_CAREER_GOAL_PAGE],
        }
        return {
            "results": by_ds.get(arguments["data_source_id"], []),
            "has_more": False,
        }
    raise AssertionError(f"unexpected tool call: {tool_name}")


def test_notion_goals_client_fetch_filters_by_owner_email_via_objectives(monkeypatch):
    """Career Goals/Key Results have no Owner field of their own (confirmed
    live) — ownership is resolved transitively: an Objective is kept only
    if its Owner email matches, then Key Results/Career Goals are kept
    only if they relate to an already-owned Objective."""
    monkeypatch.setattr(
        "app.ingest.live_source.notion.McpSession", _fake_mcp_session(_fake_notion_call_tool)
    )
    monkeypatch.setenv("NOTION_TOKEN", "fake-token")

    rows = LiveNotionGoalsClient(notion_owner_email="mentee@example.com").fetch(
        window=None, owner_user_id="usr_1"
    )

    by_type = {r["goal_type"]: r for r in rows}
    assert set(by_type) == {"objective", "key_result", "career_goal"}
    assert by_type["objective"]["external_id"] == "obj-1"
    assert by_type["key_result"]["external_id"] == "kr-1"
    assert by_type["career_goal"]["external_id"] == "cg-1"


def test_notion_goals_client_fetch_excludes_unowned_objectives(monkeypatch):
    monkeypatch.setattr(
        "app.ingest.live_source.notion.McpSession", _fake_mcp_session(_fake_notion_call_tool)
    )
    monkeypatch.setenv("NOTION_TOKEN", "fake-token")

    rows = LiveNotionGoalsClient(notion_owner_email="someone-else@example.com").fetch(
        window=None, owner_user_id="usr_1"
    )

    assert rows == []


_RAW_CAREER_GOAL_PAGE_OTHER_USER = {
    "object": "page",
    "id": "cg-other",
    "properties": {
        "Career Goal": {
            "type": "title",
            "title": [{"plain_text": "Someone else's career goal"}],
        },
        "Objectives": {"type": "relation", "relation": [{"id": "obj-other"}]},
    },
}


def _fake_notion_call_tool_multiple_career_goals_dbs(spec, tool_name, arguments):
    """Real, live-confirmed shape: two separate private "Career Goals"
    dbs with the identical title, one per user."""
    if tool_name == "API-post-search":
        if arguments["query"] == "Career Goals":
            return {
                "results": [
                    {
                        "object": "data_source",
                        "id": "career-goals-ds-other-user",
                        "title": [{"plain_text": "Career Goals"}],
                    },
                    {
                        "object": "data_source",
                        "id": "career-goals-ds-mentee",
                        "title": [{"plain_text": "Career Goals"}],
                    },
                ]
            }
        by_title = {"Objectives": "objectives-ds", "Key Results": "key-results-ds"}
        ds_id = by_title.get(arguments["query"])
        if ds_id is None:
            return {"results": []}
        return {
            "results": [
                {
                    "object": "data_source",
                    "id": ds_id,
                    "title": [{"plain_text": arguments["query"]}],
                }
            ]
        }
    if tool_name == "API-query-data-source":
        by_ds = {
            "objectives-ds": [_RAW_OBJECTIVE_PAGE],
            "key-results-ds": [_RAW_KEY_RESULT_PAGE],
            "career-goals-ds-other-user": [_RAW_CAREER_GOAL_PAGE_OTHER_USER],
            "career-goals-ds-mentee": [_RAW_CAREER_GOAL_PAGE],
        }
        return {
            "results": by_ds.get(arguments["data_source_id"], []),
            "has_more": False,
        }
    raise AssertionError(f"unexpected tool call: {tool_name}")


def test_notion_goals_client_fetch_finds_career_goals_across_multiple_dbs_with_the_same_title(
    monkeypatch,
):
    """Real, live-caught bug this session: Career Goals can be a separate
    private db per user sharing an identical title (unlike Objectives/
    Key Results/1:1 Notes, confirmed singular) — a single-match title
    resolver silently picked whichever db search happened to return
    first, dropping the mentee's real career goals whenever that wasn't
    theirs. Fixed by querying every matching db and relying on the
    existing Objectives-relation ownership filter to sort out which rows
    are real."""
    monkeypatch.setattr(
        "app.ingest.live_source.notion.McpSession",
        _fake_mcp_session(_fake_notion_call_tool_multiple_career_goals_dbs),
    )
    monkeypatch.setenv("NOTION_TOKEN", "fake-token")

    rows = LiveNotionGoalsClient(notion_owner_email="mentee@example.com").fetch(
        window=None, owner_user_id="usr_1"
    )

    career_goal_rows = [r for r in rows if r["goal_type"] == "career_goal"]
    assert len(career_goal_rows) == 1
    assert career_goal_rows[0]["external_id"] == "cg-1"


def _fake_notion_notes_call_tool(spec, tool_name, arguments):
    if tool_name == "API-post-search":
        by_title = {
            "Objectives": "objectives-ds",
            "Key Results": "key-results-ds",
            "1:1 Notes": "notes-ds",
        }
        ds_id = by_title.get(arguments["query"])
        if ds_id is None:
            return {"results": []}
        return {
            "results": [
                {
                    "object": "data_source",
                    "id": ds_id,
                    "title": [{"plain_text": arguments["query"]}],
                }
            ]
        }
    if tool_name == "API-query-data-source":
        by_ds = {
            "objectives-ds": [_RAW_OBJECTIVE_PAGE],
            "key-results-ds": [_RAW_KEY_RESULT_PAGE],
            "notes-ds": [_RAW_ONE_ON_ONE_NOTE_PAGE],
        }
        return {
            "results": by_ds.get(arguments["data_source_id"], []),
            "has_more": False,
        }
    raise AssertionError(f"unexpected tool call: {tool_name}")


def test_notion_notes_client_fetch_filters_transitively_through_key_results(
    monkeypatch,
):
    monkeypatch.setattr(
        "app.ingest.live_source.notion.McpSession",
        _fake_mcp_session(_fake_notion_notes_call_tool),
    )
    monkeypatch.setenv("NOTION_TOKEN", "fake-token")

    rows = LiveNotionNotesClient(notion_owner_email="mentee@example.com").fetch(
        window=None, owner_user_id="usr_1"
    )

    assert len(rows) == 1
    assert rows[0]["external_id"] == "note-1"


def test_notion_notes_client_fetch_excludes_notes_for_unowned_key_results(
    monkeypatch,
):
    monkeypatch.setattr(
        "app.ingest.live_source.notion.McpSession",
        _fake_mcp_session(_fake_notion_notes_call_tool),
    )
    monkeypatch.setenv("NOTION_TOKEN", "fake-token")

    rows = LiveNotionNotesClient(notion_owner_email="someone-else@example.com").fetch(
        window=None, owner_user_id="usr_1"
    )

    assert rows == []


# --- Notion pair sync (real "Manager" people field, confirmed live) --------

_RAW_OBJECTIVE_WITH_MANAGER_PAGE = {
    "object": "page",
    "id": "obj-managed-1",
    "properties": {
        "Objective Name": {"type": "title", "title": [{"plain_text": "Ship X"}]},
        "Owner": {"type": "people", "people": [{"person": {"email": "report@example.com"}}]},
        "Manager": {
            "type": "people",
            "people": [{"person": {"email": "manager@example.com"}}],
        },
    },
}

_RAW_OBJECTIVE_SELF_OWNED_PAGE = {
    "object": "page",
    "id": "obj-self-owned-1",
    "properties": {
        "Objective Name": {"type": "title", "title": [{"plain_text": "Ship Y"}]},
        "Owner": {"type": "people", "people": [{"person": {"email": "solo@example.com"}}]},
        "Manager": {
            "type": "people",
            "people": [{"person": {"email": "solo@example.com"}}],
        },
    },
}

_RAW_OBJECTIVE_NO_MANAGER_PAGE = {
    "object": "page",
    "id": "obj-no-manager-1",
    "properties": {
        "Objective Name": {"type": "title", "title": [{"plain_text": "Ship Z"}]},
        "Owner": {"type": "people", "people": [{"person": {"email": "solo2@example.com"}}]},
        "Manager": {"type": "people", "people": []},
    },
}


def _fake_notion_pair_call_tool(spec, tool_name, arguments):
    if tool_name == "API-post-search":
        if arguments["query"] != "Objectives":
            return {"results": []}
        return {
            "results": [
                {
                    "object": "data_source",
                    "id": "objectives-ds",
                    "title": [{"plain_text": "Objectives"}],
                }
            ]
        }
    if tool_name == "API-query-data-source":
        assert arguments["data_source_id"] == "objectives-ds"
        return {
            "results": [
                _RAW_OBJECTIVE_WITH_MANAGER_PAGE,
                _RAW_OBJECTIVE_SELF_OWNED_PAGE,
                _RAW_OBJECTIVE_NO_MANAGER_PAGE,
            ],
            "has_more": False,
        }
    raise AssertionError(f"unexpected tool call: {tool_name}")


def test_notion_pair_client_fetch_returns_edges_where_owner_and_manager_differ(
    monkeypatch,
):
    monkeypatch.setattr(
        "app.ingest.live_source.notion.McpSession",
        _fake_mcp_session(_fake_notion_pair_call_tool),
    )
    monkeypatch.setenv("NOTION_TOKEN", "fake-token")

    edges = LiveNotionPairClient().fetch(window=None, owner_user_id="usr_1")

    assert edges == [
        {
            "report_notion_email": "report@example.com",
            "manager_notion_email": "manager@example.com",
        }
    ]


def test_notion_pair_client_fetch_deduplicates_identical_edges(monkeypatch):
    def fake_call_tool(spec, tool_name, arguments):
        if tool_name == "API-post-search":
            return {
                "results": [
                    {
                        "object": "data_source",
                        "id": "objectives-ds",
                        "title": [{"plain_text": "Objectives"}],
                    }
                ]
            }
        assert tool_name == "API-query-data-source"
        return {
            "results": [
                _RAW_OBJECTIVE_WITH_MANAGER_PAGE,
                {**_RAW_OBJECTIVE_WITH_MANAGER_PAGE, "id": "obj-managed-2"},
            ],
            "has_more": False,
        }

    monkeypatch.setattr(
        "app.ingest.live_source.notion.McpSession", _fake_mcp_session(fake_call_tool)
    )
    monkeypatch.setenv("NOTION_TOKEN", "fake-token")

    edges = LiveNotionPairClient().fetch(window=None, owner_user_id="usr_1")

    assert len(edges) == 1


def test_notion_pair_client_health_reflects_credential_presence(monkeypatch):
    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    assert isinstance(LiveNotionPairClient().health(), Unauthorized)

    monkeypatch.setenv("NOTION_TOKEN", "fake-token")
    assert isinstance(LiveNotionPairClient().health(), Healthy)


_RAW_OBJECTIVE_WITH_MANAGER_PAGE_AND_IDS = {
    "object": "page",
    "id": "obj-managed-ids-1",
    "properties": {
        "Objective Name": {"type": "title", "title": [{"plain_text": "Ship X"}]},
        "Owner": {
            "type": "people",
            "people": [
                {
                    "id": "person-report-1",
                    "name": "Report One",
                    "person": {"email": "report@example.com"},
                }
            ],
        },
        "Manager": {
            "type": "people",
            "people": [
                {
                    "id": "person-manager-1",
                    "name": "Manager One",
                    "person": {"email": "manager@example.com"},
                }
            ],
        },
    },
}


def test_notion_pair_client_fetch_directory_returns_distinct_people(monkeypatch):
    def fake_call_tool(spec, tool_name, arguments):
        if tool_name == "API-post-search":
            return {
                "results": [
                    {
                        "object": "data_source",
                        "id": "objectives-ds",
                        "title": [{"plain_text": "Objectives"}],
                    }
                ]
            }
        assert tool_name == "API-query-data-source"
        return {
            "results": [
                _RAW_OBJECTIVE_WITH_MANAGER_PAGE_AND_IDS,
                # A second Objective owned solely by the same report, no
                # manager set — Owner should still only appear once.
                {
                    "object": "page",
                    "id": "obj-no-manager-ids-1",
                    "properties": {
                        "Objective Name": {
                            "type": "title",
                            "title": [{"plain_text": "Ship W"}],
                        },
                        "Owner": {
                            "type": "people",
                            "people": [
                                {
                                    "id": "person-report-1",
                                    "name": "Report One",
                                    "person": {"email": "report@example.com"},
                                }
                            ],
                        },
                        "Manager": {"type": "people", "people": []},
                    },
                },
            ],
            "has_more": False,
        }

    monkeypatch.setattr(
        "app.ingest.live_source.notion.McpSession", _fake_mcp_session(fake_call_tool)
    )
    monkeypatch.setenv("NOTION_TOKEN", "fake-token")

    directory = LiveNotionPairClient().fetch_directory()

    assert sorted(directory, key=lambda d: d["notion_person_id"]) == [
        {
            "notion_person_id": "person-manager-1",
            "email": "manager@example.com",
            "display_name": "Manager One",
        },
        {
            "notion_person_id": "person-report-1",
            "email": "report@example.com",
            "display_name": "Report One",
        },
    ]


def test_notion_pair_client_fetch_active_member_ids_filters_to_person_type(
    monkeypatch,
):
    def fake_call_tool(spec, tool_name, arguments):
        assert tool_name == "API-get-users"
        return {
            "results": [
                {"object": "user", "id": "person-1", "type": "person"},
                {"object": "user", "id": "person-2", "type": "person"},
                {"object": "user", "id": "bot-1", "type": "bot"},
            ]
        }

    monkeypatch.setattr(
        "app.ingest.live_source.notion.McpSession", _fake_mcp_session(fake_call_tool)
    )
    monkeypatch.setenv("NOTION_TOKEN", "fake-token")

    member_ids = LiveNotionPairClient().fetch_active_member_ids()

    assert member_ids == {"person-1", "person-2"}


def test_notion_pair_client_fetch_active_member_ids_returns_none_on_failure(
    monkeypatch,
):
    def fake_call_tool(spec, tool_name, arguments):
        raise RuntimeError("boom")

    monkeypatch.setattr(
        "app.ingest.live_source.notion.McpSession", _fake_mcp_session(fake_call_tool)
    )
    monkeypatch.setenv("NOTION_TOKEN", "fake-token")

    assert LiveNotionPairClient().fetch_active_member_ids() is None


def test_notion_pair_client_fetch_active_member_ids_returns_none_on_notion_error_shape(
    monkeypatch,
):
    """Regression test for a real bug found live: Notion's error response
    is itself a dict ({"object": "error", "status": 401, "message": ...}
    — this exact shape was observed live this session) with no "results"
    key. The old code's `result.get("results", result)` fallback treated
    that error dict AS IF it were the results list, iterated its string
    keys, and silently produced an empty set — indistinguishable from
    "the workspace genuinely has zero members" — which fed straight into
    revoke_departed_notion_users mass-revoking every real linked user.
    Must return None (unknown/failed), not set()."""

    def fake_call_tool(spec, tool_name, arguments):
        return {
            "status": 401,
            "object": "error",
            "code": "unauthorized",
            "message": "API token is invalid.",
            "request_id": "fake-request-id",
        }

    monkeypatch.setattr(
        "app.ingest.live_source.notion.McpSession", _fake_mcp_session(fake_call_tool)
    )
    monkeypatch.setenv("NOTION_TOKEN", "fake-token")

    assert LiveNotionPairClient().fetch_active_member_ids() is None


def test_notion_pair_client_fetch_active_member_ids_returns_none_when_results_key_missing(
    monkeypatch,
):
    """A dict response with no "object": "error" flag but also no usable
    "results" list (any other unexpected shape) must still fail closed."""

    def fake_call_tool(spec, tool_name, arguments):
        return {"unexpected": "shape"}

    monkeypatch.setattr(
        "app.ingest.live_source.notion.McpSession", _fake_mcp_session(fake_call_tool)
    )
    monkeypatch.setenv("NOTION_TOKEN", "fake-token")

    assert LiveNotionPairClient().fetch_active_member_ids() is None


# --- Fathom (unverified response shape — see LiveFathomClient's docstring) --


def test_adapt_fathom_transcript_handles_structured_segments():
    raw = {
        "transcript": [
            {"speaker": {"display_name": "Amal"}, "text": "Let's start."},
            {"speaker": {"display_name": "Mentee"}, "text": "Sounds good."},
        ]
    }
    assert _adapt_fathom_transcript(raw) == "Amal: Let's start.\nMentee: Sounds good."


def test_adapt_fathom_transcript_handles_plain_string():
    """Confirmed live: list_meetings returned prose ("Found 0
    meeting(s).") for its empty case, not JSON — get_meeting_transcript
    may do the same for a non-empty one."""
    assert _adapt_fathom_transcript("Full transcript text here.") == (
        "Full transcript text here."
    )


def test_adapt_fathom_transcript_returns_none_for_empty_or_unknown_shapes():
    assert _adapt_fathom_transcript("") is None
    assert _adapt_fathom_transcript({}) is None
    assert _adapt_fathom_transcript(None) is None
    assert _adapt_fathom_transcript(42) is None


def test_fathom_client_health_reflects_token_presence(tmp_path):
    empty_storage = FileTokenStorage(tmp_path / "empty.json")
    assert isinstance(LiveFathomClient(empty_storage).health(), Unauthorized)

    from mcp.shared.auth import OAuthToken

    populated_storage = FileTokenStorage(tmp_path / "populated.json")
    import asyncio

    asyncio.run(
        populated_storage.set_tokens(OAuthToken(access_token="x", token_type="Bearer"))
    )
    assert isinstance(LiveFathomClient(populated_storage).health(), Healthy)


class _FakeHttpMcpSession:
    def __init__(self, spec, fake_call_tool):
        self._spec = spec
        self._fake_call_tool = fake_call_tool

    def __enter__(self):
        return self

    def call(self, tool_name, arguments, timeout=None):
        return self._fake_call_tool(self._spec, tool_name, arguments)

    def __exit__(self, exc_type, exc, tb):
        return False


def _fake_http_mcp_session(fake_call_tool):
    return lambda spec: _FakeHttpMcpSession(spec, fake_call_tool)


def test_fathom_client_find_transcript_returns_none_on_empty_prose_result(
    monkeypatch, tmp_path
):
    def fake_call_tool(spec, tool_name, arguments):
        assert tool_name == "list_meetings"
        return "Found 0 meeting(s)."

    monkeypatch.setattr(
        "app.ingest.live_source.fathom.HttpMcpSession",
        _fake_http_mcp_session(fake_call_tool),
    )

    result = LiveFathomClient(FileTokenStorage(tmp_path / "token.json")).find_transcript(
        window=Window(
            start=datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC),
            end=datetime.datetime(2026, 1, 2, tzinfo=datetime.UTC),
        )
    )

    assert result is None


def test_fathom_client_find_transcript_fetches_the_first_matching_recording(
    monkeypatch, tmp_path
):
    calls = []

    def fake_call_tool(spec, tool_name, arguments):
        calls.append((tool_name, arguments))
        if tool_name == "list_meetings":
            return {"meetings": [{"recording_id": 42, "title": "1:1"}]}
        assert tool_name == "get_meeting_transcript"
        assert arguments == {"recording_id": 42}
        return "Amal: hello."

    monkeypatch.setattr(
        "app.ingest.live_source.fathom.HttpMcpSession",
        _fake_http_mcp_session(fake_call_tool),
    )

    result = LiveFathomClient(FileTokenStorage(tmp_path / "token.json")).find_transcript(
        window=Window(
            start=datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC),
            end=datetime.datetime(2026, 1, 2, tzinfo=datetime.UTC),
        ),
        organizer_email="amal@example.com",
    )

    assert result == "Amal: hello."
    list_call = next(c for c in calls if c[0] == "list_meetings")
    assert list_call[1]["recorded_by"] == ["amal@example.com"]

