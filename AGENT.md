# AGENT.md — Mentor Agent

Instructions for any AI coding agent (Claude Code, Cursor, Copilot Workspace, Viktor)
working in this repository. Read this file fully before your first edit.
If reality and this file disagree, fix this file in the same PR.

---

## 0. The plan gate (highest-priority rule)

**Always present a written plan and wait for explicit approval before executing any
multi-step task.** Multi-step = more than one file changed, any migration, any new
dependency, any destructive or external action.

The plan is a message, not a commit:

```
PLAN
Goal:        one sentence
Steps:       1..n, each with the file(s) touched
Layer(s):    which of the 7 layers
Risks:       what could break / what is irreversible
Verification: the exact commands you will run
Not doing:   explicit out-of-scope
```

Then stop and wait. Rules:
- Approval is per-plan, not standing. A new task needs a new plan.
- If the plan changes mid-execution, stop, say what changed, re-propose.
- "Looks good", "yes", "go" = approval. Silence, a question, or a reaction is not.
- Single-step, read-only, or reversible-in-one-command work (reading files, running
  tests, formatting) needs no plan — just do it and report.
- Never bundle unapproved extras into an approved plan ("while I was there I also...").

## 1. What this project is

**Mentor Agent** is a *personal execution co-pilot* for knowledge workers: it prepares
meetings, keeps 1-on-1s aligned, delivers a morning "Command Center" pulse, runs a Friday
review, and silently maintains an **Accomplishment Ledger** tied to the user's OKRs.

It is **not** an academic tutor, not a chatbot wrapper, and not a read-only dashboard.
It reads from work systems over MCP, decides what deserves the user's attention, and
**writes back** (agenda items, ledger entries, ticket comments) with the user's consent.

Five product features (all in scope — phased, never cut):

| # | Feature | Trigger |
|---|---------|---------|
| F1 | Pre-meeting dossier | 15 min before a qualifying event |
| F2 | Rolling 1-on-1 agenda | continuous + on 1-on-1 events |
| F3 | Morning Command Center pulse | daily, before work hours |
| F4 | Friday reflection / review | Friday afternoon |
| F5 | Accomplishment Ledger | continuous, silent |

Interaction surface: Slack DM cards + slash commands (`/mentor prep`, `/mentor review`,
`/mentor memory`, `/mentor quiet until 9am`).

---

## 2. Architecture: the 7 layers (do not reorder)

Data flows strictly downward. A layer may only call the layer directly below it.

1. **Triggers** — time-based schedules + explicit user invocation. Emits a `TriggerEvent`.
2. **Ingestion (MCP connectors)** — one client per source (Calendar, Slack, Linear/Jira,
   HRIS). Raw payloads only; no interpretation, no LLM calls.
3. **Normalization** — raw payloads → canonical internal types (`Event`, `Message`,
   `WorkItem`, `Goal`). Timezone-aware, deduplicated, provenance-stamped.
4. **Identity graph** — resolves every actor to a single `person_id` (see §4).
   **4b. Declarative memory** — per-person / per-ritual markdown skill files describing
   *behaviour* ("Amal prefers 3 bullets, no emoji"). Separate from numeric weights.
5. **Salience & budget** — decides *whether* to speak: scoring, notification budget,
   suppression gate (see §5).
6. **Composition (LLM)** — decomposed prompts, one job each: dossier writer, agenda
   merger, pulse ranker, ledger extractor. Never one mega-prompt.
7. **Delivery & feedback / write-back** — renders cards, sends them, captures feedback
   signals, writes back to source systems, updates weights and memory.

**Invariants**
- Layers 1–4 contain **zero LLM calls**. If you feel you need an LLM there, you are
  hiding a normalization bug.
- Layer 6 never queries a connector directly; it receives an assembled context object.
- Everything the user sees is reproducible: given the same context object + weights,
  regenerate the same card (temperature pinned, prompt version recorded).

---

## 3. Stack & layout

**Framework: Google ADK** (`google-adk`, Python) driven by **`agents-cli`**
(`uvx google-agents-cli setup`). Consequences you must respect:

