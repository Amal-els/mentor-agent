# Mentor

Mentor is a private execution co-pilot for one person: a Gemini-powered agent (Google ADK)
that pulls together your Calendar, Slack, Notion, Jira, Linear, Gmail/Docs and GitHub
activity into a single knowledge graph per user, then surfaces it back to you as timed
rituals — a morning pulse, a pre-meeting dossier, a Friday reflection — plus an
interactive chat.

See [SOUL.md](app/SOUL.md) for who the agent is and how it's meant to speak. This file
covers how the project is built and how to run it.

## What it does

| Ritual | Trigger | What it produces |
|---|---|---|
| **Morning pulse** | `PULSE_SCHEDULED_OWNER_IDS`, fires at each user's `pulse_fire_time_local` ([cron_scheduler.py](app/triggers/cron_scheduler.py)), or `/mentor pulse` in Slack | A short daily briefing: what's on today, what's blocked, what needs a reply |
| **Pre-meeting dossier** | `DOSSIER_SCHEDULED_REPORT_USER_IDS`, T-15-minutes before a calendar event ([dossier_scheduler.py](app/triggers/dossier/dossier_scheduler.py)) | Who you're about to meet and what's unresolved with them |
| **Friday reflection** | Per-user `friday_review_fire_time_local`, Fridays only ([friday_review_scheduler.py](app/triggers/friday_review_scheduler.py)) | A review of the week: what got done, what's still open |
| **Rolling 1-on-1 agenda** | Notion OKR sync + meeting-end poll ([agenda_scheduler.py](app/triggers/agenda/agenda_scheduler.py)) | A living agenda for manager↔report 1-on-1s, mapped to real OKRs |
| **Mentor advisor** | On demand via `/webhooks/mentor-advice` ([mentor_advisor_router.py](app/triggers/mentor_advisor_router.py)) | Short-term and long-term tasks aligned to the user's career goals |
| **Agent chat** | The dashboard's Chat tab, talking directly to ADK's own `/run` endpoint | Freeform conversation with `root_agent`, with the same tools above and live tool-call tracing |

Every ritual and every row in the database is scoped by `owner_user_id` — there is no
shared or global data between users (see [OwnerScope](app/core/scope.py)).

## Architecture

```
app/
├── agent.py            # root_agent — the ADK Agent used by the dashboard's chat tab
├── SOUL.md              # agent identity/voice, loaded into every prompt
├── fast_api_app.py      # FastAPI app: mounts ADK's own dev server + all webhook routers
├── sub_agents/           # one folder per ritual — each is its own ADK agent pipeline
│   ├── pulse/             # morning pulse: gather → rank/critic loop → deliver
│   ├── dossier/            # pre-meeting dossier: gather → synthesize → deliver
│   ├── friday_review/       # Friday reflection: suppression check → gather → synthesize → deliver
│   ├── agenda/                # rolling 1-on-1 agenda: capture → map_okrs → synthesize
│   ├── checklist/               # supportive/checklist responses
│   └── mentor_advisor/            # career-goal-aligned advice
├── triggers/            # L1 triggers — local polling loops + FastAPI webhook routers,
│   │                      # grouped into a subfolder per feature area where more than
│   │                      # one file belongs to it; standalone triggers stay flat
│   ├── agenda/             # rolling agenda webhooks, scheduler, setup/login/goals/ledgers
│   ├── slack/               # /mentor slash command: router, Socket Mode listener, signature
│   ├── dossier/               # pre-meeting dossier webhook + T-15 scheduler
│   └── webhooks/                # inbound push-connector receiver (GitHub/Linear/Jira) + sig
├── ingest/               # per-connector clients (live_source/ — one module per connector:
│   │                       # calendar_, slack, linear, jira, google_docs, gmail, notion,
│   │                       # fathom — plus normalize.py, which projects every ingested
│   │                       # record into the knowledge graph
│   └── live_source/
├── graph/resolver.py     # the knowledge graph itself: GraphNode/GraphEdge projection and
│                           # identity resolution
├── identity/             # cross-source identity resolution (who is @sarahdev really)
├── salience/              # what matters enough to surface — one subfolder per ritual
│   ├── pulse/               # assemble, gate, score, checklist, weights for the morning pulse
│   └── dossier/               # the dossier's own gate + score (self-contained, no pulse import)
├── core/                   # models, scope, config, crypto, clock
├── delivery/                # Slack + Email delivery channels
├── tools/mcp_config.py       # every MCP server spec this app spawns, and the shared
│                               # McpSession/call_tool machinery
├── cli.py                      # Typer CLI — seed, link accounts, purge test data, etc.
└── static/                      # the dashboard (single-page, vanilla JS + Cytoscape)
```

