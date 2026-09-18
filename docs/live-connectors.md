# Live connectors (Calendar / Slack / Linear / Jira / Google Docs) — setup and status

**Status: all five proven live** (`tests/live/test_mcp_smoke.py -m live`
passes for Calendar, Slack, Linear, Jira, and Google Docs) and wired into
`seed --live` / `app.cli seed --live --user <id>`. `app/ingest/live_source.py`
implements `SourceClient` (the same protocol `app/ingest/fixture_source.py`
implements for fixtures/tests) against five real MCP servers, spawned
over stdio via `app/tools/mcp_config.py`. Each connector surfaced at
least one real bug only visible against a real server — see
`docs/plans/morning-pulse.md`'s M2/M4.1/live-connectors notes for the
specifics (Slack: a not-in-channel error shape; Linear: wrong env var
name + a GraphQL connection response shape; Calendar: a required
`calendarId` argument the tool's own docs didn't make obvious; Jira: the
obvious "my open issues" tool returns a near-empty shape, and the real
tool's response is pre-flattened with no email/accountId available;
Google Docs: a fixed OAuth scope set rejected until every scope was
registered on the consent screen, and an N+1-call fetch shape that took
~2.5 minutes before the doc count was cut down).

Check current status any time with:
```
uv run python -m app.cli live-status
```

`seed --fixture` (the fixture-driven path every other test and demo in
this repo depends on) is untouched — `seed --live --user <id>` is a
separate, additive path onto an existing user.

## Calendar (`LiveCalendarClient`)

Backed by [`@cocal/google-calendar-mcp`](https://github.com/nspady/google-calendar-mcp)
(`list-events` tool).

1. Google Cloud Console → create/select a project → enable the
   [Calendar API](https://console.cloud.google.com/apis/library/calendar-json.googleapis.com).
2. Create OAuth 2.0 credentials, type **Desktop app** (not a service
   account — this server expects an interactive user-consent flow). Add
   your own email as a test user under "Audience".
3. Download the credentials JSON, then:
   ```
   export GOOGLE_OAUTH_CREDENTIALS=/path/to/gcp-oauth.keys.json
   npx @cocal/google-calendar-mcp auth   # one-time interactive consent
   ```
4. Set `GOOGLE_OAUTH_CREDENTIALS` in `.env` (same path). `live-status`
   reports `healthy` once it's set — that only means the env var is
   present, not that step 3's consent flow has completed. If it hasn't,
   `fetch()` will fail at runtime with an auth error from the server.

## Slack (`LiveSlackClient`)

Backed by [`@modelcontextprotocol/server-slack`](https://www.npmjs.com/package/@modelcontextprotocol/server-slack)
(`slack_list_channels` + `slack_get_channel_history` tools).

**Note:** this package is marked deprecated upstream ("no longer
supported") as of when this was written. Still functional at the time of
writing; worth checking for a maintained replacement before relying on it
long-term.

1. Create a Slack app at [api.slack.com/apps](https://api.slack.com/apps),
   install it to your workspace.
2. Add bot token scopes: `channels:history`, `channels:read`,
   `users:read`. Add more if you extend which tools are called.
3. Set `SLACK_BOT_TOKEN` (starts `xoxb-`) and `SLACK_TEAM_ID` in `.env`.

## Linear (`LiveLinearClient`)

Backed by [`mcp-server-linear`](https://www.npmjs.com/package/mcp-server-linear)
(`linear_search_issues` tool).

1. Linear → Settings → Security & access → Personal API keys → create one.
2. Set `LINEAR_API_KEY` (starts `lin_api_`) in `.env`.

## Jira (`LiveJiraClient`)

Backed by [`mcp-jira-cloud`](https://www.npmjs.com/package/mcp-jira-cloud)
(`jira_search_issues` tool — not `jira_get_my_open_issues`, which returns
too sparse a shape to use).

1. Jira Cloud only (not Server/Data Center). Get your site URL (e.g.
   `https://your-domain.atlassian.net`).
2. [id.atlassian.com/manage-profile/security/api-tokens](https://id.atlassian.com/manage-profile/security/api-tokens) → create an API token.
3. Set `JIRA_BASE_URL`, `JIRA_EMAIL` (your Atlassian account email), and
   `JIRA_API_TOKEN` in `.env`.

## Google Docs (`LiveGoogleDocsClient`)

Backed by [`@a-bonus/google-docs-mcp`](https://www.npmjs.com/package/@a-bonus/google-docs-mcp)
(`listDriveFiles` + `listComments` tools).

1. Reuses the same OAuth client as Calendar (`GOOGLE_OAUTH_CREDENTIALS`'s
   JSON already has a `client_id`/`client_secret` — no need for a second
   OAuth client, scopes are requested at consent time). Enable the
   [Docs API](https://console.cloud.google.com/apis/library/docs.googleapis.com)
   and [Drive API](https://console.cloud.google.com/apis/library/drive.googleapis.com)
   on that same project (comments live in the Drive API, not the Docs API).
2. Set `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` in `.env` to that same
   OAuth client's `client_id`/`client_secret` fields.
3. This package's consent flow always requests a fixed six-scope set
   (Docs/Drive/Sheets/script.external_request/Gmail-modify/
   Calendar-events) — go to the OAuth consent screen's scope
   configuration (Google Cloud Console → APIs & Services → OAuth consent
   screen → Data Access → Add or remove scopes) and add all six *before*
   running auth, or every attempt fails with a bare `Error 400:
   invalid_request` regardless of which APIs are enabled.
4. Run the one-time consent flow:
   ```
   GOOGLE_CLIENT_ID=... GOOGLE_CLIENT_SECRET=... npx -y @a-bonus/google-docs-mcp auth
   ```
   This opens a browser URL; if pasting it doesn't work, copy the raw
   printed URL text directly into the address bar rather than clicking a
   rendered/wrapped link — a truncated `response_type` param produces a
   misleading "Required parameter is missing" error that looks like a
   config problem but isn't. The refresh token is stored separately at
   `~/.config/google-docs-mcp/token.json`, not in `.env`.

## Once credentials exist

```
uv run python -m app.cli live-status          # confirm all five report healthy
uv run pytest -m live tests/live/test_mcp_smoke.py   # prove the adapters against real data
```

If a smoke test fails, the response adapter in `live_source.py` is wrong
about that server's actual shape — fix the adapter, not the test's
invariants (same rule `tests/live/test_llm_smoke.py` follows: assert
invariants, not exact output, since real API responses vary run to run).
