# PLAN — F3(L1–L7): Morning Pulse, full ritual

## Goal
Ship the morning Command Center pulse (F3) end-to-end — L1 trigger through L7
delivery — as the first ritual shipped, ahead of `/mentor prep`, reusing the
identity graph and core scaffolding that already exist on this branch.

## Locked decisions (from brainstorm, do not relitigate mid-build)
1. **Critic fixtures**: `app/fixtures/pulse/critic/{draft_card,context,expected_revision}`
   as three files per scenario. Scenarios: invented name, missing why-now,
   focus count > 3, missing degradation line, one clean pass.
2. **`candidate_focus`**: computed in L5 as urgency (explicit deadline / time
   pressure inside the window) **AND** relevance (someone is blocked waiting
   on the user, OR it moves a tracked goal). Both required — no single-factor
   promotion.
3. **Trigger parity**, two separate assertions, both enforced by construction
   not by test-only discipline:
   - **(a) Pre-gate parity, no exceptions**: `assemble_and_score` is
     trigger-blind. `pre_gate_context(cron) == pre_gate_context(pull)`,
     compared on canonical item IDs, scores, score terms, and order —
     excluding `trigger`, `requested_at`, `delivery_id`.
   - **(b) Gate accountability**: the suppression+budget gate is one
     post-scoring step that returns `(surviving_items, removals)` where each
     removal is `(item_id, reason)`. Then
     `set(pull.items) - set(cron.items) == set(gate_removals)` and
     `set(cron.items) - set(pull.items) == {}`. No cron-only omission may
     exist without a named gate removal.
   - Implementation consequence: no `WHERE not suppressed` in any query, no
     `if trigger == "pull"` branch inside scoring. The gate is the only place
     trigger affects output, and it logs removals (that log doubles as the
     eval dataset per AGENT.md's eventing rule).
4. **Cross-user pulses: disabled entirely.** No delegation grant mechanism.
   `pulse.md` §6.2 is overridden — "requested for someone else" always
   refuses, unconditionally, no grant table. Only the accidental-collision
   isolation test (owner_user_id scoping) applies; document the §6.2 override
   in the same commit that touches `pulse.md`.
5. **Late-call cutoff**: per-user `late_cutoff_local` on the `users` row
   (default `21:00`, evaluated in-tz via `core/clock.py`); `config.py` holds
   only the default for new users, never the effective value (same treatment
   as the 08:30 fire time). Window selection order:
   1. Today still has an un-elapsed item → show today, regardless of clock.
   2. Else `now >= late_cutoff_local` → tomorrow.
   3. Else (before cutoff, nothing left today) → tomorrow too — an empty
      remainder is not a decision.
   `PulseContext` carries `window_date` + `window_reason` (`today` |
   `late_cutoff` | `today_exhausted`); card copy derives from `window_reason`,
   never guessed by the LLM.

## Reality already on this branch (audited, do not redo)
- `agents-cli` scaffold, `compose.yaml` (Postgres 16), `alembic.ini`,
  `migrations/env.py`, `pyproject.toml` deps (adk, sqlalchemy, alembic,
  psycopg, pydantic-settings, pyyaml, pytest, ruff, black) — **already
  bootstrapped**. Step "2. Bootstrap with agents-cli" in the source prompt is
  already done; I will not re-run `agents-cli setup`.
- `app/core/{clock.py,config.py,db.py,models.py,scope.py}` exist, but
  `models.py` only has minimal `Event/WorkItem/Message` stubs
  (id, owner_user_id, actor_reference_key, resolved_person_id) — no
  title/time/status/body fields. Needs extending, not replacing.
- `app/identity/*` (resolve.py, matchers.py, roster.py, tier ladder) is a
  real, tested implementation (`tests/identity/*`, golden set). Per your
  scope note this stays untouched; the pulse ritual calls it through a
  `resolve_actor()` seam only, and does not extend the tier ladder.
- One Alembic migration exists (`2eda314919aa_initial_owner_scoped_schema`)
  covering users/events/work_items/messages/persons/identities/not_same_as/
  merge_log/unresolved_references/pending_confirmations. Missing for pulse:
  `goals, commitments, suppressions, weights, pulse_deliveries,
  feedback_events, raw_ingest_refs`, plus richer columns on events/work_items.
- **Missing entirely**: `app/fixtures/pulse/` (no README, no scenario files —
  contrary to the source prompt's "read first," there is nothing to read or
  copy; fixtures must be authored from scratch), `app/cli.py`, `typer` and
  `slack-sdk` deps, `docs/data_inventory.md`, any L2 connector, L5 salience,
  L6 agents/critic, L1 trigger handling, L7 delivery/cards.
- `app/agent.py` is still the ADK scaffold's placeholder weather agent —
  `root_agent` needs to become the real L6 coordinator, not a second entrypoint.

## Milestones (one commit each, tests first)

### M1 — deps, schema extension, fixture authoring
- Add `typer`, `slack-sdk`, `python-dateutil` to `pyproject.toml` (deps
  already otherwise present).
- Extend `app/core/models.py`: full `Event/Message/WorkItem` fields the
  pulse needs (title, start/end or timestamp, status, url, body-ref not
  body), add `Goal`, `Commitment`, `Suppression`, `Weight`, `PulseDelivery`,
  `FeedbackEvent`, `RawIngestRef`. Add `late_cutoff_local`,
  `pulse_fire_time_local`, `tz` to `User`.
- New Alembic revision on top of the existing baseline (not a new baseline).
  All new tables: `owner_user_id` non-nullable, indexed first, part of every
  unique constraint; `timestamptz`, `jsonb`, no naive datetimes.
- Author `app/fixtures/pulse/{normal_day,clear_day,series_suppression,...}.yaml`
  + `README.md` (the fixture contract) + `app/fixtures/pulse/critic/*` per
  decision 1 + `scripts/validate_pulse_fixtures.py` +
  `tests/test_pulse_fixtures.py`.
- **Tests first**: schema round-trip test (write+read every new table,
  owner-scoped uniqueness), two-owner isolation test extended to new tables,
  `validate_pulse_fixtures.py` green.
- Demo: `alembic upgrade head` from current head; isolation test passes;
  validator green.

### M2 — L2 connectors + L3 normalize + `seed`
- `SourceClient` protocol (`fetch(window, owner_user_id)`,
  `health() -> Healthy | Unauthorized | Error(stale_as_of)`).
- Calendar + Slack clients, one tracker (Linear; Jira registered as a stub).
  Fixture-backed and live paths selected by config; tests never hit network.
- Normalize into the extended `Event/Message/WorkItem` types; idempotent
  upsert on `(owner_user_id, source, external_id)`; recurring events
  expanded; provenance stamped; actor resolution goes through
  `resolve_actor()` (the identity seam), never a new matcher.
- `docs/data_inventory.md`: one row per field read.
- Tests first: per-connector fixture fetch → normalized rows, upsert
  idempotency, health degraded path, actor-resolution seam call recorded.
- Demo: `python -m app.cli seed --fixture normal_day` + row count.

### M3 — L5 salience (numeric only — narrows, does not pick the headline)
- `score(item, ctx) -> ScoredItem` with contributing terms retained for
  why-now string derivation downstream (L5 itself never writes prose).
- `candidate_focus` per decision 2, used to bias shortlist inclusion, not to
  make the final focus decision.
- Suppression + budget as one separate post-scoring gate returning
  `(surviving_items, removals)` per decision 3b; scopes
  `instance|series|temporal|global`, each TTL'd, narrower beats broader.
  Push: budget applies. Pull: budget + suppression both bypassed, L2
  permissions never bypassed.
- Weights: read-path only, lazy decay, clamp `[0.2, 3.0]`, structural floors.
- `PulseContext` output holds a **ranked shortlist of ~5–8 candidates**, each
  with its full score terms attached. L5's job is *whether to speak* and
  *narrowing the field*; it does not cut to 1–3 and does not choose the
  headline — that is L6's job in M4.
- No LLM, no randomness in this layer.
- Tests first: fixture → asserted shortlist + scores (5–8 items, not 1–3);
  decision-3a pre-gate-parity test (cron vs pull, same fixture, assert
  equality modulo excluded fields) at the shortlist level; decision-3b
  gate-accountability test on `series_suppression.yaml`.
- Demo: fixtures → asserted shortlist + score terms printed.

### M4 — L6 ranker + writer + critic (as built — supersedes the original
single-writer-agent sketch above; this repo has no Gemini/ADK credentials
configured, which shaped the design below more than originally planned)

