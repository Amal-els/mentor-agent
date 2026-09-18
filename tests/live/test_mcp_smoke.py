"""One-shot live smoke tests for the MCP-backed Calendar/Slack/Linear/Jira/
Google Docs connectors. Deselected by default (pyproject.toml's `addopts = "-m 'not
live'"`) — run explicitly with `uv run pytest -m live` after setting the
relevant credential env var(s). Each test is independently skipped if its
own service's credentials aren't configured, so partial setup (e.g. Slack
only) still runs what it can.

Until each of these has passed at least once against a real server, treat
that connector's response adapter in app/ingest/live_source.py as
unreviewed code — see app/tools/mcp_config.py's module docstring for why:
tool names/argument shapes came from live introspection (no credentials
needed for that), but response *shapes* were never actually observed."""

import datetime

import pytest

from app.ingest.base import Window
from app.ingest.live_source import (
    LiveCalendarClient,
    LiveGoogleDocsClient,
    LiveJiraClient,
    LiveLinearClient,
    LiveSlackClient,
)

pytestmark = pytest.mark.live

_TODAY_WINDOW = Window(
    start=datetime.datetime.now(datetime.UTC).replace(
        hour=0, minute=0, second=0, microsecond=0
    ),
    end=datetime.datetime.now(datetime.UTC).replace(
        hour=23, minute=59, second=59, microsecond=0
    ),
)


@pytest.mark.skipif(
    not LiveCalendarClient().health().__class__.__name__ == "Healthy",
    reason="GOOGLE_OAUTH_CREDENTIALS not configured",
)
def test_live_calendar_fetch_returns_well_shaped_rows():
    client = LiveCalendarClient()
    rows = client.fetch(_TODAY_WINDOW, owner_user_id="live-smoke-test")

    for row in rows:
        assert row["source"] == "calendar"
        assert row["external_id"]
        assert row["actor_reference_key"].startswith("calendar:")


@pytest.mark.skipif(
    not LiveSlackClient().health().__class__.__name__ == "Healthy",
    reason="SLACK_BOT_TOKEN not configured",
)
def test_live_slack_fetch_returns_well_shaped_rows_with_no_message_content():
    client = LiveSlackClient()
    rows = client.fetch(_TODAY_WINDOW, owner_user_id="live-smoke-test")

    for row in rows:
        assert row["source"] == "slack"
        assert row["external_id"]
        assert row["body_ref"].startswith("slack://")
        assert "text" not in row  # never the raw message body


@pytest.mark.skipif(
    not LiveLinearClient().health().__class__.__name__ == "Healthy",
    reason="LINEAR_API_KEY not configured",
)
def test_live_linear_fetch_returns_well_shaped_rows():
    client = LiveLinearClient()
    rows = client.fetch(_TODAY_WINDOW, owner_user_id="live-smoke-test")

    for row in rows:
        assert row["source"] == "linear"
        assert row["external_id"]
        assert row["actor_reference_key"].startswith("linear:")


@pytest.mark.skipif(
    not LiveJiraClient().health().__class__.__name__ == "Healthy",
    reason="JIRA_BASE_URL/JIRA_EMAIL/JIRA_API_TOKEN not configured",
)
def test_live_jira_fetch_returns_well_shaped_rows():
    client = LiveJiraClient()
    rows = client.fetch(_TODAY_WINDOW, owner_user_id="live-smoke-test")

    for row in rows:
        assert row["source"] == "jira"
        assert row["external_id"]
        assert row["actor_reference_key"].startswith("jira:")
        assert row["url"].startswith("http")


@pytest.mark.skipif(
    not LiveGoogleDocsClient().health().__class__.__name__ == "Healthy",
    reason="GOOGLE_CLIENT_ID/GOOGLE_CLIENT_SECRET not configured",
)
def test_live_google_docs_fetch_returns_well_shaped_rows_with_no_comment_content():
    client = LiveGoogleDocsClient()
    rows = client.fetch(_TODAY_WINDOW, owner_user_id="live-smoke-test")

    for row in rows:
        assert row["source"] == "google_docs"
        assert row["external_id"]
        assert row["body_ref"].startswith("gdocs://")
        assert row["actor_reference_key"].startswith("google_docs:")
        assert "content" not in row  # never the raw comment text
