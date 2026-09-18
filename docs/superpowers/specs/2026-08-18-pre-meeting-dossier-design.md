# Pre-Meeting Dossier (F1) — Design

Status: draft, pending user review
Date: 2026-08-18
Owner feature: F1 (`AGENT.md` §1); triggers L1, composition L6, delivery L7
Feature dependency: consumes `person_id` from L4 identity resolution (see
`docs/superpowers/specs/2026-08-05-identity-resolution-design.md`, authoritative on the
multi-tenant `owner_user_id`/`OwnerScope` shape every table in this design follows) and
reads F2's agenda store for recurring 1-on-1s (`docs/superpowers/specs/2026-08-14-rolling-agenda-design.md`).

Built on a branch (`worktree-pre-meeting-dossier`) rebased onto `main`'s tip, which already
contains F2/F3's implementation — so every "reuse X" reference below points at real,
importable code, not a parallel worktree. The dossier trigger is still its own independent
scheduler process alongside `agenda_scheduler.py`; see §3 for why.

## 1. Problem

15 minutes before a qualifying meeting, the user should get one Slack DM that earns the
interrupt: who they're meeting and the last real interaction with each, why this meeting
matters now, three ranked talking points, and anything promised-and-not-delivered in either
direction. A dossier that only restates the calendar invite is noise
(`app/skills/meeting-prep.md`). This design wires that skill spec to real code.

Two things have to hold simultaneously:
- **Every claim is sourced.** No source link, no talking point (provenance rule).
- **Silence is a valid outcome.** Most meetings (recurring standups, low-signal 1:1s) should
  never trigger a card at all — the salience gate exists to make "nothing sent" the common
  case, not a fallback.

## 2. Data flow

```
Calendar (poll, 60s, dossier_scheduler.py — separate process from agenda_scheduler.py)
  │
  ▼
[1] T-15-before-event window check + dedup deque (same shape as agenda's "just ended" check,
    inverted: event.start - now <= 15min, event.start - now > 0)
  │
  ▼
[2] qualification filter — event type (1-on-1 / external attendee / 3+ stakeholders),
    replaces the old hardcoded rule
  │  no  → no dossier scheduled; still available on-demand via /mentor prep
  │  yes
  ▼
[3] salience gate (app.salience.gate.apply_gate + a new dossier score function in
    app.salience.score) — score = w1*attendee_rarity + w2*is_external + w3*unresolved_threads
    + w4*deadline_proximity + w5*is_one_on_one + w6*prep_absent - w7*recurrence_familiarity
  │  not top 20% of this owner's history → on-demand only (/mentor prep still works, full
  │                                        pipeline below runs synchronously for a pull)
  │  top 20%, no push budget left today  → queue for next window (9:30 / T-15 / Fri 16:00)
  │  top 20%, budget available           → proceed
  ▼
[4] ingestion sweep (parallel, all pre-existing connectors, no new ones):
    LiveCalendarClient (attendees, already fetched for the trigger),
    LiveSlackClient (DMs, channel, open questions),
    LiveLinearClient / LiveJiraClient (open work items for attendees),
    LiveNotionPairClient (org/manager context — replaces HRIS),
    LiveFathomClient (prior meeting transcript, if any, for "why now")
  │
  ▼
[5] per attendee: app.identity.resolve.resolve(scope, raw_reference) — unresolved stays
    unresolved, rendered as the raw handle per identity design's Resolution contract
  │
  ▼
[6] recurring 1-on-1? (identity-resolved pair, event marked recurring)
    yes → app.agenda.scope.resolve_pair_scope + app.agenda.store.get_agenda(scope) —
          reuse existing open talking points, skip fresh talking-point synthesis
    no  → talking points synthesized fresh in step 7
  │
  ▼
[7] sub_agents/dossier (SequentialAgent: gather → synthesize → deliver)
    synthesize produces: Who, Why now, Talking points (3, ranked — or agenda's if step 6
    matched), Promised-and-not-delivered (from Commitment), suggested opener
    (external/high-stakes only)
  │
  ▼
[8] drop any talking point with no source link (provenance rule) — if this empties talking
    points AND there's no agenda carryover, fall to the "nothing to prep" edge case
  │
  ▼
[9] build_dossier_card (app.delivery.cards) → SlackDeliverer → Slack DM
    (channel-triggered pull still DMs privately + acknowledges in-channel, per
    meeting-prep.md's "private by default" rule)
  │
  ▼
[10] FeedbackEvent capture (👍/👎/mute/opened) on card interaction → nightly
     weight_consolidation job folds signal into this owner's dossier Weight rows
```

`/mentor prep` (pull path) enters at step 4 directly, bypassing steps 1–3 (the gate decides
*whether to push*, never whether the dossier can be pulled) and must still be complete.