- Agents are `Agent` / `LlmAgent` instances with instructions + tools; multi-agent
  structure is a tree via `sub_agents`. Orchestration uses ADK workflow agents
  (`SequentialAgent`, `ParallelAgent`, `LoopAgent`) — not hand-rolled control flow.
- `agents-cli` reads **`AGENTS.md`** as the rules file (it projects to `CLAUDE.md`,
  `GEMINI.md`, `.cursorrules`). This file is the source of truth; keep
  `AGENTS.md` as the real file and `AGENT.md` as a symlink to it, so every coding
  agent gets the same rules.
- Skills live in `.agents/skills/<name>/SKILL.md`, hooks in `.agents/hooks/`,
  MCP servers in the `agents-cli` MCP config — never hard-code an MCP client.
- Tooling: Python 3.12, `uv`, `pytest`, `ruff` + `black`. Persistence: SQLite in dev,
  Postgres in prod (SQLAlchemy + Alembic). Scheduling: ADK-invoked jobs, one trigger
  module per ritual.

```
app/
  agent.py         # root_agent (the Mentor coordinator) — thin, delegates
  agents/          # one sub-agent per L6 job: dossier, agenda, pulse, ledger
  tools.py         # @tool functions exposed to agents (thin wrappers only)
  triggers/        # L1  schedules + slash-command entrypoints
  connectors/      # L2  one module per MCP source, behind a SourceClient protocol
  normalize/       # L3  payload -> canonical models
  identity/        # L4  person registry, match tiers, merge + provenance
  memory/          # L4b skill files, weights, consolidation job
  salience/        # L5  scoring, budget, suppression gate
  deliver/         # L7  Slack cards, feedback capture, write-back
  core/            # models, config, db, clock, logging
.agents/
  skills/          # agent-facing skills (see SOUL.md + §7b)
  workflows/       # named multi-step procedures the agent may run
  hooks/
SOUL.md            # the agent's identity — read first, see §0
AGENTS.md          # this file (AGENT.md -> symlink)
tests/  migrations/  docs/
```

Rules:
- **Tools are thin.** A `@tool` function validates input, calls one layer function,
  returns a typed result. No business logic inside a tool definition.
- Business logic never imports a vendor SDK — only `connectors/` and `deliver/` may.
- Never call `datetime.now()` outside `core/clock.py`. Tests freeze time.
- All config through `core/config.py` (pydantic settings). No bare `os.environ`.
- Agent instructions are versioned files under `app/prompts/`, with the version
  ID recorded on every generated card.

