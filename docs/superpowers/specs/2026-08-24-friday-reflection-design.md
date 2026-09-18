# Friday Reflection / Review (F4) — Design

Status: draft, pending user review
Date: 2026-08-24
Owner feature: F4 (`AGENT.md` §1); triggers L1, composition L6, delivery L7 + a
propose-then-confirm write-back
Feature dependency: reads F2's agenda store (`docs/superpowers/specs/2026-08-14-rolling-agenda-design.md`),
F1's `DossierDelivery`/audio-companion pattern (`docs/superpowers/specs/2026-08-18-pre-meeting-dossier-design.md`),
and the identity design's Friday batch (`docs/superpowers/specs/2026-08-05-identity-resolution-design.md` §4.8,
already implemented at `app/identity/friday_batch.py`). Consumes `OwnerScope`
throughout, same multi-tenant convention every prior design follows.

Built on a branch (`worktree-friday-reflection`) branched from `mentor` at
`c654c06` (the tip that already contains F1/F2/F3's real implementation — every
"reuse X" reference below points at real, importable code). `mentor`, not
`main`, is this repo's actual line of development: `origin/main` predates the
whole `mentor/` app and doesn't contain it at all — `AGENT.md` and
`app/core/models.py` only exist on `mentor` and its descendants, matching F1's
own precedent of branching from and merging back into `mentor`.

## 1. Problem

Friday afternoon, the user should get one Slack DM that turns a week of silent
signal-collection into a short, evidence-first mirror: what shipped, what moved
an OKR, what slipped and why, one concrete adjustment for next week — plus,
from the whiteboard sketch (`docs/whiteboards/Friday_afternoon_reflection_summary.excalidraw`),
a skill-distribution read, which 1-on-1 agenda items resolved vs. are stuck, a
recurring-pattern callout pulled from the week's daily pulses, current OKR
progress, one paragraph of career-narrative advice grounded in a stated target
role, and a "confirm & log" checklist that writes real evidence into the
permanent record only with the user's consent.

Two things have to hold simultaneously, both already stated by
`app/skills/friday-review.md` (the pre-authored skill spec this design
implements as real code):
- **Every win is checkable.** "No citation ⇒ it does not appear" — the same
  provenance discipline F1 enforces on talking points, applied here to wins.
- **It is a mirror, not a performance report.** Neutral voice, no trend claims
  from fewer than 3 weeks of data, and a week off is never reviewed.

A third constraint comes from `AGENT.md` §1's own framing of this whole app —
"it... **writes back**... with the user's consent" — and from the whiteboard's
own "Ledger update proposal" node, distinct from the "Friday reflection" card
node: the review does not just read the ledger, it can *propose* logging
freshly-evidenced-but-not-yet-recorded wins, and only actually writes them once
the user clicks "Confirm & log." This is not in tension with
`friday-review.md`'s "the review reads them, never invents them" rule — that
rule is about not fabricating evidence; every proposed item here is still
backed by a real `WorkItem`/`Commitment` row, just one that was never
explicitly turned into a permanent `Accomplishment` record. Matches
`AGENT.md` §6.4's "write-back requires a consent scope... default is
propose-only."

## 2. What F5 not shipping yet means for this design