Three separate modules, not one `pulse.py` LlmAgent, wired by a fourth:

- **`app/agents/ranker.py`** (`prompts/pulse_ranker.v1.md`): in — the L5
  shortlist (5–8 `ScoredItem`s, score terms only); out — `RankerResult
  {ordered_item_ids, rationale, prompt_id}`. Decides order and the cut to
  1–3. `fallback_rank()` is a deterministic score-order-then-cut
  implementation with no LLM call at all — it is simultaneously (a) what
  `app/fixtures/pulse/ranker/*` tests directly, since there is no live model
  to call in this environment, and (b) the actual "ranker fails → L5 score
  order" fallback the orchestrator uses. `rank()` accepts an optional
  `model_call`; on any exception (including "no API key") it falls back.
  `llm_rank()` is the real Gemini path (`app/agents/llm.py`'s
  `call_gemini_json`, single-shot JSON response, no ADK Runner). Python's
  stable sort means ties break by shortlist input order, never at random —
  covered by `ranker/tied_items.yaml`.
- **Code-side checks live in `app/pipeline/pulse.py`, not the ranker
  module** — `enforce_ranker_output()`: an id absent from the shortlist is a
  hard fail, discarding the *entire* ranker result for `fallback_rank`'s
  plain score order (logged); otherwise dedup + hard-truncate to 3. Tested
  directly, including the "bogus id" fallback-firing case the M4 demo
  requires.
