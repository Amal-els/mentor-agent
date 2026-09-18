"""Live SourceClient implementations (app/ingest/base.py) backed by real MCP
servers — the live half of the seam app/ingest/fixture_source.py already
implements for tests/demos. STATUS: see app/tools/mcp_config.py's module
docstring. Tool names/argument shapes are from real introspection
(session.list_tools() against each server, no credentials required for
that); the response *adapters* below (_adapt_*) are written defensively
against each server's documented/typical response shape. Slack, Linear,
Calendar, Jira, and Google Docs have each been proven against a real
server (docs/plans/morning-pulse.md). Wired into app/ingest/seed.py's
seed_live() and app/cli.py's `seed --live` / `ingest --live`.

LiveNotionGoalsClient/LiveNotionNotesClient are proven against a real
Notion workspace this session (real data_source ids, real property
shapes — see notion_mcp_spec's and each class's own docstring). Pagination
(start_cursor/has_more) was never actually exercised live (the test
workspace never had enough rows to trigger it) — _notion_query_all is
written defensively against Notion's documented cursor shape but that
specific path is unverified.

LiveFathomClient does not exist yet — the official Fathom MCP server is
remote HTTP+OAuth, which app/tools/mcp_config.py has no transport for
yet (every client in this module is stdio+npx); see the rolling-agenda
plan for the staged design.

This used to be one 1656-line module — split one file per connector
(calendar_.py, slack.py, linear.py, jira.py, google_docs.py, gmail.py,
notion.py, fathom.py; Notion kept as one file for its three classes'
shared adapters — see notion.py's own docstring) once it had grown large
enough that finding any one connector's code meant scrolling past eight
others'. This __init__ re-exports every public name from every submodule
so every existing `from app.ingest.live_source import LiveSlackClient`
(etc.) across the app keeps working unchanged — nothing outside this
package needs to know it's now a package rather than one file."""

from app.ingest.live_source.calendar_ import (
    LiveCalendarClient,
    _adapt_calendar_event,
    _format_calendar_timestamp,
)
from app.ingest.live_source.fathom import LiveFathomClient, _adapt_fathom_transcript
from app.ingest.live_source.gmail import (
    LiveGmailClient,
    _adapt_gmail_message,
    _extract_email_address,
    _is_gmail_noise,
)
from app.ingest.live_source.google_docs import (
    LiveGoogleDocsClient,
    _adapt_google_docs_comment,
)
from app.ingest.live_source.jira import (
    LiveJiraClient,
    _adapt_jira_issue,
    _blocks_others,
)
from app.ingest.live_source.linear import LiveLinearClient, _adapt_linear_issue
from app.ingest.live_source.notion import (
    LiveNotionGoalsClient,
    LiveNotionNotesClient,
    LiveNotionPairClient,
    _adapt_notion_career_goal,
    _adapt_notion_key_result,
    _adapt_notion_objective,
    _adapt_notion_one_on_one_note,
    _notion_formula_number,
    _notion_number,
    _notion_people_emails,
    _notion_people_entries,
    _notion_plain_text,
    _notion_query_all,
    _notion_relation_ids,
    _notion_rollup_progress,
    _notion_select,
    resolve_notion_data_source,
    resolve_notion_data_source_id,
    resolve_notion_data_sources,
    resolve_notion_database_id,
)
from app.ingest.live_source.slack import (
    LiveSlackClient,
    _adapt_slack_message,
    _extract_mentioned_slack_user_ids,
    _mentions_viewer,
    resolve_slack_display_name,
    resolve_slack_mentions_for_display,
)

__all__ = [
    "LiveCalendarClient",
    "LiveFathomClient",
    "LiveGmailClient",
    "LiveGoogleDocsClient",
    "LiveJiraClient",
    "LiveLinearClient",
    "LiveNotionGoalsClient",
    "LiveNotionNotesClient",
    "LiveNotionPairClient",
    "LiveSlackClient",
    "_adapt_calendar_event",
    "_adapt_fathom_transcript",
    "_adapt_gmail_message",
    "_adapt_google_docs_comment",
    "_adapt_jira_issue",
    "_adapt_linear_issue",
    "_adapt_notion_career_goal",
    "_adapt_notion_key_result",
    "_adapt_notion_objective",
    "_adapt_notion_one_on_one_note",
    "_adapt_slack_message",
    "_blocks_others",
    "_extract_email_address",
    "_extract_mentioned_slack_user_ids",
    "_format_calendar_timestamp",
    "_is_gmail_noise",
    "_mentions_viewer",
    "_notion_formula_number",
    "_notion_number",
    "_notion_people_emails",
    "_notion_people_entries",
    "_notion_plain_text",
    "_notion_query_all",
    "_notion_relation_ids",
    "_notion_rollup_progress",
    "_notion_select",
    "resolve_notion_data_source",
    "resolve_notion_data_source_id",
    "resolve_notion_data_sources",
    "resolve_notion_database_id",
    "resolve_slack_display_name",
    "resolve_slack_mentions_for_display",
]