`app/skills/friday-review.md`'s frontmatter says `reads: [Commitment, WorkItem,
Goal, Event]` — notably *not* `Accomplishment`, even though its body talks
about "the accomplishment ledger." `AGENT.md` §9 lists F5 (the Accomplishment
Ledger) as still "Not yet shipped," and confirmed live in this branch: no
silent-capture pipeline exists anywhere that writes `Accomplishment` rows from
a `WorkItem` close or a `Commitment` delivery. What *does* already exist,
built as part of F2's rolling agenda:

- `Accomplishment` (`app/agenda/models.py`) — minimal columns (`id`,
  `owner_user_id`, `description`, `source_reference_key`, `occurred_at`),
  owner-scoped "since it's a durable per-user ledger, not agenda state" (its
  own docstring).
- `app.agenda.store.append_ledger_item(pair_scope, owner_scope, kind, item,
  clock)` — writes a `Commitment`/`Accomplishment` row *and* mirrors a pointer
  onto the shared 1-on-1 agenda (`visibility="shared"` by default,
  `AgendaItem.source="accomplishment_ledger"`/`"commitment_ledger"`).

**Correction, stated up front rather than discovered mid-implementation:**
`append_ledger_item` is the wrong reuse target for this design's write-back.
It requires a `PairScope` (report+manager pairing) and — critically — mirrors
every write onto the *shared, manager-visible* agenda by construction. F4's
own privacy rule (`friday-review.md`: "Private, always... never surfaced to a
manager without an explicit grant record") makes that default wrong for a
Friday-review-originated write: this ritual's "confirm & log" must write a
private `Accomplishment` row and nothing else. A new, smaller write path
(§4, "confirm & log") writes directly via `OwnerScope`, no `PairScope`, no
agenda mirror — matching the design's own privacy rule instead of reusing a
function built for a different (meeting-synthesis, explicitly shared) context.

Given F5 isn't built, F4 treats `Commitment`, `WorkItem`, `Goal`, `AgendaItem`,
and the sparse existing `Accomplishment` rows as its evidence base directly —
exactly what `friday-review.md`'s own `reads:` frontmatter already says — and
the "confirm & log" write-back is *how new `Accomplishment` rows come to
exist at all* in this branch, not a consumer of an F5 pipeline that doesn't
exist yet. This mirrors F1's own precedent of reusing `Commitment` directly
for its "promised and not delivered" section rather than waiting on a ledger
feature.

## 3. Data flow

```
Friday afternoon, per-owner local time (triggers/friday_review_scheduler.py,
templated on triggers/cron_scheduler.py's poll loop)
  │
  ▼
[1] weekday==Friday + now_local.time() >= friday_review_fire_time_local (new
    User column, default 16:00 — the same "Fri 16:00" window F1's design
    already names as the deferred-dossier catch-up slot) + not-yet-delivered-
    this-week check (FridayReviewDelivery UNIQUE(owner_user_id, week_start_date,
    trigger='scheduled'))
  │
  ▼
[2] suppression/vacation check — a `Suppression` row (scope=temporal|global,
    target_ref="friday_review") active right now means "on leave" (friday-
    review.md: "A week off / on leave -> Skip the review entirely. Do not
    review a vacation.") -> write a skipped FridayReviewDelivery row
    (sent_at=None, skipped_reason="on_leave") for idempotency, no card sent
  │  not suppressed
  ▼
[3] gather (sub_agents/friday_review/sub_agents/gather) — pure aggregation,
    zero LLM calls, one context object, reusing only existing tables:
      - Accomplishment (occurred_at in the past 7 days) -> this week's wins
      - Commitment (delivered_at in the past 7 days, status=delivered) ->
        also wins, cited
      - Commitment (status=open, due_at < now) -> slipped
      - Commitment (status=open, promised_at <= now-21d) -> "3 weeks
        running" pattern candidate
      - WorkItem (status in closed/done/merged, source in jira/github/linear,
        updated_at in the past 7 days) -> Shipped citations
      - AgendaItem, via app.agenda.scope.resolve_pair_scope(session,
        owner_user_id, owner_user_id) + app.agenda.store.get_agenda(scope) ->
        resolved-this-week vs. still-carried (surfaced_count >= 3 == stuck)
      - Goal (goal_type in objective/key_result, status=active) -> OKR
        progress (current value only — no week-over-week delta; see §10)
      - Goal (goal_type=career_goal, status=active) -> the "toward <role>"
        narrative's target
      - PulseDelivery (ritual="pulse", the past 7 local_dates, this owner) ->
        item_ids cross-referenced across days; an item_id appearing in >=3
        distinct days this week is a "pattern from your daily briefs"
      - Accomplishment (occurred_at in the trailing 8 weeks, skill_category
        is not null) -> skill distribution counts
      - app.identity.friday_batch.build_batch(scope) -> up to 7 "who is
        this" identity-disambiguation asks (spec §4.8), appended as their own
        card section if non-empty
  │
  ▼
[4] synthesize (sub_agents/friday_review/sub_agents/synthesize, LLM,
    app/prompts/friday_review_synthesize.md) — the ONLY LLM call in this
    whole flow (AGENT.md §2 invariant). Given the assembled, already-computed
    context (skill distribution counts, agenda resolved/carried, OKR
    progress, pattern candidates are all pre-computed in [3], never left to
    the model to infer), the model's job is narrow: phrase each win with its
    citation, map wins to the OKR/Goal they moved ("moved the goal"), phrase
    exactly one adjustment from the slipped/pattern evidence, write the one-
    paragraph career-narrative advice grounded only in the given skill
    counts + career goal + gaps, and classify skill_category for any
    Accomplishment in the context missing one (both existing rows read in
    [3] and any newly-proposed item from [3]'s WorkItem/Commitment evidence)
  │
  ▼
[5] provenance drop — any win with no source_reference_key/source_link is
    dropped before card assembly (drop_unsourced_wins, mirrors F1's
    drop_unsourced_talking_points exactly) — if this empties wins AND there's
    no OKR/agenda/pattern content either, falls to the "quiet week" edge case
    (friday-review.md: state it plainly, skip the adjustment, still send)
  │
  ▼
[6] build_friday_review_card (app.delivery.cards) -> SlackDeliverer -> Slack
    DM, private always (no channel-ack branch — this ritual has no pull-from-
    a-channel entry point, only DM and /mentor review)
  │
  ▼
[7] deliver_friday_review records FridayReviewDelivery (prompt_version,
    sent_at, card_ref, proposed_ledger_items JSON — the "confirm & log"
    checklist's contents, computed in [3]/[4] from WorkItem/Commitment
    evidence not yet mirrored into Accomplishment), best-effort audio
    companion (mirrors deliver_dossier_audio's "enhancement, never a
    dependency" contract exactly), and — only if delivery actually sent —
    app.identity.friday_batch.commit_batch(scope, batch, card_ref, clock)
    for any identity asks included in [3]
  │
  ▼
[8] "Confirm & log N items" button click (Slack interactive) ->
    confirm_and_log_ledger_items(scope, clock, delivery_id) -> writes a real
    Accomplishment row (OwnerScope only, no PairScope, no agenda mirror —
    see §2's correction) for each item in that delivery's
    proposed_ledger_items, idempotent (a second click after
    ledger_confirmed_at is set no-ops)
```

`/mentor review` (pull path, `sub_agents/friday_review/pull.py`) enters at
step 3 directly, bypassing steps 1–2 (the Friday/time gate decides *when to
push automatically*, never whether the review can be pulled on demand — same
framing F1's spec gives `/mentor prep` for its own push gate) — and, unlike
the scheduled path, does not respect the once-per-week `FridayReviewDelivery`
uniqueness (a manual pull is always answered fresh; §6's data model reflects
this with a `trigger` discriminator, see the correction there).

## 4. Why this architecture

**Own scheduler, own table, own gate-equivalent — the established per-ritual
pattern, not a new cross-ritual abstraction.** `cron_scheduler.py` (pulse),
`agenda_scheduler.py`, and `dossier_scheduler.py` are already three
independent poll loops; `friday_review_scheduler.py` is a fourth, following
the identical shape (own whitelist env var, own 60s poll, own per-user local-
time check). `FridayReviewDelivery` is a fourth per-ritual delivery table,
same reasoning F1's design gave for not generalizing `PulseDelivery`: "matches
the existing per-ritual pattern, zero risk to F1/F3's working code, small
duplication that can be generalized later if a fourth ritual needs the
identical shape" — this is that fourth ritual, and the duplication is still
small enough that generalizing now would be premature.

**No salience gate.** Unlike F1's dossier (whose whole design is "silence is
the common case"), a weekly reflection is not a push/drop scoring problem —
it is a scheduled ritual like the morning pulse, gated only by "is it Friday
at the right time" and "is the user on leave." No `salience/friday_review_*`
module exists; the suppression check in step 2 is a direct `Suppression`
query, the same primitive `dossier_gate.py` builds on, used at its simplest
(no score, no percentile, no budget — just "is there an active silence for
this ritual").

**`gather` does real aggregation work, not a thin ingestion sweep.** F1's
`gather` mostly calls connectors and identity resolution. F4's `gather` is
where most of the actual logic lives, because almost everything it needs is
already a row in an existing owner-scoped table — no new connector, no new L2
ingestion. This keeps the L6 invariant intact (`gather` is the only place
this flow touches storage; `synthesize` receives one assembled context) while
being honest that F4's "L2/L3" step is mostly a set of `OwnerScope`-scoped SQL
queries against tables F2/F3/F1 already populate, not a connector sweep.

**Skill distribution and OKR-progress are computed in `gather`, not asked of
the LLM.** Counting `Accomplishment.skill_category` rows and reading
`Goal.progress` are exact, auditable operations — asking an LLM to eyeball a
skill breakdown from raw evidence would reintroduce exactly the kind of
ungrounded claim the provenance rule exists to prevent. The LLM's only
numeric-adjacent job is classifying a *new* accomplishment's skill_category
(a genuine judgment call with no ground truth to compute), never counting or
aggregating.

**`skill_category` is a new nullable column on `Accomplishment`, classified
lazily.** No categorization pipeline exists yet (F5 isn't built). Rather than
require a backfill migration or block on a full taxonomy service, `synthesize`
classifies any `Accomplishment` row it reads that still has `skill_category IS
NULL` (both pre-existing rows and this week's newly-proposed ones), and
`deliver` persists those classifications back onto the rows. This means the
trend gets more precise each week as more rows accumulate a label, starting
from zero — an explicit, bounded imprecision (documented in §10, not hidden),
consistent with `friday-review.md`'s own "No trend claims from fewer than 3
weeks of data" guard already protecting against over-reading a sparse start.

**"Confirm & log" is a new write-back, not a reuse of `append_ledger_item`.**
Already covered in §2 — the reuse would have been wrong on privacy grounds
(agenda mirror = manager-visible by default), so this is new code following
`append_ledger_item`'s *pattern* (write the durable row, stamp provenance)
without its *scope* (no `PairScope`, no mirror).

**Audio companion, following F1's `deliver_dossier_audio` precedent exactly.**
The whiteboard's fuller mockup and F1's own "vibrant audio companion" addition
suggest this is now an expected L7 shape for a DM ritual — `deliver_friday_review_audio`
mirrors `deliver_dossier_audio`/`deliver_pulse_audio` structurally: LLM script
draft with a deterministic template fallback, TTS, threaded upload, never
raises, never blocks the text card that already sent.

## 5. New components

All under `mentor/app/`, following existing module conventions exactly:

- **`triggers/friday_review_scheduler.py`** — polling loop mirroring
  `cron_scheduler.py`: `FRIDAY_REVIEW_SCHEDULED_OWNER_IDS` whitelist env var
  (refuses to start without it, same reasoning as pulse's/dossier's
  whitelist), `_should_fire(now_local, fire_time) -> bool` (weekday==4 AND
  time>=fire_time), `run_friday_review_for_user(session, owner_user_id,
  clock)`, `main()`.
- **`sub_agents/friday_review/`** — mirrors `sub_agents/dossier/`'s real
  (not draft-plan) shape exactly: a plain function
  `run_friday_review_flow(owner_scope, clock, llm_agent=None, deliverer=None)
  -> FridayReviewDelivery | None` (gather -> suppression check -> synthesize
  -> deliver as one flow, `llm_agent` built internally via
  `build_synthesize_agent()` when omitted — same "None default would crash
  the real flow" reasoning F1's `run_dossier_flow` corrected its own draft
  plan on), plus `FridayReviewOrchestrator(BaseAgent)` as the thin ADK-invoked
  wrapper.
  - `sub_agents/gather/agent.py` — `gather_friday_review_context` (§3 step 3).
  - `sub_agents/synthesize/agent.py` — `build_synthesize_agent()` (loads
    `app/prompts/friday_review_synthesize.md`, `output_schema=SynthesizeOutput`,
    `output_key=OUTPUT_KEY`, same contract every other LLM sub-agent in this
    codebase follows), `synthesize_friday_review`, `drop_unsourced_wins`.
  - `sub_agents/deliver/agent.py` — `deliver_friday_review` (§3 steps 6-7),
    `confirm_and_log_ledger_items` (§3 step 8).
  - `pull.py` — `pull_friday_review(owner_scope, clock, llm_agent=None,
    deliverer=None)`, the `/mentor review` entry point (bypasses steps 1-2).
- **`app/prompts/friday_review_synthesize.md`** — versioned prompt
  (`friday_review_synthesize@v1`), version ID recorded on every
  `FridayReviewDelivery` row (`AGENT.md` §3's versioned-prompt rule).
- **`app/delivery/cards.py` addition** — `build_friday_review_card(card,
  week_label) -> list[dict]`, new action id
  `FRIDAY_REVIEW_CONFIRM_LOG_ACTION_ID` alongside the existing pulse action
  ids.
- **`app/delivery/tts.py` addition** — `deliver_friday_review_audio`, mirrors
  `deliver_dossier_audio` exactly (own narrator agent + prompt
  `friday_review_audio_script.v1`, own deterministic fallback script
  builder).
- **`app/triggers/slack_command.py` addition** — `run_review_command`,
  mirroring `run_prep_command`'s shape (this file's own docstring already
  lists "review" as one of the not-yet-wired `/mentor` subcommands).
- **`app/triggers/slack_router.py` / `slack_socket_listener.py`** — wire the
  `"review"` slash-command text branch (mirrors the existing `"prep"` branch
  in both the dormant HTTP route and the live Socket Mode path) and the new
  `FRIDAY_REVIEW_CONFIRM_LOG_ACTION_ID` interactive action (mirrors the
  existing feedback-button branch in `_handle_interactive`).

## 6. Data model

Two schema changes; everything else reuses existing owner-scoped models
unchanged.

```python
class FridayReviewDelivery(Base):   # mirrors DossierDelivery's shape
    id: UUID
    owner_user_id: FK(User)         # indexed first
    week_start_date: date           # Monday of the reviewed week (see §10's
                                     # naive-week-math caveat)
    trigger: Enum("scheduled", "pull")  # correction over an earlier draft of
                                     # this design that had UNIQUE(owner_user_id,
                                     # week_start_date) with no discriminator —
                                     # that would make a same-week /mentor
                                     # review either silently no-op after the
                                     # Friday auto-send, or collide with it.
                                     # UNIQUE(owner_user_id, week_start_date,
                                     # trigger) instead: at most one
                                     # *scheduled* delivery per week (the
                                     # idempotency the poll loop needs), pulls
                                     # always fresh (mirrors PulseDelivery's
                                     # own (owner, ritual, local_date,
                                     # trigger) uniqueness shape).
    sent_at: datetime | None        # null if skipped (on_leave)
    skipped_reason: str | None      # "on_leave" | None
    prompt_version: str
    card_ref: str | None
    proposed_ledger_items: JSONB    # [{description, source_reference_key,
                                     #   confidence: "auto"|"needs_confirm"}]
    ledger_confirmed_at: datetime | None
    created_at: datetime
    # UNIQUE(owner_user_id, week_start_date, trigger)
```

```python
# app/agenda/models.py — Accomplishment gets one new nullable column
class Accomplishment(Base):
    ...
    skill_category: Mapped[str | None]  # "technical_execution" |
                                         # "cross_team_collab" | "mentorship" |
                                         # "leadership_docs" | None
```

Reused as-is, no schema change: `Commitment`, `WorkItem`, `Goal`, `AgendaItem`
(via `resolve_pair_scope`/`get_agenda`), `PulseDelivery` (read-only, for
pattern detection), `Suppression` (the on-leave check), and
`app.identity.friday_batch`'s existing tables (`UnresolvedReference`,
`PendingConfirmation`) via its own `build_batch`/`commit_batch`.

**Decision:** `FridayReviewDelivery` is its own table, not a fourth `ritual`
value folded into `PulseDelivery`, matching F1's identical decision for
`DossierDelivery` and for the same stated reason (§4).

## 7. Edge cases

From `app/skills/friday-review.md`'s own table, mapped to the components
above:

| Case | Handling |
|---|---|
| Quiet week, little shipped | `synthesize` still runs; card says "quiet week," shows whatever real evidence exists, omits the "one adjustment" section entirely (not an empty placeholder) |
| Same item slipped 3 weeks running | `gather`'s `promised_at <= now-21d` filter surfaces it; `synthesize` names the pattern once, plainly, in the "one adjustment" phrasing — never a second, separate nag |
| A week off / on leave | `Suppression(target_ref="friday_review")` active -> skip entirely, step 2, no card, no LLM call |
| Ledger empty (new user) | `gather` returns empty wins/patterns/skill_distribution; `synthesize` explains what the ledger will collect (per the skill's own edge case) rather than presenting an empty section as a failure |
| User disputes a listed win | Out of scope for this pass — `friday-review.md`'s "remove it and record the correction" implies an edit/dispute affordance on `Accomplishment`, which doesn't exist yet even for F5's own silent-capture writes; noted in §11 |
| Goals not connected | `gather`'s `Goal` query returns empty; card ships Wins + Slipped, OKR/career-narrative sections say the mapping is unavailable rather than omitting silently |
| Agenda item stuck (surfaced_count>=3) | Rendered in the "Agenda 1-on-1s" section with the same "worth raising directly" framing the whiteboard mockup shows, not auto-escalated anywhere |
| Skill distribution has zero categorized rows yet | Section renders "not enough categorized history yet" rather than an all-zero bar chart that reads as "you did nothing" |
| Identity Friday batch is empty | The identity-asks section is omitted from the card entirely (not rendered as an empty section) |
| Confirm & log clicked twice | `confirm_and_log_ledger_items` checks `ledger_confirmed_at is not None` first and no-ops on a second click — no duplicate `Accomplishment` rows |
| `/mentor review` pulled on a non-Friday | Runs anyway (pull bypasses the day/time gate, §3) — the week window is still "the past 7 days from now," not "the most recently completed Mon-Fri" |

## 8. Error handling

| failure | behavior |
|---|---|
| A `gather` query against one table fails (e.g. `AgendaItem` lookup errors because no `Pair` exists for this owner) | Caught per-source in `gather`, that section renders as unavailable in the context (mirrors F1's "gather proceeds with what succeeded" contract for connector failures) — never blocks the whole review on one missing table |
| LLM synthesis call fails | `deliver_friday_review` is not invoked; no `FridayReviewDelivery` row is written at all (unlike F1, where a `sent_at=null` row still gets written by `deliver`) — the next poll cycle, still past fire time, retries the whole flow fresh, since nothing was queued for delivery yet |
| Slack send fails mid-flight | `FridayReviewDelivery` row written with `sent_at=null`, `skipped_reason=None` — distinguishable from an on-leave skip; not retried automatically this cycle (per-week uniqueness would need an explicit retry path — noted in §11 as a known gap, same posture F1's own open-questions section takes toward its analogous gap) |
| Two poll cycles race on the same owner/week | `UNIQUE(owner_user_id, week_start_date, trigger='scheduled')` — loser's insert fails, no duplicate DM (same mechanism as `DossierDelivery`'s race protection) |
| `friday_batch.commit_batch` raises after a successful card send | Caught, logged, does not roll back or resend the card — the text/audio card already sent is the source of truth; identity asks simply aren't recorded as `surfaced` this cycle and remain eligible for a future batch |
| `confirm_and_log_ledger_items` called for a delivery with no `proposed_ledger_items` | No-op, returns `[]` — not an error (a quiet week can easily have nothing to propose) |
| Audio companion (TTS/upload) fails | Caught inside `deliver_friday_review_audio`, logged, never raised — same "enhancement, never a dependency" contract as F1/F3's audio companions |

## 9. Testing

Per `AGENT.md`'s conventions: real Postgres via the `pg_session` fixture
(`tests/unit/conftest.py`), `FrozenClock` throughout, no live LLM calls —
every LLM-calling path is exercised by monkeypatching
`app.core.adk_runner.run_agent_sync`, the same pattern
`tests/unit/sub_agents/dossier/test_edge_cases.py` established (golden
fixture dicts keyed by `OUTPUT_KEY`, no network).

- `friday_review_scheduler.py`: `_should_fire` boundary tests (Thursday
  16:00 -> False, Friday 15:59 -> False, Friday 16:00 -> True, Friday 23:59 ->
  True), whitelist-required test.
- `gather_friday_review_context`: one test per evidence source (wins from
  `Accomplishment` + delivered `Commitment`, slipped from overdue open
  `Commitment`, the 3-week pattern filter's exact boundary, agenda
  resolved-vs-carried via a seeded `Pair`+`AgendaItem`, OKR progress from
  `Goal`, skill distribution counting only categorized rows within the
  trailing 8 weeks and excluding rows outside it, the daily-brief pattern
  detector's >=3-distinct-days threshold).
- `synthesize_friday_review`/`drop_unsourced_wins`: provenance drop test
  (mirrors F1's `drop_unsourced_talking_points` test), quiet-week and
  ledger-empty edge cases via golden fixtures under
  `tests/fixtures/friday_review/`.
- `build_friday_review_card`: renders every section conditionally (each
  section's "empty" rendering per §7's edge-case table gets its own test,
  same discipline `render_blocks`'s existing day/owed-empty tests already
  follow for pulse).
- `deliver_friday_review`: writes `FridayReviewDelivery` with the right
  `trigger` discriminator, dedupe test (two scheduled deliveries same
  owner/week -> one row, `IntegrityError` on the loser mirrors
  `DossierDelivery`'s existing race test), on-leave skip test (Suppression
  seeded -> `sent_at is None`, `skipped_reason="on_leave"`, no deliverer call
  at all).
- `confirm_and_log_ledger_items`: writes one `Accomplishment` per proposed
  item, second call no-ops (`ledger_confirmed_at` already set), writes go
  through `OwnerScope` only — an explicit assertion that no `AgendaItem` row
  is created as a side effect (the §2 correction, made into a real
  regression test, not just a docstring).
- `pull_friday_review`: runs on a non-Friday `FrozenClock`, still delivers
  (bypasses the day gate), uses `trigger="pull"` so it never collides with
  that week's scheduled delivery's uniqueness.
- Cross-owner isolation: same shape as F1's and the identity design's —
  seed two owners with overlapping `Accomplishment`/`Commitment`/`Goal` data,
  assert zero cross-owner rows in any query `gather` runs.

## 10. Scope

In scope: the full weekly ritual (gather -> synthesize -> deliver) over
existing `Commitment`/`WorkItem`/`Goal`/`AgendaItem`/`Accomplishment`/
`PulseDelivery` data, the skill-distribution column + lazy classification,
the on-leave suppression skip, the identity Friday-batch integration, the
`/mentor review` pull path, the audio companion, and the "confirm & log"
write-back (propose + explicit-consent write of new `Accomplishment` rows).

Out of scope for this pass, stated explicitly rather than silently dropped:
- **The `/ui` dashboard panel** F1 shipped for dossiers. F4 is already the
  larger of the two features in real component count; a `/ui` Friday-review
  panel would follow the identical `build_*_a2ui_payload` +
  `GET /webhooks/friday-review-payload` pattern F1 established, but is
  deferred to keep this pass shippable. Noted as a fast-follow, not designed
  away.
- **Week-over-week OKR deltas** ("60% → 85% this week" in the whiteboard
  mockup). `Goal` stores only current `progress`/`current_value`, no history
  — a real delta needs a weekly snapshot table, which is a genuine new piece
  of schema this pass doesn't introduce. Ships with current-progress-only,
  still real and sourced, just not a delta.
- **Disputing/correcting a listed win** (`friday-review.md`'s "user disputes
  a listed win -> remove it, record the correction" edge case). No dispute
  affordance exists on `Accomplishment` at all yet, even for other write
  paths — this needs its own small design (an `Accomplishment.status`
  column + a "remove" button, or similar), not invented ad hoc here.
- **A real Mon-Fri calendar week.** The evidence window is "the trailing 7
  days from `clock.now()`," not each owner's actual Mon-Fri work week in
  their own timezone — same pragmatic simplification `cron_scheduler.py`'s
  own nightly-consolidation clock already accepts ("not real per-user
  timezone handling... matching this fix's own don't-over-engineer scope").
  Correct for a review that fires Friday afternoon; would drift for a
  same-week `/mentor review` pulled on, say, a Tuesday.
- **Merging `friday_review_scheduler.py` with `cron_scheduler.py`/
  `agenda_scheduler.py`/`dossier_scheduler.py`** now that there are four
  near-identical poll loops. Explicitly deferred, not decided against — same
  posture F1's design took on merging with `agenda_scheduler.py`.

## 11. Open questions for the implementation plan

- Exact `skill_category` taxonomy (the four values above, taken directly from
  the whiteboard mockup) is a closed set enforced in code, not a DB enum —
  confirm this matches `AgendaItem.source`/`.status`'s existing free-`String`-
  with-comment convention rather than introducing this codebase's first real
  DB-level enum.
- Whether a failed Slack send (§8, "not retried automatically this cycle")
  needs a real requeue path or is an acceptable gap for this pass, same
  unresolved status F1's own design left its analogous gap in.
- Cold-start weights/priors don't apply here (no salience score to seed) —
  the closest analogous open question is whether `FRIDAY_REVIEW_SCHEDULED_OWNER_IDS`
  should default-inherit from `PULSE_SCHEDULED_OWNER_IDS` (same owners, in
  practice, for this dev deployment) rather than requiring a second explicit
  env var — leaning toward keeping them independent (matches every other
  ritual's own separate whitelist) but flagged for the plan to decide.