## 3. Why a separate scheduler process

`agenda_scheduler.py` already polls Calendar every 60s for meeting-*end* events. A T-15-
*before* trigger could share that loop. Decision: **separate `dossier_scheduler.py`**,
templated on `agenda_scheduler.py`'s shape (own whitelist env var
`DOSSIER_SCHEDULED_REPORT_USER_IDS`, own 60s poll, own dedup deque).

Reasoning: fault isolation (a bug in one ritual's trigger can't stall or crash the other's
poll loop) and consistency with the existing one-process-per-ritual pattern
(`agenda_scheduler.py`, `pulse_trigger.py`, `cron_scheduler.py` are already separate). The
cost — a second redundant Calendar poll per cycle — is accepted; the codebase already pays
this cost per-ritual elsewhere.

## 4. New components

All under `mentor/app/`, following existing module conventions exactly (no restructuring):

- **`triggers/dossier_scheduler.py`** — polling loop. `poll_dossier_window_once()`: fetch
  Calendar via `LiveCalendarClient.fetch(window, report_user_id)`, filter by
  `_is_t_minus_15(event, now)`, dedupe via a process-lifetime `deque`, emit a `TriggerEvent`
  per qualifying event. Refuses to start without `DOSSIER_SCHEDULED_REPORT_USER_IDS` set,
  same as agenda's whitelist guard.
- **`salience/dossier_score.py`** (new module, not an addition to `score.py`) —
  `score_dossier_candidate(event, context) -> float` computing the 7-term formula above.
  **Correction from an earlier draft of this spec:** `salience/gate.py`'s `apply_gate` and
  `salience/score.py`'s scoring functions are pulse-specific (operate on pulse's
  `PreGateContext`/`ScoredItem` types, pulse-only config constants) — not a generic,
  importable gate. Dossier gets its own small gate module (`salience/dossier_gate.py`)
  following the *same pattern* (Suppression matching via `scope`/`target_ref`/`reason`, a
  budget cap) as new code, not an import. This matches the codebase's established
  one-module-per-ritual duplication (already true of the scheduler, per §3) rather than
  introducing a first cross-ritual shared abstraction here. `Weight` rows use a plain
  `key` string column (already free-form, e.g. `"dossier:attendee_rarity"`) — no schema
  change needed for weight storage.
- **`sub_agents/dossier/`** — **correction:** `sub_agents/agenda` is not a declarative
  auto-running tree; it's a plain function (`run_post_meeting_flow`, in
  `sub_agents/agenda/agent.py`) that builds child `LlmAgent`s via factory functions
  (some cloned from module-level singletons via `.clone(...)`, required because ADK's
  `SequentialAgent` stamps a parent onto each sub-agent and reuse without cloning raises
  `ValidationError`), assembles them into one `SequentialAgent`, and runs it via
  `run_agent_sync(...)` — with a separate thin `BaseAgent` subclass
  (`RollingAgendaOrchestrator`) as the actual ADK-invoked entry point that just calls the
  function. Dossier follows this exact shape: `sub_agents/dossier/agent.py` defines
  `run_dossier_flow(event, owner_scope, clock) -> dict` (gather → synthesize → deliver as
  one `SequentialAgent`, children built by factory functions under
  `sub_agents/dossier/sub_agents/{gather,synthesize,deliver}/agent.py`), plus a
  `DossierOrchestrator(BaseAgent)` wrapper mirroring `RollingAgendaOrchestrator`.
  - `gather`: runs the ingestion sweep (step 4) + identity resolution (step 5) + the
    recurring-1-on-1 agenda-store check (step 6). Produces one assembled context object —
    L6 never queries a connector directly (`AGENT.md` §2 invariant).
  - `synthesize`: LLM composition (versioned prompt, `app/prompts/dossier_synthesize.md`),
    the 5-section card body, provenance drop (step 8), the edge-case table in §6.
  - `deliver`: `build_dossier_card`, `SlackDeliverer.deliver`, records `DossierDelivery`.
- **`delivery/cards.py`** — add `build_dossier_card(...)`. **Correction:** `build_card` is
  a `PulseCard`-specific function tied to pulse's `trigger_result`/`context` shape, not a
  generic builder — `build_dossier_card` is new code following the same
  dataclass → `render_blocks` *pattern*, not a literal call into `build_card`.
- **Cross-feature integration task:** `salience/types.py`'s `DayEventSummary` already has a
  `has_dossier: bool = False` stub field with a docstring noting F1 doesn't exist yet.
  `salience/assemble.py` constructs `DayEventSummary` rows without ever passing
  `has_dossier=True`. Once `DossierDelivery` exists, `assemble.py` must query which
  `events_in_window` have a `DossierDelivery` row and set `has_dossier` accordingly, so the
  morning pulse (F3) can flag "dossier waiting" per `app/skills/morning-pulse.md`. Small,
  but easy to silently miss since it lives in pulse's code, not dossier's.