The dashboard ([app/static/index.html](app/static/index.html) +
[app.js](app/static/app.js)) is one page with tabs: Rolling Agenda, Goals, Dossier,
Knowledge Graph, and Agent Chat. It talks to the webhook routers under `/webhooks/*`
(each guarded by a shared `X-Agenda-Token` plus a per-user `X-Acting-User-Secret`) and,
for chat, directly to ADK's own `/apps/.../sessions` and `/run` endpoints.

## Requirements

- **uv** — Python package manager: [install](https://docs.astral.sh/uv/getting-started/installation/)
- **Node.js + npx** — several connectors run as MCP servers over `npx` (Calendar, Docs/Gmail)
- **A Postgres database** — this project uses Supabase in development; any Postgres works
- **Google Cloud project with Vertex AI enabled** (or a Gemini API key — see below) and, if you
  hit `403 PERMISSION_DENIED: Spend cap breached`, that project's billing spend cap needs
  raising in Cloud Console before any agent call will succeed
- **A Google Cloud OAuth client** for Calendar/Docs/Gmail — see [Google OAuth setup](#google-oauth-setup) below, it needs **two** client types, not one

## Quick start

```bash
uv sync                       # install dependencies
cp .env.example .env          # then fill in .env — see below
alembic upgrade head          # create/migrate the database schema
uv run uvicorn app.fast_api_app:app --reload   # start the dashboard + webhook routers
```

Open `http://localhost:8000/ui`. `agents-cli playground` also works for iterating on
`app/agent.py` alone (auto-reloads), but the dashboard, schedulers and Slack listener all
need the full `uvicorn` process (or their own `python -m app.triggers.<name>` processes —
see [Running the schedulers](#running-the-schedulers)).

## Configuration (`.env`)

Copy [.env.example](.env.example) and fill in every value it documents inline — it's
kept up to date with *why* each variable exists, not just its name. The short version:

- **Model access**: either `GOOGLE_GENAI_USE_VERTEXAI=true` + `GOOGLE_CLOUD_PROJECT` +
  `GOOGLE_CLOUD_LOCATION` (Vertex AI), or comment those out and set `GEMINI_API_KEY`
  (Google AI Studio) instead.
- **`DATABASE_URL`** — a Postgres connection string.
- **Slack** — `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET`, and `SLACK_APP_TOKEN` if the Slack
  app has Socket Mode on (it does, by default — see
  [slack_socket_listener.py](app/triggers/slack/slack_socket_listener.py)).
- **Notion / Jira / Linear** — API keys/tokens for each connector you want live (all
  optional; unconfigured connectors just don't ingest).
- **Google (Calendar/Docs/Gmail)** — see below, this is the part that's easy to get wrong.
- **`PULSE_SCHEDULED_OWNER_IDS`** / **`DOSSIER_SCHEDULED_REPORT_USER_IDS`** — comma-separated
  allowlists of `owner_user_id`s the pulse and dossier schedulers are allowed to fire for.
  Required, no default — this is a deliberate guard, not an oversight: the dev database
  is easy to end up sharing with test runs, and without an explicit allowlist a scheduler
  would fire for every linked user in the table, test users included.

### Google OAuth setup

This app needs **two separate Google OAuth clients**, because it has two different
integration shapes:

1. **A Web client** (`GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET`) — used by the dashboard's
   in-browser "Connect Google" flow ([google_oauth_router.py](app/triggers/google_oauth_router.py)),
   which redirects to a fixed, registered callback URL
   (`http://localhost:8000/webhooks/google-oauth/callback`). Also used for any per-user
   `google-docs-mcp` spawn once that user has completed the connect flow.
2. **A Desktop client** (`GMAIL_MCP_CLIENT_ID` / `GMAIL_MCP_CLIENT_SECRET`) — used by the
   one-time `npx -y @a-bonus/google-docs-mcp auth` consent flow, which listens on a random
   `http://localhost:<port>` — only a Desktop-type client accepts an unregistered loopback
   redirect like that. This client backs the shared token that setup-link emails and any
   user who hasn't connected their own Google account fall back to.

Mixing these up is the single most common setup failure here: a token minted with one
client and read back with the other's id/secret fails with `redirect_uri_mismatch` (during
`auth`) or `invalid_grant` / `unauthorized_client` (at send time). See
[app/tools/mcp_config.py](app/tools/mcp_config.py)'s `google_docs_mcp_client_env` for
exactly which client each code path uses, and [.env.example](.env.example) for the setup
steps inline.

Additionally, `@cocal/google-calendar-mcp` needs its own one-time interactive consent —
`npx @cocal/google-calendar-mcp auth`, pointed at the credentials file in
`GOOGLE_OAUTH_CREDENTIALS` — before `LiveCalendarClient.fetch()` works.

Whichever client you use for `npx -y @a-bonus/google-docs-mcp auth`, its OAuth consent
screen must have the following scopes added (Console → OAuth consent screen → Scopes),
or the flow fails outright with `invalid_request`:
`documents`, `drive`, `spreadsheets`, `script.external_request`, `gmail.modify`,
`calendar.events`.

## Running the schedulers

The four rituals above are local, persistent polling loops — not Cloud Scheduler or a
deployed trigger — deliberately, for now (see each scheduler's own module docstring for
the full reasoning). Each is its own process:

```bash
uv run python -m app.triggers.cron_scheduler                    # morning pulse
uv run python -m app.triggers.dossier.dossier_scheduler          # pre-meeting dossier
uv run python -m app.triggers.friday_review_scheduler            # Friday reflection
uv run python -m app.triggers.agenda.agenda_scheduler             # rolling 1-on-1 agenda
uv run python -m app.triggers.slack.slack_socket_listener          # /mentor pulse Slack slash command
```

They only run while their process is running on this machine — if the machine sleeps or
a process isn't up at fire time, that day's ritual simply doesn't happen. There is no
cloud backstop yet.

## CLI

`app/cli.py` is a Typer CLI, run as `uv run python -m app.cli <command>`. A few commands
worth knowing:

| Command | What it does |
|---|---|
| `seed --fixture <name>` | Load a fixture from `app/fixtures/pulse/` for a user, for local testing without live connectors |
| `seed --live --user <id>` | Ingest from the real, connected connectors for a user |
| `link-slack --user <id> --slack-user-id <U...>` | Link a Slack identity to an existing user |
| `link-notion ...` | Link a Notion identity |
| `link-agenda-client --user <id>` | Generate that user's `agenda_client_secret` (the dashboard's per-user auth secret) |
| `purge-test-data --user <id> [--confirm]` | Preview (default) or delete synthetic/fixture rows for one user, without touching their real Notion identity data |
| `live-status` | Show which live connectors are configured for which users |
| `validate` | Sanity-check configuration |

Run `uv run python -m app.cli --help` for the full list and options.

## Tests

```bash
uv run pytest tests/unit tests/integration
```

> **Careful:** `tests/unit/conftest.py`'s `pg_session` fixture currently points at
> whatever `DATABASE_URL` is set in your environment/`.env` and never rolls back or
> truncates — every committed row from a test run persists. If that's pointed at a
> shared dev database, running the suite will leave real-looking test users (and their
> graph nodes, goals, messages, etc.) in it. Point `DATABASE_URL` at a disposable database
> before running tests, or expect to need `purge-test-data` (see [CLI](#cli)) or a manual
> cleanup afterwards.

## Observability

Cloud Trace/Logging/BigQuery export is available via ADK but is **off** in
[fast_api_app.py](app/fast_api_app.py) (`otel_to_cloud=False`) — it was found live to add
real per-call latency while never once succeeding (the project's service account was never
granted the IAM role the export needs), so it's disabled rather than paying that cost for
nothing. Application logs go to stdout; each of the local scheduler processes above also
writes its own `<name>.log` file when run standalone.