**Actual current layout for the pulse ritual's L6 step** (the diagram above
predates this and was never updated for it): real ADK agents live under
`app/sub_agents/<ritual>/`, one folder per agent, recursively — `agent.py`
(the agent's own definition + any pure helper it needs) plus a `sub_agents/`
subfolder only if that agent has its own children. `app/sub_agents/pulse/
agent.py` holds `pulse_pipeline` (a `SequentialAgent` chaining
`ranker_agent` -> `writer_agent`); `app/sub_agents/pulse/sub_agents/
{ranker,writer,critic}/agent.py` hold the three leaf `LlmAgent`s, each
file also carrying that agent's own deterministic fallback logic
(`fallback_rank`, `fallback_render`) and result type. `app/tools/
custom_tools.py` holds `get_morning_pulse`, the one real tool on
`root_agent` (`app/agent.py`) so far — thin per the rule above,
delegating to `app/triggers`/`app/pipeline` for the actual work.
`app/tools/mcp_config.py` holds the MCP transport bridge and one server-
spec factory per connector (`calendar_mcp_spec`/`slack_mcp_spec`/
`linear_mcp_spec`/`jira_mcp_spec`), used by `app/ingest/live_source.py`'s
SourceClient implementations — not agent-invocable tools themselves, just
shared MCP configuration. `app/core/adk_runner.py` is the generic
sync/async bridge every one of the agents above runs through
(`run_agent_sync`), the ADK equivalent of `app/tools/mcp_config.py`'s
existing bridge for the MCP SDK.

## 4. Identity resolution (layer 4) — the hard part

One `Person` row per human, with `identities` rows (`source`, `external_id`, `tier`,
`confidence`, `first_seen`). Match ladder, highest confidence first:

1. Verified email match
2. IdP / directory ID match (HRIS ↔ SSO)
3. Exact handle match within the same workspace
4. Normalized full-name match + context prior (co-meeting / co-project)
5. Fuzzy name match above threshold **+** context prior
6. Closed-list LLM disambiguation — only for free-text mentions, only choosing among
   candidates the graph already produced. Never invents a person.

Non-negotiables:
- Merges are **append-only with provenance** and reversible (`merge_log`).
- Below-threshold matches stay *unresolved*, they never guess.
- A golden set of hand-labelled pairs lives in `tests/identity/golden.yaml`;
  CI fails if precision drops below **0.99**.

---

## 5. Salience, budget, suppression

- Score = urgency × relevance × weights, then a **notification budget** per day.
- **Suppression is a separate gate after the trigger.** Never retune a heuristic to
  silence one meeting. One `suppressions` table, four scopes:
  `instance` | `series` | `temporal` | `global`, each with a TTL.
- Precedence: explicit user suppression beats urgency; narrower scope beats broader;
  suppression means **silence, not deletion** — pull surfaces (`/mentor prep`) still
  show everything.
- Repeated overrides feed the layer-7 proposal pipeline (propose → user commits).

**Weight updates run on three clocks:** instant append-only event log; nightly 03:00
consolidation batch; lazy exponential decay computed at read time. Hard signals
(explicit mute/thumbs-down) may apply instantly. Weights clamp to `[0.2, 3.0]`, gated by
`n_signals`, with structural floors (e.g. manager 1-on-1 never decays out).

---

## 6. Privacy rules — treat as tests, not guidelines

1. Ledger and career notes are **user-owned**. No admin, manager, or support path reads
   them. Enforce per-user encryption at rest; keys derived per user.
2. Cross-user reads must go through an explicit grant record. There is no
   "service account can see all" mode.
3. Never log message bodies or ledger content. Log IDs, counts, decisions.
4. Write-back requires a consent scope per target system; default is propose-only.
5. Any new connector or new field ships with a line in `docs/data_inventory.md`
   (what is fetched, why, retention). PRs adding data collection without it are invalid.

---

## 7. Working agreement for the agent

**Do**
- Read `SOUL.md` first, then this file, then `docs/` (spec v3) and the tests for the layer you touch.
- Post a PLAN (§0) and wait for approval before any multi-step change.
- Keep changes to one layer per PR where possible; state which layer in the PR title.
- Write the test first for anything in identity, salience, or privacy.
- Prefer small pure functions with explicit inputs; pass the clock, db session and
  config in rather than importing globals.
- Use structured logging with `person_id` / `trigger_id` correlation IDs.
- Update `AGENT.md`, `docs/adr/` and docstrings when a decision changes.

**Don't**
- Don't add a new dependency without noting why in the PR description.
- Don't call an LLM in layers 1–5.
- Don't widen a heuristic to fix one bad notification — add a suppression.
- Don't mock at the HTTP layer for connector tests; use recorded fixtures in
  `tests/fixtures/{source}/`.
- Don't commit real workspace data, tokens, or `.env` files. Fixtures are synthetic.
- Don't silently change a prompt: prompts are versioned files with an ID recorded on
  every generated card.

**Definition of done**
`ruff check . && black --check . && pytest` passes · new behaviour has tests ·
no new privacy surface without a data-inventory line · docs updated · PR body says
*what changed / which layer / how it was verified / what was not covered*.

---

## 7b. Skills & workflows

Two different things — do not merge them.

**Skills** (`.agents/skills/<name>/SKILL.md`) = *how to do a kind of work well.*
Durable, reusable, agent-facing. Frontmatter `name` + one-to-two-sentence `description`;
body under ~3k tokens; scripts in `scripts/`, long detail in `references/`.
Starter set for this repo:
- `mentor-layer-map` — which layer owns what, and the invariants
- `mentor-connector-add` — checklist for adding an MCP source (fixtures, inventory line)
- `mentor-identity-eval` — run and interpret the golden-set precision gate
- `mentor-card-authoring` — voice and shape of a Slack card
- `mentor-privacy-review` — the checks before any PR touching user data

**Workflows** (`.agents/workflows/<name>.md`) = *a named multi-step procedure with a
fixed order and a definition of done.* Each starts with the PLAN template from §0.
Starter set: `add-feature`, `add-connector`, `phase-gate` (P1→P2 promotion checks),
`incident-bad-notification` (diagnose → suppression, never heuristic retune),
`release`.

Maintenance: when a skill's process turns out wrong, fix the skill in the same PR as the
code. A skill that lies is worse than no skill.

## 8. Commands

```bash
uvx google-agents-cli setup        # install skills/rules into your coding agent
uv sync                            # install deps
uv run adk web                     # ADK dev UI, poke the agent by hand
uv run adk run app                 # run the root agent in the terminal
uv run pytest -q                   # tests
uv run pytest tests/identity       # golden-set identity precision gate
uv run ruff check . && uv run black .
uv run alembic upgrade head        # migrations
uv run python -m app.cli demo-card --ritual pulse --user amal   # render a card locally
```

Secrets in `.env` (see `.env.example`): Slack bot/app tokens, Google/Outlook OAuth,
Linear/Jira keys, LLM key, `ENCRYPTION_MASTER_KEY`.

---

## 9. Phasing (keep breadth, stage delivery)

The morning pulse (F3) shipped first, end to end — L1 triggers through L7
delivery — ahead of `/mentor prep` (F1). This supersedes the original
P1/P2 split below, which had pulse landing second; that plan was written
before the pulse ritual's own scope (docs/plans/morning-pulse.md) made it
the more self-contained starting point (fixture-backed, no live connector
dependency, exercises every layer). Identity resolution's tier 0–5 ladder
already existed independently on this branch and was reused via a
deliberately narrow `resolve_actor()` seam (exact provider ID or primary
email only) rather than extended — see morning-pulse.md's M2 section.

- **Shipped** — Layers 1–7 for the morning pulse: schema + fixtures (M1),
  L2/L3 ingestion (M2), L5 salience (M3), L6 ranker/writer/critic (M4), L1
  triggers + late-cutoff window selection (M5), L7 delivery/cards/
  affordances (M6). Postgres throughout (Docker Compose), not SQLite.
- **Not yet shipped** — `/mentor prep` (F1), the Rolling 1-on-1 agenda (F2),
  Friday review (F4), the Accomplishment Ledger (F5). All three live
  connectors (Calendar, Slack, Linear) are now proven live and wired into
  `seed --live`. `app/agent.py`'s `root_agent` is no
  longer just the ADK scaffold's placeholder — it has one real tool,
  `get_morning_pulse` (`app/tools/custom_tools.py`), so asking for a pulse in a live
  chat session actually runs the ritual for that session's own user; L6
  itself (ranker/writer/critic) is now built as real ADK agents
  (`app/sub_agents/pulse/`), not the plain-function seam this section used
  to describe. `/mentor prep`/etc. and a from-scratch conversational
  identity/delegation model for `root_agent` remain future work.
- **Original P1–P4 breakdown**, kept for what it still gets right about
  sequencing later features:
  - P1 — Layers 1–3 + `/mentor prep`, Calendar + Slack only, dossier card.
  - P2 — Identity graph + golden set (done, independently of pulse),
    Linear/Jira, salience v1 (done, as part of the pulse ritual above).
  - P3 — Ledger, Friday review, write-back with consent, feedback signals.
  - P4 — Weights + declarative memory + suppression surfaces, HRIS,
    encryption hardening.

Do not implement a later phase's feature by hacking an earlier layer. Land the layer first.

---

## 10. Glossary

`person_id` canonical human · `TriggerEvent` L1 output · `context object` assembled input
to L6 · `card` one Slack delivery unit · `salience` speak-or-not score ·
`suppression` user-set silence with scope+TTL · `weights` numeric per-person preferences ·
`skill file` markdown declarative memory (product-side, per person) — distinct from `.agents/skills/` (dev-side) · `ledger` private accomplishment log.