- **`app/prompts/dossier_synthesize.md`** — versioned prompt, version ID recorded on every
  `DossierDelivery` row (`AGENT.md` §3 rule: "prompts are versioned files with an ID recorded
  on every generated card").
- **`app/intents/prep-meeting.md`**, **`app/skills/meeting-prep.md`** — already written;
  no change needed, this design implements them as-is.

## 5. Data model

One new table; everything else reuses existing owner-scoped models unchanged.

```python
class DossierDelivery(Base):        # mirrors PulseDelivery's shape exactly — see §7
    id: UUID
    owner_user_id: FK(User)         # indexed first, per identity design's §11.2 convention
    event_external_id: str          # calendar event this dossier was for
    sent_at: datetime | None        # null if queued/suppressed, not yet sent
    prompt_version: str
    talking_points_source: Enum("fresh", "agenda_carryover")  # step 6's branch, for audit
    card_ref: str | None            # opaque pointer to the delivered card, same convention
                                     # as identity design's PendingConfirmation.card_ref
    feedback: Enum("none", "up", "down", "muted", "opened")
    feedback_at: datetime | None
    created_at: datetime
    # UNIQUE(owner_user_id, event_external_id) — one dossier per event per owner
```

Reused as-is, no schema change: `Commitment` (promised-and-not-delivered section),
`Suppression` (gate precedence, structural floors), `Weight` (free-form `key` string column,
e.g. `"dossier:attendee_rarity"` rows — no schema change), `AgendaItem`/`Pair` via
`app.agenda.store` (recurring 1-on-1 carryover), and identity module tables consumed via
`app.identity.resolve.resolve` (`Person`, `Identity`, `UnresolvedReference`, ...).
**Correction:** `FeedbackEvent` is pulse-specific (FK'd to `PulseDelivery`) — dossier feedback
(👍/👎/mute/opened) lives directly on the `feedback`/`feedback_at` columns on
`DossierDelivery` below instead, since one dossier is one card, not a set of scored items
needing per-item feedback rows the way pulse does.

**Decision:** `DossierDelivery` is its own table rather than generalizing `PulseDelivery`
into a shared `Delivery` table now. Matches the existing per-ritual pattern, zero risk to
F3/pulse's working code, small duplication that can be generalized later if a third ritual
needs the identical shape.

## 6. Edge cases

From `app/skills/meeting-prep.md`, mapped to the components above:

| Case | Handling |
|---|---|
| 12 attendees | `gather` profiles organizer + top-3 by `PersonRelationship.co_meeting_count`, others counted only |
| External attendee, no internal history | `identity.resolve` returns `Unattributed`/`Unconfirmed` → raw handle shown, `synthesize` preps from agenda + public context only, labels the gap explicitly |
| Recurring standup | `recurrence_familiarity` term suppresses by default (step 3); if pulled via `/mentor prep`, `synthesize` returns one line of deltas, not a full dossier |
| Meeting in 4 minutes | Late trigger still fires; `synthesize` ships Who + Why-now only, card labeled "short version" |
| No agenda, no history | `synthesize` returns "nothing to prep" + three suggested questions; still passes the provenance check since it asserts nothing |
| Attendee is the user's manager | Structural floor on `Weight`/`Suppression` (never decays out, never suppressed) — same mechanism the identity/salience layer already uses for structural floors, applied to a manager-tagged `Person` |
| All talking points fail provenance | Falls through to the "no agenda, no history" case, step 8 |
| Push budget exhausted for the day | Salience gate queues for the next window (9:30 / T-15 / Fri 16:00) rather than dropping — the dossier still ships, just delayed, never silently lost |
| Channel-triggered pull | DM the dossier, acknowledge publicly in-channel per `meeting-prep.md`'s privacy rule |

## 7. Error handling

| failure | behavior |
|---|---|
| A connector in the ingestion sweep is down | `gather` proceeds with what succeeded; `synthesize` notes the gap in "Why now" if it's load-bearing, never blocks the whole dossier on one source (L2's problem propagates as a partial context object, not a pipeline failure) |
| `identity.resolve` unresolved for an attendee | Not an error — render raw handle, per the identity design's contract |
| `dossier_gate` raises | Trigger event is not delivered this cycle; scheduler logs and retries next poll, same as agenda's existing failure mode |
| LLM synthesis call fails | `deliver` is not invoked; `DossierDelivery.sent_at` stays null; scheduler's dedup deque still marks the event as attempted so it isn't retried every 60s indefinitely — retried once on the next full window check only |
| Slack send fails mid-flight | `DossierDelivery` row written with `sent_at = null`; feedback capture never fires; treated as "not delivered," visible for ops via the table, not silently dropped |
| Two poll cycles race on the same event | `UNIQUE(owner_user_id, event_external_id)` on `DossierDelivery` — loser's insert fails, no duplicate DM |

## 8. Testing

Per `AGENT.md`'s connector-testing rule: reuse `tests/fixtures/{source}/` fixtures already
built for F2 — no new fixture format needed.

- `dossier_scheduler.py` window math: frozen clock (`core/clock.py`), boundary tests at
  exactly T-15, T-14:59, T-15:01, and the "meeting in 4 minutes" late-trigger case.
- `score_dossier_candidate`: unit tests per weight term, plus the manager structural-floor
  test (never drops below the push threshold regardless of other terms).
- `sub_agents/dossier` golden outputs: "short version," "nothing to prep," "external
  attendee no history," "recurring standup deltas-only," "agenda carryover" (talking points
  sourced from `AgendaItem` instead of fresh synthesis) — recorded fixtures, not live LLM
  calls, for CI stability.