- **`app/agents/writer.py`** (`prompts/pulse_writer.v1.md`): in —
  `WriterInputItem{item_id, title, score_terms, rationale}` for only the
  chosen 1–3 ids (titles are fetched from `Event`/`WorkItem` by the
  orchestrator, since L5's `ScoredItem` deliberately carries no title); out
  — `PulseItem{item_id, title, why_now, action}`. `fallback_render()` builds
  `why_now`/`action` only from `score_terms`, so it is provenance-safe by
  construction. `write()` rejects (falls back on) any model response whose
  `item_id` order doesn't match the input exactly — and
  `app/pipeline/pulse.py` also hard-asserts
  `[i.item_id for i in draft] == ordered_item_ids` right after the call, per
  the standing note that prompts alone won't hold that line. `llm_write()`
  is the real Gemini path, same shape as `llm_rank()`.
- **`app/agents/critic.py`** (`prompts/pulse_critic.v1.md` documents the
  contract): **deliberately not an LLM call** — every one of its checks
  (provenance via a name-span regex against `score_terms.person_waiting`,
  focus count 1–3, why-now presence, closed set, degradation line) is
  mechanically checkable, so a rule check is strictly more reliable than a
  model judgment here. `evaluate(draft, context) -> CriticResult{verdict,
  reasons: [(item_id, reason)]}` — `item_id` is `""` for whole-draft issues.
  Tested against all five `app/fixtures/pulse/critic/*` scenarios
  (`clean_pass`, `invented_name`, `missing_why_now`,
  `focus_count_exceeds_three` at 5 items, `missing_degradation_line`) plus
  closed-set/empty-list cases the fixtures don't cover. Reported everywhere
  (logs, card footer) as `critic_rules.v1`, not `pulse_critic.v1` — the
  prompt-file-shaped name would misleadingly imply a model call that never
  happens.
- **`app/pipeline/pulse.py`'s `run_pulse()`** wires L5 → ranker → checks →
  writer → writer-order assertion → critic → (≤1 revise, dropping
  critic-flagged items by id) → `RenderedPulse`. It is the only module that
  touches `OwnerScope`/DB (fetching titles) or the fixture-backed
  `SourceClient`s. The degradation line is computed once, up front, from
  `PulseContext.degraded_sources` — not left for the critic to catch
  reactively, so that check is defense in depth and normally never fires.
  Per-call log line (`_log_call`): step, prompt_id, `context_hash` (sha256
  of shortlist ids+scores), temperature, latency, token_count (`None` on
  every fallback path, since no tokens were spent). Ranker/writer stay
  behind plain function calls (`rank()`, `write()`) rather than a class
  hierarchy, so wiring a real ADK `LlmAgent` in later is a change inside
  those two files, not a pipeline rewrite.
- Not built in M4, built later (see "M4.1 — ranker/writer/critic rebuilt as
  real ADK agents" below): `app/agent.py`'s `root_agent` wired to L6 as a
  real conversational tool. At M4 time, `run_pulse()` was called directly
  by the CLI only.
- Demo: `python -m app.cli pulse --fixture normal_day` — prints 1–3 focus
  items with `why_now`, then a footer with all three prompt ids and the
  fallback/revise/skip flags.

**Known gap, must close before M6:** this environment has no Gemini/ADK
credentials, so every automated run of the fixture suite exercises
`fallback_rank`/`fallback_render`/the rule-based critic — never
`llm_rank`/`llm_write`, i.e. never the actual prompt files. A green suite
proves the degradation path works; it does not prove the prompts work, and
`RenderedPulse.ranker_used_llm`/`writer_used_llm` would silently stay
`False` forever without anyone noticing if the prompts were broken. Two
things guard against that:
- `test_run_pulse_reports_whether_the_llm_path_actually_ran`
  (`tests/unit/pipeline/test_pulse_orchestrator.py`): when credentials
  *are* present, asserts `ranker_used_llm`/`writer_used_llm` are both
  `True` — a fallback with credentials configured is treated as a real
  failure, not a pass. When they aren't, `app/cli.py pulse` prints a
  `DEGRADED:` line rather than silently shipping the fallback as if it
  were the real prompt output.
- `tests/live/test_llm_smoke.py`, marked `@pytest.mark.live` (deselected by
  default — `pyproject.toml`'s `addopts = "-m 'not live'"`; run with
  `uv run pytest -m live`) and `skipif` on missing credentials: one real
  `llm_rank` call and one real `llm_write` call against the `ranker/
  normal_day` fixture, asserting only invariants (closed set, count ≤ 3,
  why_now present, no name belonging to a different item) — never
  fixture-exact output, since models drift run to run.

**Update: closed.** `tests/live/test_llm_smoke.py` has passed against a
real Vertex AI key (`.env`: `GOOGLE_GENAI_USE_VERTEXAI=true` +
`GOOGLE_CLOUD_PROJECT` — application-default credentials were already
configured in this environment via `gcloud`). `app/agents/llm.py`'s
`creds_available()` now recognizes both the Gemini-API-key path and the
Vertex path. `pulse_ranker.v1.md`/`pulse_writer.v1.md` are no longer
unreviewed — a real `pulse` run produces genuine model-written why-now/
action text that correctly cites only facts in `score_terms`.

Three things fixed while wiring this in, all real, all test-first:
- `app/core/config.py`'s `Settings` used pydantic-settings' default
  `env_file` behavior, which forbids any `.env` key not declared as a
  field — a real `.env` carrying `GOOGLE_*`/`SLACK_BOT_TOKEN` broke
  `Settings()` outright. Fixed with `extra="ignore"`.
- Once real credentials were live, every unit test exercising
  `run_pulse()` started silently making real network calls — the
  "tests never hit network" principle broken exactly the way M2 through
  M6 protected against for connectors, just via a different door.
  `tests/conftest.py` now carries an autouse fixture forcing
  `creds_available()` False everywhere except `tests/live/` (which
  overrides it back in its own `conftest.py`) — unit tests exercise the
  fallback path deterministically regardless of what's configured in the
  environment; only `tests/live/` (still deselected by default) touches
  the real model.
- A live run surfaced the ranker occasionally mistyping one character of
  a 36-char UUID `item_id` when echoing it back — `enforce_ranker_output`'s
  closed-set check caught it and fell back correctly, proving the safety
  net works under real conditions rather than only in synthetic tests. Not
  fixed: shortening the ids exposed to the LLM (e.g. `i0`, `i1`) to reduce
  transcription-error-driven fallbacks is a worthwhile follow-up, flagged
  but not implemented — full UUIDs are more collision-proof but strictly
  worse for round-tripping through a model.

### M4.1 — ranker/writer/critic rebuilt as real ADK agents; root_agent wired

Requested explicitly: real ADK `Agent`/`SequentialAgent` objects instead of
M4's plain-function seam, following ADK best practice (agent-per-folder
under `sub_agents/`), plus wiring `root_agent` to actually do something —
both deliberate scope expansions of M4, not required by the original spec.

- **`app/core/adk_runner.py`** (new): `run_agent_sync(agent, initial_state,
  kickoff_text="Begin.")` — the generic sync/async bridge every agent below
  runs through. Seeds a fresh `InMemorySessionService` session with
  `initial_state`, runs a `Runner` to completion, returns final
  `session.state` as a plain dict. Same "raise on any failure, caller
  decides the fallback" contract `call_gemini_json`/`call_tool` already
  had.
- **`app/sub_agents/pulse/sub_agents/{ranker,writer,critic}/agent.py`**:
  one real `Agent` (= `LlmAgent`, confirmed identical in this ADK version)
  each, `output_schema` = a Pydantic model, `temperature=0`, instruction =
  the existing `prompts/pulse_*.v1.md` content verbatim (no rewrite
  needed — they were already written as agent instructions).
  `ranker_agent`/`writer_agent` are chained via **`app/sub_agents/pulse/
  agent.py`'s `pulse_pipeline = SequentialAgent(sub_agents=[ranker_agent,
  writer_agent])`** — writer's input (which the ranker never saw: titles)
  is merged from the ranker's ADK-produced state via a
  `before_agent_callback`, not a second Python call. `critic_agent` runs
  standalone, not inside the `SequentialAgent` — the existing "one revise
  loop" drops critic-flagged items in code and re-checks once, which fits
  two independent critic calls better than a fixed third sequence step.
  `app/agents/{ranker,writer,critic}.py`'s `llm_rank`/`llm_write`/
  `evaluate()` became thin delegators to these, keeping their external
  contracts (and every existing caller/test) unchanged.
- **Critic is now a real LLM-as-judge, fully replacing the rule-based
  `evaluate()`** (an explicit decision, not a stopgap — the old docstring's
  argument that "a rule check is strictly more reliable than a model
  judgment for structured facts" was tested, not assumed: a live run of
  the LLM judge against all five `app/fixtures/pulse/critic/*` scenarios
  matched every expected verdict, and on `missing_degradation_line` caught
  an unprovenanced name leaking into `action` that the old regex — which
  only ever scanned `why_now` — missed entirely. `pulse_critic.v1.md`'s
  contract didn't change; it's the literal instruction text now, not just
  documentation of what code checked. `CRITIC_PROMPT_ID` changed from
  `"critic_rules.v1"` to `"pulse_critic.v1"` (the old name specifically
  advertised "not a model call," which stopped being true). The five
  scenarios moved from a unit test (`tests/unit/agents/test_critic.py`,
  deleted) to `tests/live/test_critic_smoke.py` (`@pytest.mark.live`) since
  they now require a real model call.
- **Trade-off from real `SequentialAgent` chaining, not a bug**: ranker and
  writer used to be independent fallback stages (a writer failure never
  touched whether the ranker's LLM result was used). Running them as one
  atomic pipeline call means a failure anywhere in that call now falls
  back to *both* deterministic paths together, and a post-hoc ranker
  closed-set violation (`enforce_ranker_output`, still code, still runs
  after the pipeline call) now discards the writer's draft too, since it
  was written against the rejected item list — the writer no longer gets
  an independent fresh LLM retry against the corrected one.
  `fallback_rank`/`fallback_render` remain fully safe either way; this is
  documented in `app/pipeline/pulse.py`'s module docstring.
- **Critic gained a `creds_present` gate it never needed before** — the old
  rule-based critic ran unconditionally, credentials or not. A live run
  against `root_agent` caught the gap this created: unit tests (creds
  forced off by `tests/conftest.py`) started attempting a real,
  doomed-to-fail critic call. Fixed: `app/pipeline/pulse.py` now skips the
  critic step straight to `critic_skipped=True` when `creds_present` is
  False, rather than attempting a call that can only fail.
- **`app/tools/custom_tools.py`'s `get_morning_pulse`** (new): `root_agent`'s first real
  tool (previously only the ADK scaffold's toy `get_weather`/
  `get_current_time`). Thin per AGENT.md's tool rule — delegates to
  `render_pulse_text_for_owner()`, which runs the exact same
  `seed_live` -> `build_pull_trigger` -> `handle_trigger` path the CLI and
  Slack surfaces already use, so idempotency and the cross-user refusal
  (decision 4) aren't reimplemented for this third trigger surface. The
  owner user id comes from `ToolContext.user_id` — the real ADK session
  identity — never a model-supplied argument, matching this codebase's
  standing "never trust the model" posture. Design choice: an ADK
  conversational `user_id` *is* a Mentor `owner_user_id` directly (no new
  identity-linking table) — consistent with the CLI already addressing
  users by that same raw string and with decision 4's "no delegation
  mechanism" stance.
- **Two real, identical-shaped nested-event-loop bugs found live**, both
  in code that predates this rebuild but had only ever been exercised from
  a plain sync context (CLI, a background thread) until `get_morning_pulse`
  put it inside ADK's own already-running event loop: `app/core/
  adk_runner.py`'s `run_agent_sync` and `app/tools/mcp_config.py`'s
  `call_tool` (called via `seed_live` during a live pulse) both called
  `asyncio.run()` unconditionally, which raises "cannot be called from a
  running event loop" in that context. Neither crashed — existing
  try/except fallback paths caught the `RuntimeError` and degraded
  gracefully — but the LLM/connector path was silently never attempted at
  all, the exact "fallback indistinguishable from never attempted" gap
  `tests/live/test_llm_smoke.py` already exists to catch elsewhere. Fixed
  identically in both: detect a running loop
  (`asyncio.get_running_loop()`), and if there is one, run the bridge on a
  fresh worker thread with its own loop instead of on the current one.
- **Real live run, end to end, through `root_agent` as a genuine chat
  turn**: "give me my pulse" -> `get_morning_pulse` tool call -> real
  Slack + Linear MCP ingestion -> real `pulse_pipeline` (ranker + writer,
  one `SequentialAgent` call) -> real `critic_agent` -> a natural-language
  reply relaying the rendered card, no fallback anywhere in the chain.
- **Deprecation note**: this ADK version (`google-adk>=2.2.0,<3.0.0`) warns
  `SequentialAgent is deprecated in favor of Workflow and will be removed
  in a future version. Workflow cannot yet be used as an LlmAgent
  sub-agent` — `SequentialAgent` is still the correct/only tool for this
  today; revisit `app/sub_agents/pulse/agent.py` if/when `Workflow` gains
  that capability.
- **Not done**: `root_agent`'s toy `get_weather`/`get_current_time` tools
  are untouched (kept, not removed); no other `/mentor` subcommand or
  ritual (`prep`/`review`/`ledger`) got an ADK agent; no new identity-
  linking mechanism for the conversational surface beyond "ADK `user_id` =
  Mentor `owner_user_id`" — if that ever needs to be friendlier (a display
  name instead of typing a raw id), that's separate future work.

### M4.2 — `app/agents/` deleted; layout aligned to ADK's `sub_agents/`/`tools/` convention

Requested explicitly: `app/agents/{ranker,writer,critic,llm}.py` were a
leftover from before M4.1's ADK rebuild — each had already become a thin
delegator to its real agent in `app/sub_agents/pulse/sub_agents/<name>/
agent.py` (M4.1's note above), so the split no longer earned its keep.

- **Domain types + fallback logic merged into each agent's own file**:
  `RankerResult`/`fallback_rank`/`rank`/`llm_rank` moved into
  `app/sub_agents/pulse/sub_agents/ranker/agent.py`; `WriterInputItem`/
  `PulseItem`/`fallback_render`/`write`/`llm_write` into `.../writer/
  agent.py`; `CriticResult`/`evaluate` into `.../critic/agent.py`. Not a
  pure rename — these are genuinely two different things (an ADK `Agent`
  definition, and the deterministic non-LLM path used when the agent
  isn't attempted or fails) that now live in the same file because a
  reader asking "what does the ranker do" shouldn't have to check two
  places for the answer.
- **`app/agents/llm.py`'s `creds_available()` moved to `app/core/llm.py`**
  (a cross-cutting gate used by the pipeline, not agent-specific — doesn't
  belong triplicated across three sub_agents folders). Its sibling
  `call_gemini_json()` was **deleted, not moved** — dead code once
  `llm_rank`/`llm_write` started delegating to the real ADK agents in
  M4.1; nothing called it anymore, only comments describing what it used
  to be.
- **`app/agents/prompts/*.md` moved to `app/prompts/`** — matches ADK's
  own convention of a top-level `prompts/` directory, and removes the
  `app/agents/` mention that survived in every prompt file's own path
  lookup (`Path(__file__).resolve().parents[4] / "prompts" / ...`).
- **`app/ingest/mcp_client.py` moved to `app/tools/mcp_config.py`**, and
  each Live*Client's inline `_spec()` method body extracted into a
  standalone factory function there (`calendar_mcp_spec`/`slack_mcp_spec`/
  `linear_mcp_spec`/`jira_mcp_spec`) — "MCP config: which package to
  spawn, what env vars it needs," matching ADK's own `tools/` convention
  for shared external capabilities. **Real naming collision found doing
  this**: `app/tools.py` (the file holding `get_morning_pulse`) already
  occupied the `app.tools` name a package directory now needed. Resolved
  by moving `get_morning_pulse`/`render_pulse_text_for_owner` into
  `app/tools/custom_tools.py` — which is, not coincidentally, exactly
  where the convention says a plain-python (non-MCP) function tool
  belongs. `app/ingest/live_source.py`'s SourceClient classes (fetch/
  adapt/health logic — genuinely ingestion-layer code, not "a tool an
  agent calls") stayed in `app/ingest/`, now importing their specs from
  `app.tools.mcp_config` instead of defining them inline.
- Every relocated symbol kept its name; only import paths changed. Tests
  moved with their subject (`tests/unit/agents/` deleted, content merged
  into `tests/unit/sub_agents/pulse/sub_agents/{ranker,writer,critic}/
  test_agent.py`, `tests/unit/core/test_llm.py`,
  `tests/unit/tools/test_custom_tools.py`; `tests/unit/agents/
  fixture_loading.py` moved to `tests/fixture_loading.py` since it's
  shared by unit and `tests/live/` alike). Full unit suite green
  afterward (255 passed) — the only failure surfaced by this whole
  refactor was `test_seed_live_ingests_from_live_clients_and_reports_
  degraded` expecting an exact `degraded_sources` set that didn't yet
  account for Jira (added same session as this reorg), not a regression
  from the move itself.

### Live connectors (MCP) — status: Slack, Linear, Calendar, Jira, Google Docs all proven live

Requested after M6: real MCP-backed `SourceClient` implementations for
Calendar/Slack/Linear, matching the "MCP servers as the client" approach
(spawn existing published servers over stdio, not a custom API wrapper).
Full setup guide: `docs/live-connectors.md`.

- **`app/tools/mcp_config.py`**: generic stdio transport
  (`McpServerSpec`, `call_tool()`), one spawn per call, plus a factory
  function per connector (`calendar_mcp_spec`/`slack_mcp_spec`/
  `linear_mcp_spec`/`jira_mcp_spec`). Bridges the MCP SDK's asyncio-first
  API to `SourceClient.fetch()`'s synchronous interface via
  `asyncio.run()`. (Originally `app/ingest/mcp_client.py`; moved when
  `app/agents/` was deleted and its contents merged into `app/sub_agents/`
  — see the note at the end of this document.)
- **`app/ingest/live_source.py`**: `LiveCalendarClient`
  (`@cocal/google-calendar-mcp`'s `list-events`), `LiveSlackClient`
  (`@modelcontextprotocol/server-slack`'s `slack_list_channels` +
  `slack_get_channel_history` — that package is marked deprecated
  upstream, still functional), `LiveLinearClient` (`mcp-server-linear`'s
  `linear_search_issues`). Tool names and argument shapes are **real** —
  each server's `list_tools()` was introspected live (no credentials
  needed just to list tools; Calendar's server requires OAuth credentials
  even to start, so its schema came from the package's published docs
  instead). Response *adapters* (`_adapt_calendar_event`,
  `_adapt_slack_message`, `_adapt_linear_issue`) are written defensively
  against each server's documented/typical response shape and have never
  processed real data.
- `app/cli.py live-status`: reports `health()` (credential presence only)
  for all three.
- **Now wired into `app/ingest/seed.py` / `app/cli.py seed`** via
  `seed_live(session, owner_user_id, clock=None)`, added alongside (not
  replacing) `seed_fixture`. `seed --live --user <id>` calls it; `--fixture`
  is now optional on `seed`/`ingest` and required only when `--live` is
  absent. Key difference from `seed_fixture`: there is no fixture to source
  `pulse_fire_time_local`/`tz`/`late_cutoff_local` or roster/commitments/
  suppressions from, so `seed_live` only ever updates an *existing* user's
  ingested rows (events/work_items/messages) for today — it never creates a
  user and never touches the roster, same as `_reset_owner_ingested_data`'s
  existing contract. Each source's `health()` is checked per-call (not
  cached at import time) so a partially-configured environment (e.g. Slack
  + Linear set, Calendar not) degrades per-source exactly like
  `seed_fixture`'s `unhealthy_sources`. A `fetch()` that raises at runtime
  (network/API failure — `health()` only checks credential presence, not
  reachability) is now also caught and folded into `degraded_sources` as
  `status: "error"` rather than crashing the whole seed, covered by
  `test_seed_live_catches_fetch_failures_as_degraded`.
- `tests/unit/ingest/test_live_source.py`: the response adapters are unit
  tested with synthetic data (pure functions, no transport). `tests/live/
  test_mcp_smoke.py`: per-service gated + skipped live tests, ready to run
  once credentials exist — **until each one passes, treat that service's
  adapter as unreviewed**, the same standing this repo held
  `pulse_ranker.v1.md` to before its own live test passed.
- **Slack is now proven live** (`SLACK_BOT_TOKEN`/`SLACK_TEAM_ID` set,
  `tests/live/test_mcp_smoke.py -k slack` passes). The live run surfaced a
  real bug the synthetic unit tests couldn't: `slack_list_channels` can
  list channels the bot has never been invited to, and
  `slack_get_channel_history` for those returns `{"ok": false, "error":
  "not_in_channel"}` — a valid *dict*, not an exception, not a list. The
  original adapter did `history.get("messages", history)`, which for that
  shape falls through to returning the whole error dict, then iterated its
  string *keys* (`"ok"`, `"error"`) as if they were message dicts, crashing
  on `raw.get("ts", "")` inside `_adapt_slack_message`. Fixed in
  `LiveSlackClient.fetch()`: channels where `history.get("ok") is False`
  are now skipped rather than treated as data. Covered by a new unit test,
  `test_slack_fetch_skips_channels_the_bot_cannot_read` (mocks `call_tool`
  to return that exact error shape for one channel and real messages for
  another, asserts only the readable channel's message survives).
  Calendar and Linear credentials are still not set up in this
  environment — their adapters remain unreviewed until each has its own
  live pass.
- **Linear is now proven live** (`LINEAR_API_KEY` set,
  `tests/live/test_mcp_smoke.py -k linear` passes). Two real bugs found:
  1. `mcp-server-linear` reads its token from the spawned process's
     `LINEAR_ACCESS_TOKEN` env var, not `LINEAR_API_KEY` — despite the
     name similarity to our own config var, they're different. Passing
     the wrong env var name meant the server started with no token at
     all, and every tool call failed with `MCP error -32600: Not
     authenticated. Call linear_auth first.` — a config/spawn bug, not an
     OAuth requirement. Fixed in `LiveLinearClient._spec()`: the env dict
     now maps our `LINEAR_API_KEY` value onto the server's expected
     `LINEAR_ACCESS_TOKEN` key.
  2. `linear_search_issues`'s real response shape is `{"issues":
     {"pageInfo": {...}, "nodes": [...]}}` — a GraphQL connection, one
     level deeper than the flat `{"issues": [...]}` the adapter assumed.
     `raw.get("issues", raw)` returned the connection dict itself
     (`{"pageInfo": ..., "nodes": ...}`), and the code then iterated its
     string *keys* as if they were issues, crashing in
     `_adapt_linear_issue` the same way the Slack bug did. Fixed to
     unwrap `.get("nodes", [])` when the "issues" field is itself a dict.
     Covered by `test_linear_fetch_unwraps_graphql_connection_shape`.
- **Calendar is now proven live too** (`GOOGLE_OAUTH_CREDENTIALS` set, the
  interactive `npx @cocal/google-calendar-mcp auth` consent flow completed,
  `tests/live/test_mcp_smoke.py -k calendar` passes). What unblocked it:
  the shared project's "Insufficient permissions to check the enablement
  status of this product" error (hit enabling the Calendar API) was a
  real IAM gap, not a code problem — resolved once the account got
  Service Usage Admin on `aidodev-agents-hackathon-2026`, so the earlier
  "separate personal GCP project" workaround wasn't needed after all.
  One real bug found and fixed: `list-events`'s `calendarId` argument is
  **required**, not optional as the adapter's own comment assumed from the
  tool's docstring hints — a live call failed with `MCP error -32602:
  Invalid input at calendarId` until `LiveCalendarClient.fetch()` was
  fixed to always pass `calendarId: "primary"` (the account's default
  calendar; selecting a non-default calendar isn't supported yet).
  Covered by `test_calendar_fetch_passes_the_required_calendar_id`. All
  three live connectors (Calendar, Slack, Linear) are now proven and flow
  into `seed --live` together.
- **Jira added as a fourth connector** (`docs/whiteboards/
  Data_Flow_Diagram_For_Command_Center_Brief.excalidraw` shows Jira, not
  Linear, downstream of the Fetch orchestrator — both are now wired,
  Linear kept as-is). `LiveJiraClient` (`app/ingest/live_source.py`) uses
  `mcp-jira-cloud`'s `jira_search_issues` tool with a bounded JQL
  (`assignee = currentUser() AND resolution = Unresolved`) — the obvious
  `jira_get_my_open_issues` tool was tried first but its response is
  near-empty (`{summary, description, key}` only, no status/assignee/
  dates), discovered only by calling it live. Proven via a real
  create-issue -> search -> delete round-trip against a live Jira Cloud
  site: the search response is pre-flattened by the MCP server
  (`status`/`assignee`/`reporter` as plain strings, not nested objects —
  no email or accountId available, so `actor_reference_key` is built from
  the assignee's display name), and there is no `self`/url field, so the
  browse URL is built manually from `JIRA_BASE_URL` + the issue key.
  `FixtureJiraClient` (`app/ingest/fixture_source.py`) replaces the old
  registered-stub `JiraClient` — both it and `FixtureLinearClient` now
  filter the shared `work_items` fixture list by each row's own `source`
  field, so a fixture exercising both trackers doesn't double-count.
  Wired into `seed_live`, `app/cli.py`, `app/tools/custom_tools.py`'s
  `get_morning_pulse`, and `app/triggers/{cron_scheduler,slack_command}.py`
  alongside the other three. Requires `JIRA_BASE_URL`/`JIRA_EMAIL`/
  `JIRA_API_TOKEN`.
- **Google Docs added as a fifth connector**, mapped to `Message` not a
  new canonical type (comments/threads are message-shaped: an actor
  referencing you, a `body_ref`, no full content stored — same call as
  Jira PRs-as-WorkItem). `LiveGoogleDocsClient`
  (`app/ingest/live_source.py`) uses `@a-bonus/google-docs-mcp`'s
  `listDriveFiles` (enumerate recent docs) + `listComments` (per doc,
  kept only if unresolved) — no single "comments across all my docs"
  endpoint exists, same two-step shape as Slack's channel enumeration.
  Auth is a different shape from Calendar's: `GOOGLE_CLIENT_ID`/
  `GOOGLE_CLIENT_SECRET` (the same values already inside Calendar's OAuth
  client JSON — reused rather than creating a second OAuth client, since
  scopes are requested at consent time, not baked into the client) plus a
  separate one-time `npx -y @a-bonus/google-docs-mcp auth` flow that
  stores its own refresh token at `~/.config/google-docs-mcp/token.json`.
  Real problems hit getting this far, in order:
  1. The consent flow's fixed scope request (Docs/Drive/Sheets/
     script.external_request/Gmail-modify/Calendar-events — this package
     always requests all six, no way to narrow it) was rejected with a
     bare `Error 400: invalid_request` until every one of those six
     scopes was explicitly added to the GCP project's OAuth consent
     screen's configured scope list — enabling the corresponding APIs
     alone wasn't enough, a real gap between "API enabled" and "scope
     grantable" that cost a few failed attempts before the actual cause
     (missing consent-screen scope registration, not a code or client
     problem) was clear.
  2. The Docs/Drive APIs also needed enabling on the shared GCP project
     (only Calendar's was on) — `gcloud services enable
     docs.googleapis.com drive.googleapis.com`.
  3. Once authorized, a real 20-doc fetch (`listDriveFiles` maxResults=20
     followed by one `listComments` call per doc) took **~2.5 minutes**
     end to end — `app/tools/mcp_config.py`'s "one spawn per call"
     design is fine for every other connector's 1-2 calls per fetch, but
     this connector's N+1 shape multiplies that spawn overhead badly.
     Fixed by cutting `LiveGoogleDocsClient._MAX_DOCS` from 20 to 6
     (recency matters more than exhaustiveness here); re-proven live at
     ~43 seconds.
  Comment shape (confirmed via a live add-comment -> listComments/
  getComment -> delete round-trip) is pre-flattened the same way Jira's
  server is: `author` is a plain display-name string, no email or
  accountId, and there's no per-comment permalink — `url` points at the
  document itself. Wired into `seed_live`, `app/cli.py`,
  `app/tools/custom_tools.py`'s `get_morning_pulse`, and
  `app/triggers/{cron_scheduler,slack_command}.py` alongside the other
  four.

### Delivery (`app/delivery/slack_deliverer.py`) — status: proven live

Built in M6 with unit tests only (mocked `slack_sdk.WebClient`); never
wired into the CLI or run against a real workspace until now. `pulse`
gained two new flags: `--deliver-to <slack_user_id>` actually posts the
rendered card via `SlackDeliverer` (off by default — every other
`pulse`/`demo-card` invocation still only prints); `--channel-id
<slack_channel_id>` (only meaningful alongside `--deliver-to`) simulates a
channel invocation — the card still only ever goes to the DM, plus an
ephemeral "sent you that in DM" ack in that channel. Both are covered by
`tests/test_cli.py` with a fake `SlackDeliverer`, so unit tests never
touch the network.

A real send against the Slack workspace set up for the ingestion
connectors surfaced two bugs:
- The bot token only had the `channels:history`/`channels:read`/
  `users:read` scopes added for ingestion — posting needs `chat:write`
  too. First live attempt failed with `missing_scope`. Fixed by adding
  the scope and reinstalling the app (issues a new bot token).
- A D-prefixed Slack ID (a specific DM *conversation* id) doesn't
  resolve via `chat.postMessage`'s `channel` param the way a user id
  does — failed with `channel_not_found`. `chat.postMessage` auto-opens
  (or reuses) the DM when given an actual user id (`U...`), which is
  what `--deliver-to` expects and documents.
- Slack additionally warned (not an error) that `chat_postMessage` was
  missing a top-level `text` argument, used for push notifications and
  screen readers when blocks can't render. Added a static fallback
  string rather than leaving it unset. Covered by a new assertion in
  `test_dm_only_invocation_posts_only_to_the_user`.

### Slash-command trigger (`/mentor pulse`) — status: Socket Mode built, unproven live

The first inbound Slack surface — everything before this was Slack as
*delivery only* (`--deliver-to`); this is Slack as an *entrypoint*
(AGENT.md §1's "Slack DM cards + slash commands"). Built twice: an HTTP
route first, then a Socket Mode listener once live testing showed the
actual Slack app has Socket Mode enabled — see the two transports below.

- **`users.slack_user_id`** (migration `050b49816c97`, nullable, unique):
  nothing else in the schema maps "which Slack user typed this" to an
  `owner_user_id`. Set via the `link-slack --user <id> --slack-user-id
  <U...>` CLI command — a one-time manual step per user, matching the
  single-user reality of every other command in this repo so far.
- **`app/triggers/slack_command.py`** is the transport-agnostic core, used
  by both transports below unchanged: `parse_slash_command` (payload dict
  -> typed value), `resolve_owner_user_id` (looks up `slack_user_id`),
  `run_pulse_command` (the actual work: `seed_live` -> `build_pull_trigger`
  -> `handle_trigger` -> `SlackDeliverer.deliver`). A `seed_live` failure
  is logged and swallowed, not raised — the pulse still runs off whatever
  was already ingested rather than going silent on a live-connector hiccup.

**Transport 1 — HTTP (`POST /slack/commands`), status: built, dormant.**
Assumes Slack delivers slash commands as an inbound HTTP POST to a
registered Request URL — true when Socket Mode is off, which turned out
not to be the case for this Slack app once live testing began (an `ngrok`
tunnel + registered slash command never received a single request; Slack's
UI said "Socket Mode is enabled, you won't need to specify a Request URL"
the moment the command was saved). Kept in the codebase, unused for now,
in case Socket Mode is ever disabled.
- **`app/triggers/slack_signature.py`**: verifies Slack's
  `X-Slack-Signature`/`X-Slack-Request-Timestamp` HMAC-SHA256 against
  `SLACK_SIGNING_SECRET`, rejecting requests older than 5 minutes (replay
  protection). Pure function, unit tested with synthetic secrets.
- **`app/triggers/slack_router.py`**: the FastAPI route, deliberately kept
  in its own module (not directly in `app/fast_api_app.py`) so unit tests
  can hit it without paying that module's ADK-scaffold import cost (~20s:
  `google.auth.default()`, a Cloud Logging client, full ADK app init).
  Schedules `run_pulse_command` as a **FastAPI background task** after
  acking — Slack requires an ack within 3 seconds, and a live MCP
  round-trip across three connectors routinely takes 10-20s, so the pulse
  itself cannot run inline in the request. Mounted via
  `app.include_router(slack_router)` in `app/fast_api_app.py`.
- Tests: `tests/unit/triggers/test_slack_signature.py`,
  `tests/unit/triggers/test_slack_router.py`.

**Transport 2 — Socket Mode, status: proven live.** Slack delivers slash
commands over a persistent outbound WebSocket instead, authenticated by a
new `SLACK_APP_TOKEN` (`xapp-...`, from "Basic Information" -> "App-Level
Tokens", `connections:write` scope) — no public URL or Request URL needed.
- **`app/triggers/slack_socket_listener.py`**: built on `slack_sdk`'s
  bundled `socket_mode.builtin.SocketModeClient` — no new dependency for
  the transport itself. `handle_socket_mode_request(client, req,
  session_factory)` acks *every* envelope immediately regardless of type
  (a Socket Mode protocol requirement, not just for `slash_commands`),
  then for `text in ("", "pulse")` resolves the owner and schedules
  `run_pulse_command` on a background `threading.Thread` — same 3s ack
  budget problem as the HTTP transport, same fix. `main()` wires up the
  real `SocketModeClient` + `WebClient` and blocks forever; run standalone
  with `uv run python -m app.triggers.slack_socket_listener` (no `uvicorn`
  server, no `ngrok` required — it makes an outbound connection to Slack).
- **Real live run**: `/mentor pulse` typed in Slack was acked instantly,
  ran the full pipeline against real Slack + Linear data with the real
  ranker/writer LLM path (confirmed via `gen_ai.choice` telemetry), and
  delivered a real DM. First attempt failed silently (no card, no error to
  the user) with `SLACK_BOT_TOKEN not configured` logged server-side — the
  module never called `load_dotenv()` (unlike `app/cli.py` and
  `app/fast_api_app.py`, which both do), so `os.environ` never picked up
  `.env` in that process at all; `get_settings().slack_app_token` still
  worked because pydantic-settings reads the `.env` file itself,
  independent of `os.environ` — which is exactly why the socket connection
  itself came up fine while delivery silently no-opped. Fixed by adding
  `load_dotenv()` to the module. The only other errors observed were from
  the pre-existing Cloud Logging IAM gap (Task #44,
  `aidodev-agents-hackathon-2026`), unrelated to this feature.
- **Progress UX** (added after the first live run, per a live request):
  channel-originated slash commands now get an immediate `"⏳ Working on
  your pulse..."` ephemeral message via `response_url`
  (`_post_progress_message`, only when `channel_id` starts with `C`) —
  completion is still signalled by the existing "sent you that in DM"
  ephemeral ack (`SlackDeliverer.deliver`'s `channel_id` param), so this
  only adds the missing start-of-work signal, nothing about completion
  changed. DM-originated slash commands (`channel_id` starts with `D`)
  deliberately get no progress message — redundant inside a DM you're
  about to receive the card in.
- **Natural-language DM trigger** (same addition): `events_api` envelopes
  where `event.type == "message"`, `channel_type == "im"`, no `bot_id`/
  `subtype` (skips the bot's own messages and edits) are matched against
  `PULSE_ALIASES` — a hardcoded copy of `app/intents/pulse.md`'s own
  `aliases` frontmatter (`pulse, morning, brief, today`; the non-English
  ones dropped for now), manually kept in sync, matching that file's
  `tier_hint: B` (keyword match, not full NLU — "brief" matching inside an
  unrelated word is a known, accepted v1 limitation). A slash command has
  no message to react to (no `ts` in its payload), but a DM message does —
  so this path adds a real `hourglass_flowing_sand` reaction immediately
  (`SlackDeliverer.add_reaction`, new alongside `remove_reaction` and
  `send_text_message`, all thin `reactions_add`/`reactions_remove`/
  `chat.postMessage` wrappers that swallow `already_reacted`/`no_reaction`
  as non-failures), then swaps it for `white_check_mark` once
  `run_pulse_command` finishes. An unlinked user gets a plain DM reply
  instead of a reaction. A small bounded in-memory dedup on `event_id`
  guards against Slack's own event redelivery causing a double reaction/
  double pulse — process-lifetime only, resets on restart, accepted as
  enough since redeliveries happen within seconds.
- Tests: `tests/unit/triggers/test_slack_socket_listener.py` (every
  envelope-type/subcommand/linked-vs-unlinked/progress-message/DM-alias/
  dedup branch, fake client + fake session factory, no real WebSocket),
  `tests/unit/delivery/test_slack_deliverer.py` (the three new methods).
- **Not done**: any subcommand besides `pulse`, real NLU (alias matching
  only), reacting to channel messages/@mentions (DM only), and the
  progress-UX / reaction-swap pieces above have not yet been proven live
  themselves (only the base slash-command path has) — same "prove it
  live" standing applies until each is exercised for real.

### M5 — L1 triggers (as built)
- **`app/triggers/pulse_trigger.py`** (pure, zero DB/LLM):
  `TriggerEvent(kind, trigger: "cron"|"pull", owner_user_id, requested_at,
  params)`. `build_cron_trigger(owner_user_id, requested_at)` for the
  scheduled path. `build_pull_trigger(requesting_user_id,
  target_owner_user_id, requested_at, date=None)` for `/mentor pulse
  [date]` — raises `CrossUserPulseRequestError` whenever
  `requesting_user_id != target_owner_user_id`, unconditionally (decision
  4): no grant parameter exists on the function at all (asserted directly
  via `inspect.signature` in `test_cross_user_refusal_has_no_grant_escape_
  hatch`), so there's no escape hatch to audit later.
- **`app/salience/assemble.py`'s `select_window(user, clock, events_today)`**
  implements decision 5's window selection: an un-elapsed item today
  (`ends_at > now`) wins outright; otherwise past `user.late_cutoff_local`
  (a per-user column, default 21:00 — `config.py` never holds the effective
  value) shows tomorrow as `late_cutoff`; otherwise (before cutoff, nothing
  left) also tomorrow, as `today_exhausted`. `PreGateContext`/`PulseContext`
  both carry `window_date` + `window_reason` now, so card copy derives from
  `window_reason` rather than being guessed by the writer.
- **`app/triggers/dispatch.py`'s `handle_trigger(scope, clock,
  source_clients, event, dry_run=False)`** is the only DB-touching part of
  L1: resolves `local_date` (from `event.params["date"]` if given, else
  `requested_at` in the user's tz), then **idempotency is cron-only** — a
  retried cron finding an existing `PulseDelivery` row for
  `(owner_user_id, "pulse", local_date, "cron")` skips without re-running
  the pipeline. This deliberately deviates from the plan's original wording
  ("idempotent on (owner_user_id, ritual, local_date, trigger)" read as
  covering both triggers): `pulse.md`'s own edge-case table says "Called
  twice in one day | Second call is fine (pull)" — a second pull must never
  be silently blocked. Pull always runs `run_pulse()` again; a delivery row
  already existing for that day's pull gets updated in place (not a second
  insert, which would violate the table's unique constraint) so M6 can
  later read it back to render "since HH:MM" deltas.
- `pulse.md`'s "Requested for someone else" edge case rewritten in the same
  commit to say cross-user requests always refuse — no `§6.2` grant record
  language (doc fix travels with the code per AGENT.md's rule).
- Tests first: the decision-5 table (23:40→`late_cutoff`, 21:05 with a
  21:30 event→`today`, 20:55 empty-remainder→`today_exhausted`,
  09:00→`today`, per-user cutoff override moves only that user's boundary);
  cron-retry idempotency; pull-not-blocked (a real bug caught by this test —
  the first implementation deduped pull too); cross-user refusal with the
  no-escape-hatch check above.
- A second real bug caught mid-milestone: `app/cli.py`'s `shortlist`/`pulse`
  commands used `SystemClock()` (real wall-clock time), not the fixture's
  frozen `now` — harmless before M5, but decision 5's window selection
  makes real time-of-day observable, so a demo run could silently show
  "tomorrow" depending on when it happened to be run. Fixed to `FrozenClock`
  pinned to the fixture's `now`, same as every test in this repo.
- Demo: `python -m app.cli pulse --fixture normal_day --user usr_amal
  --trigger cron --dry-run` — prints `window_date`/`window_reason`, then the
  same Focus/Owed/footer output as M4, without touching `PulseDelivery`.
  Run twice without `--dry-run` to see the idempotent-skip path; run
  `--trigger pull` twice to see it NOT skip.

### M6 — L7 delivery + state (as built)
- **`app/salience/types.py`/`assemble.py`**: `DayEventSummary` +
  `day_events` added to `PreGateContext`/`PulseContext` — every event on
  `window_date`, chronological, not just the ones that made the
  competitive shortlist (the "Day" section needs the full day; the
  shortlist is a competition among a subset). `has_dossier` is
  structurally present, always `False` — F1 (pre-meeting dossier) isn't
  built. `owner_tz` also added to both context types (see the timezone bug
  below).
- **`app/triggers/dispatch.py`**: `TriggerResult` gained
  `previous_item_ids`/`previous_delivered_at`, captured from the existing
  `PulseDelivery` row (if any) *before* it's overwritten — this is what
  lets `cards.py` render "since HH:MM" on a second same-day pull.
- **`app/delivery/cards.py`**: `build_card(trigger_result) -> PulseCard`,
  then two independent renderers, `render_text()` and `render_blocks()`
  (Block Kit JSON), off the same structured `PulseCard` so neither format
  can drift from what the pipeline produced. Fixed order Focus → Day →
  Owed → Suggested focus, degradation line on top, footer with prompt ids
  + context hash. `suggested_focus` is literally `focus[0].action` — never
  a new item, never a new LLM call. "N of M — ask for the rest" appears
  whenever the pre-cut shortlist was larger than what shipped.
- **`app/delivery/affordances.py`**: `snooze_item()` writes an
  `instance`-scope `Suppression` (1-day TTL, `app/delivery/config.py`);
  `record_feedback()` appends a `FeedbackEvent`, rejecting anything outside
  `{up, down}`.
- **`app/delivery/slack_deliverer.py`**: `SlackDeliverer`, gated on
  `SLACK_BOT_TOKEN` exactly like `app/agents/llm.py`'s `creds_available()`
  pattern — `enabled` is `False` and `deliver()` is a no-op without a
  token, verified without ever importing `slack_sdk`. Channel invocation
  (`channel_id` passed to `deliver()`) always posts the pulse itself only
  to DM, plus a separate ephemeral acknowledgement in the channel — never
  posts a pulse publicly, verified with a fake `WebClient` asserting the
  call sequence.
- **`app/cli.py`**: `pulse` now renders through `cards.py` (text + Block
  Kit JSON) instead of hand-formatted lines; `--fixture` on `pulse`
  defaults to `normal_day` (see the bug below). Added `ingest` (identical
  to `seed` today — same fixture-backed path; the names diverge once a
  live connector exists), `demo-card` (self-seeding — doesn't require a
  prior `seed` call, dry-run, never touches `PulseDelivery`), `validate`
  (wraps `scripts/validate_pulse_fixtures.validate_all()`).
- Tests first: card section-order test, degradation-line-before-Focus,
  "N of M" present/absent, `suggested_focus` traced to `focus[0]`,
  since-note absent-then-present across two pulls, affordance
  write-effects tests, `SlackDeliverer` no-op-without-token +
  channel→DM-redirect tests.

**Two real bugs caught during verification, both test-first:**
- The plan's own §3 command block calls `pulse --user usr_amal --trigger
  pull` with no `--fixture` — but every connector is fixture-backed, so the
  CLI has always required one. Defaulted `pulse --fixture` to `normal_day`
  so the documented command runs as written; `seed`/`demo-card` keep
  `--fixture` required since the block always passes it explicitly there.
- Running the real §3 block surfaced a genuine display bug: Postgres
  `timestamptz` round-trips every value tagged UTC regardless of the
  offset used at insert time, so `cards.py` formatting `starts_at`
  directly showed a 9am Paris meeting as "07:00". Added `owner_tz` to
  `PreGateContext`/`PulseContext` (from `assemble.py`'s already-resolved
  `User.tz`) and convert every displayed timestamp
  (`day_events[].starts_at`, the since-note's `previous_delivered_at`) to
  it in `build_card()` before formatting.
- Demo: the full §3 command block, run for real, output captured in the
  commit message.

## Cross-cutting invariants (tests, not habit)
- No layer calls anything but the layer directly below it (grep-checked in
  M6's final verification pass).
- No LLM call in L1–L5 (grep-checked).
- `docs/data_inventory.md` has a row for every field actually read by M2.
- Logging is IDs/counts/decisions only — grep log call sites for message
  bodies.

## Verification (run for real before any milestone is called done)
```
uv sync
docker compose up -d db && uv run alembic upgrade head
uv run pytest -q
uv run ruff check . && uv run black --check .
uv run python -m app.cli seed --fixture normal_day
uv run python -m app.cli pulse --user usr_amal --trigger pull
uv run python -m app.cli pulse --user usr_amal --trigger cron --dry-run
uv run python -m app.cli demo-card --fixture clear_day
uv run adk run app
```

## Not doing (explicit out-of-scope, stub with `TODO(...)`)
- L4 identity tiers 0–5 extensions/confirmation asks/Friday batching —
  `resolve_actor()` seam only, resolves by exact provider ID or primary
  email, unmatched → raw handle.
- Cross-user delegation of any kind (decision 4 — not deferred, refused).
- Rituals beyond Owed-reads-from-ledger.
- Write-back to any external system.
- Weight learning + nightly consolidation.
- Auth/signup, encryption hardening, MCP transport (seam kept only).

## Risks
- Extending `Event/WorkItem/Message` risks breaking `tests/identity/*`,
  which already depend on the current minimal columns — M1's schema test
  must run the full identity test suite, not just the new tables, before
  the migration is considered safe.
- Postgres-only from here: SQLite path in `core/db.py` stays for the
  single-user local install per AGENT.md, but pulse features are only
  tested against Postgres (compose) to match the "reality" callout in the
  source prompt.

## Doc fixes traveling with code (per "fix the doc in the same commit")
- `AGENT.md` §9 and `.agents/workflows/phase-gate.md`: morning pulse moves
  ahead of `/mentor prep` as the first ritual shipped.
- `app/intents/pulse.md` §6.2: delegation grant language removed per
  decision 4.
