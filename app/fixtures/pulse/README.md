# Pulse fixtures — the contract

Two kinds of fixture live here, and `scripts/validate_pulse_fixtures.py` checks
both. Neither kind talks to a real connector, a real DB, or a real LLM — they
are the deterministic inputs M2–M6 build and test against.

## 1. Day fixtures (`*.yaml` at this level)

A day fixture is a synthetic day of raw-normalized data for one owner: enough
to drive `seed` (M2), scoring (M3), the writer/critic (M4), triggers (M5), and
delivery (M6) end to end.

Required top-level keys:

| Key | Shape | Notes |
|---|---|---|
| `name` | string | must match the filename stem |
| `description` | string | one or two sentences, what this fixture is testing |
| `owner` | `{id, tz, pulse_fire_time_local, late_cutoff_local}` | `pulse_fire_time_local`/`late_cutoff_local` are `HH:MM` strings |
| `now` | ISO 8601 datetime, tz-aware | the fixture's frozen "current time" — never `datetime.now()` |
| `people` | list of `{id, canonical_name, primary_email, is_self}` | optional; only needed if events/messages/commitments reference a `person_id` |
| `events` | list | see below |
| `work_items` | list | see below |
| `messages` | list | see below |
| `commitments` | list | see below |
| `goals` | list | optional — see below |
| `one_on_one_notes` | list | optional — see below |
| `suppressions` | list of `{scope, target_ref, reason, expires_at}` | optional; `scope` is one of `instance\|series\|temporal\|global` |
| `unhealthy_sources` | map of `source -> {status, stale_as_of?}` | optional; `status` is `unauthorized\|error` (`error` requires `stale_as_of`); read by `app/ingest`'s fixture-backed `SourceClient.health()` |

Item shapes (all fields required unless marked optional):

- **event**: `external_id, source, title, starts_at, ends_at, status,
  actor_reference_key`, optional `url, series_id`.
- **work_item**: `external_id, source, title, status, actor_reference_key`,
  optional `url, due_at, updated_at`.
- **message**: `external_id, source, channel, sent_at, is_dm, body_ref,
  actor_reference_key` — `body_ref` is a fixture-only pointer
  (`fixture://...`), never actual message content (AGENT.md privacy rule
  applies to fixtures too).
- **commitment**: `description, promised_at, status`, optional
  `promised_to_person_id, due_at, delivered_at, source_reference_key`.
- **goal** (source=notion Objectives/Key Results/Career Goals — see
  `app/ingest/live_source.py`'s `LiveNotionGoalsClient`): `source,
  external_id, title, goal_type` (`objective|key_result|career_goal`),
  optional `status, parent_external_id, quarter, progress, current_value,
  target_value`.
- **one_on_one_note** (source=notion "1:1 Notes" db — see
  `LiveNotionNotesClient`): `source, external_id`, optional `title,
  meeting_id, linked_key_result_external_ids`.

An `expected` block records what downstream milestones assert against this
fixture (shortlist size, which item refs should carry `candidate_focus`,
which sources are degraded). It is documentation for test authors, not
machine-checked by the validator — the milestone's own tests assert against
it.

## 2. Critic fixtures (`critic/<scenario>/`)

Each scenario is a directory of exactly three files, per the M4 critic design
(decision 1 of the morning-pulse plan). The critic
(`app/sub_agents/pulse/sub_agents/critic/agent.py`) is now a real LLM-as-judge,
not deterministic code — these fixtures serve double duty as unit tests
(`tests/unit/pipeline/`, mocking the agent call) and as the eval dataset a
live run is checked against (`tests/live/test_critic_smoke.py`):

- `draft_card.yaml` — the L6 writer's output before the critic sees it:
  `prompt_id, temperature, degradation_line, items[]` (each `{item_id, title,
  why_now, action}` — `app/sub_agents/pulse/sub_agents/writer/agent.py`'s
  `PulseItem` shape), plus
  optionally a deliberately invalid field to trigger a specific rejection (an
  `item_id` absent from `context.yaml`'s shortlist, a `why_now` citing a fact
  not in that item's `score_terms`, more than 3 `items` entries, or a `null`
  `degradation_line` when the context says a source is unhealthy).
- `context.yaml` — the `PulseContext` shortlist the draft was supposedly
  written from: `shortlist[]` (each `{item_id, score, score_terms,
  candidate_focus}`), `degraded_sources[]`.
- `expected_revision.yaml` — `{verdict: pass|revise, reasons: [...]}`. Reasons
  are only present when `verdict: revise`.

One scenario (`clean_pass/`) has no defect and must resolve `verdict: pass` —
without it, a critic that always says `revise` would pass every other
fixture.

## 3. Ranker fixtures (`ranker/<scenario>.yaml`)

Test `app/sub_agents/pulse/sub_agents/ranker/agent.py`'s deterministic fallback (score-order + cut to
3 — also the step-6 "ranker fails" fallback path, so these fixtures double
as its test suite): `shortlist[]` (each `{item_id, score, score_terms}`),
`expected_ordered_ids[]` (already cut to ≤3, highest score first).