- Provenance test: any talking point missing a source link is dropped before card assembly,
  verified as a unit test on `synthesize`'s output, not just an integration smoke test.
- `DossierDelivery` uniqueness/race test: two concurrent scheduler ticks for the same event
  produce exactly one row.
- Cross-owner isolation: same shape as the identity design's cross-user isolation test —
  seed two owners with the same event/attendee data, assert zero shared `DossierDelivery`,
  `Weight`, or `Suppression` rows.

## 9. UI delivery surface

Dossiers also land in the existing demo dashboard (`/ui`), not just Slack, using the same
pull-based A2UI pattern the agenda panel already established
(`app/agenda/payload.py:build_a2ui_payload`, `GET /webhooks/agenda-payload` in
`app/triggers/agenda_router.py`):

- `app/agenda/payload.py`-sibling: `build_dossier_a2ui_payload(deliveries: list[dict]) ->
  dict` — deterministic, pure, no LLM call, fixture-testable, mirroring the existing
  function's discipline exactly.
- `GET /webhooks/dossier-payload` (new route, same router file or a sibling
  `dossier_router.py`) — same two-layer auth as every other webhook route
  (`_token_is_valid` against the shared token, `_acting_user_secret_is_valid` against the
  acting user's `agenda_client_secret`). Recomputes current `DossierDelivery` rows for the
  given `report_user_id`/`acting_user_id` fresh from the DB on every call — pull-based, not
  a relay of a stale push payload, same reasoning as the agenda route (finding 1 in
  `agenda_router.py`'s module docstring).
- `app/static/index.html` / `app.js`: add a "Dossiers" panel polling `/webhooks/dossier-
  payload` on the same interval the agenda panel already uses, rendered from the same
  component vocabulary (`editable_text`-style read-only cards; no `visibility_toggle` or
  `consent_card` components apply here since dossiers aren't editable or consent-gated).

**Flag rename:** `fast_api_app.py`'s dashboard-mount flag is currently `AGENDA_UI_ENABLED`,
named for the only feature it gated when it was written. Since it now gates a page with an
agenda panel *and* a dossier panel (and likely more later), rename it to
`MENTOR_UI_ENABLED` in `fast_api_app.py` and any deploy config referencing the old name.
This is a small, mechanical, low-risk rename (one flag, one call site) bundled into this
feature rather than deferred, since shipping a second panel under a feature-specific flag
name would be actively misleading.

## 10. Scope

In scope: Calendar + Slack + Linear/Jira + Notion (org data, replacing HRIS) + Fathom
(transcript context) ingestion, salience gate with the 7-term score and learned per-owner
weights, feedback capture and nightly consolidation, recurring-1-on-1 agenda carryover,
`/mentor prep` pull path, Slack DM delivery, and the `/ui` dashboard's Dossiers panel (§9).

Out of scope for this pass: write-back to any source system (P1/P3 boundary — dossier is
read + notify only); the visual/interactive confirm affordance for unresolved attendees
(owned by L7/identity design, this design only consumes `Resolution`, doesn't render the ask);
a generalized cross-ritual `Delivery` table (§5 decision); building any new MCP connector
(all sources already exist per the rolling-agenda worktree survey).

## 11. Open questions for the implementation plan

- Exact weight defaults/priors for the 7-term dossier score before any learned signal exists
  (cold start) — pick during planning, likely mirrors identity design's prior-seeding
  approach.
- Whether `dossier_scheduler.py` and `agenda_scheduler.py` should eventually be merged once
  both are stable, now that the redundant-polling cost is measured rather than assumed —
  explicitly deferred, not decided against.
- ~~Confirm `Weight` table's ritual column~~ — resolved: `Weight.key` is a free `String`
  column, `"dossier:<term>"` keys need no migration.
