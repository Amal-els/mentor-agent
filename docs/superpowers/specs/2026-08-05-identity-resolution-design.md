# Identity Resolution (Layer 4) — Design

Status: approved, ready for planning
Date: 2026-08-05 (revised 2026-08-06: multi-tenancy — Postgres, per-user isolation, GCS
blobs; §5, §6, §8, §9 and new §11)
Owner layer: 4 — Identity graph (`AGENT.md` §2, §4)
Feature dependency: F1 (dossier), F2 (1-on-1 agenda), F3 (morning pulse), F5 (ledger) all
consume `person_id` produced here; none of them can attribute correctly without it.

Mentor is a consumer product used by many independent individuals, each connecting their
own Slack/calendar/tracker. **There is no organization layer and no shared data between
users, ever — the user is the tenant.** Every table in this design is owned by exactly one
user, enforced structurally (§11), not by convention. This is stated up front because it
changes the shape of nearly every table in §5: a table missing `owner_user_id` is a defect,
not an oversight to catch in review.

## 1. Problem

Every MCP source names people differently: a Slack handle (`@sarah.dev`), a Jira assignee
ID, a calendar attendee email, a free-text mention in a transcript. Every layer above 4
(salience, composition, ledger) needs one canonical `person_id` per human. Getting this
wrong is worse than being silent — a wrong merge corrupts every downstream count and
attribution silently (SOUL.md: "I do not guess at identity... an unresolved person stays
unresolved").

The two things this design has to hold simultaneously:
- **Precision over recall.** A missed match just means one line item shows a raw handle
  instead of a name. A wrong match means a fact gets attached to the wrong person.
- **No ping fatigue from identity itself.** Confirming an ambiguous match must never be a
  standalone interrupt — SOUL.md's silence-by-default applies here too.

## 2. Governing principle

**Resolution is always eager; only the ask is deferred.**

Every normalized actor reference is scored against the full tier ladder at ingest time —
cheap, deterministic, no LLM. What's deferred is whether the user is ever asked to confirm
an ambiguous match, and that's gated by whether the reference is about to appear in
something the user actually reads. This preserves the "layer 3 always runs before layer 5,
nothing skips a layer" invariant while eliminating the actual source of ping fatigue: being
interrupted to confirm the identity of someone who never mattered.

**The key normalizes, the matchers guess.** `reference_key` construction uses only
lossless, reviewable transforms (case-folding, whitespace trim). Anything that could
conflate two different people (digit-suffix stripping, alias-domain folding, fuzzy name
matching) is a *matcher*, producing a scored, auditable candidate — never baked silently
into the cache key.

## 3. Data flow

Every step below is scoped to one `owner_user_id` — a `RosterSnapshot`, a cache lookup, or
an `UnresolvedReference` for user A is never visible to, or matched against, user B's data.
This is threaded through as an `OwnerScope` (§11.3), not as a parameter each function
remembers to pass — see §11 before reading `resolve()`/`roster.py` in §6.

```
normalized reference (Event/WorkItem/Message actor), scoped to owner_user_id
   │
   ▼
[1] roster.load_snapshot(scope) — one atomic read, filtered to this owner: people,
    alias emails, priors, not_same_as pairs, roster_version
   │
   ▼
[2] cache check — Identity UNIQUE(owner_user_id, source, external_id) /
    UNIQUE(owner_user_id, reference_key)
   │  hit → Resolved (free; re-ingest is a no-op)
   │  miss
   ▼
[3] tiers 0-2 — IdP, primary email, alias email (categorical, not scored)
   │  fires → Resolved(confidence="verified"), write Identity + MergeLog,
   │          one transaction (RosterVersion is NOT bumped here — an Identity
   │          write for an already-known Person isn't matching-relevant, §4.6)
   │  none fire
   ▼
[4] tiers 3-5 — exact name, handle heuristic, fuzzy name — ALL scored and passed
    through the three gates (§4.3), context-priors-adjusted, NotSameAs-filtered,
    ALWAYS RUN NOW (cheap, deterministic, no LLM)
   │
   ▼
    upsert UnresolvedReference{candidates, best_score, margin, occurrence_count++}
    → return Resolved(inferred) / Unconfirmed / Unattributed — never blocks
```

L5 (salience) and L6 (composition) consume the `Resolution` result type directly. Only at
the point L6/L7 decides to put a reference **in front of the user** does an ask happen:

- **card-attached ask** — item ships in a real-time card (pulse, dossier) and the
  candidate clears the ask gate → card renders the raw handle marked unconfirmed
  (`"@sbenali — unconfirmed"`) with a confirm affordance bound to that candidate.
- **Friday-batched ask** — anything never surfaced in a delivered card this week, with a
  candidate above the ask gate, gets swept into one capped batch inside the Friday review
  (F4) — never a standalone DM.
- **below the ask gate / ambiguous** — no name is guessed at all; degrades to the raw
  handle with no attribution.

## 4. Resolution model

### 4.1 Result type

```python
Resolved{person_id, tier, confidence: "verified" | "inferred"}
Unconfirmed{reference_key, top: {person_id, score}, margin}
Unattributed{reference_key, raw_handle}
```

Only `Resolved` writes to `Identity`. `Unconfirmed`/`Unattributed` live solely in
`UnresolvedReference`. L5 aggregation and counts (e.g. "3 open items with Sarah") read
`Identity` only — an unconfirmed guess can mislabel one line, never corrupt a count.
`Unconfirmed` items always render as their own line with the raw handle, never merged into
a resolved person's tally.

**Consumption contract for L5/L6 (binding on downstream layers, not just an implementation
note):** `confidence == "verified"` is the only value that may enter an aggregation or
tally. `confidence == "inferred"` renders as a normal named line (a near-certain
statistical match is good enough to show a name) but is excluded from any count, ranking
input, or salience aggregate — it is displayed, never counted. This is what keeps the "an
unconfirmed guess can mislabel one line, never corrupt a count" promise literally true
instead of aspirational.

### 4.2 `reference_key`

`"{source}:{external_id}"` when the connector provides a structured ID (Slack user ID,
Jira `accountId`), else `"{source}:handle:{normalize(handle)}"` for free-text mentions
with no ID. `normalize()` is the conservative-only transform from §4.4. This key is used
for the cache lookup, for grouping repeat mentions of the same unresolved person, and as
the actor reference stored on normalized `Event`/`WorkItem`/`Message` rows — never a bare
`person_id`, so an unconfirmed guess cannot silently masquerade as fact anywhere
downstream. It carries a `key_version` stamp tied to the pinned `normalize()` version.

`reference_key` is unique **per owner**, not globally — two different users can both
produce `"slack:U123"` (they each have their own Slack workspace with its own ID space)
and these are unrelated rows. Uniqueness is always `(owner_user_id, reference_key)`; see
§5, §11.2.

### 4.3 Three gates

Tiers 0–2 are categorical rules (fire or don't, always `confidence="verified"` — each is
backed by a provider-owned unique ID: IdP subject, primary email, alias email). **Tier 3
(exact name) is not categorical and never auto-links.** Exact display-name equality with no
provider-owned ID is exactly the failure case the golden set punishes: a second person with
the same name joining later makes an earlier auto-link silently wrong and invisible. Tier 3
instead emits a `MatchCandidate` at a fixed score (0.85, from the tier table) and goes
through the same gates as tiers 4–5. Given `ASK_SCORE=0.70 ≤ 0.85 < AUTO_LINK_SCORE=0.95`,
an unambiguous tier-3 match normally lands in `Unconfirmed`, not `Resolved` — the ask is
deferred anyway, so this costs nothing but a rendered "unconfirmed" tag until the reference
is actually in front of the user.

Tiers 3–5 all produce scored candidates that pass through:

| gate | meaning | result |
|---|---|---|
| `score ≥ AUTO_LINK_SCORE (0.95)` and single candidate (defined below) | near-certain | `Resolved(confidence="inferred")` — writes Identity, no ask |
| `score ≥ ASK_SCORE (0.70)` and `margin ≥ MARGIN_MIN (0.15)` | plausible, one clear leader | `Unconfirmed` — card-attached or Friday-batched ask |
| else | ambiguous or weak | `Unattributed` — no name ever shown |

**"Single candidate" is precisely defined**, not left to mean "whatever the roster happens
to contain": no other candidate scores `≥ ASK_SCORE` with `margin ≥ 2 × MARGIN_MIN (0.30)`
against the top one. This gate additionally requires `MIN_ROSTER_SIZE` active people in the
snapshot; below that, auto-link never fires regardless of score — a one-person roster
scoring "single candidate" by vacuous default is a cold-start false-certainty bug, not a
real match.

### 4.4 Normalization vs. matching

`normalize_handle` = Unicode NFKC + casefold + trim. Nothing else. `normalize_email` =
lowercase + RFC `+tag` stripping only (same-mailbox, provably safe). Digit-suffix
stripping, alias-domain folding, and any fuzzy comparison are *matcher* logic (tier 4 for
handle heuristics, tier 2 for the alias-domain table), where they produce a scored,
auditable, rejectable candidate — never a silent key collision.

### 4.5 Negatives

A rejected confirmation persists `NotSameAs(reference_key, person_id)`. Every future
scoring pass filters rejected candidates out before ranking, permanently, independent of
re-scoring. Margin is recomputed *after* filtering, so a rejected top candidate cannot
leave an inflated margin behind for the runner-up.

**Ask count is bounded.** `UnresolvedReference.ask_count` tracks how many times a
`(reference_key, candidate)` pair has ever been surfaced (card or Friday batch), incremented
whenever a `PendingConfirmation` is created for it. `MAX_ASKS_PER_CANDIDATE = 1`: once a
candidate has been asked and gone unanswered (expired) or been rejected, it is never asked
again — an expired card-ask must not fall straight into the next Friday batch and re-ask the
same question. Rejected candidates are already permanently excluded via `NotSameAs`; this
rule additionally covers the "asked, never answered" case, which `NotSameAs` doesn't cover.

### 4.6 Invalidation

`UnresolvedReference` stores `scored_at` + `roster_version`. `RosterVersion` is a counter
**keyed one row per `owner_user_id`** (§5, §11.2 — not a single global row: one user's
roster changes must never bump another user's version and force an unrelated re-score),
bumped **only on changes that are actually matching-relevant** for that owner: a `Person`
insert, a `canonical_name` change, an alias-email add/remove, an `is_active` flip, or a
`NotSameAs` insert. It is deliberately **not** bumped on an `Identity` write for an
already-known `Person` — that's the common case (routine ingest of someone already
resolved) and it invalidates nothing about any other reference's score. Treating every
write as version-relevant would make the counter a global write tally and every
`UnresolvedReference` permanently stale. Anything touched while genuinely stale (re-ingest,
or L6 about to render it) re-runs tiers 3–5 before returning candidates — no triggers, no
background job.

### 4.7 Confirmation and its inverse

One idempotent function, `confirm(reference_key, person_id)`:
writes `Identity(verified_by="user_confirmed", confidence="verified")` + `MergeLog`,
closes the matching `PendingConfirmation` (marks it `confirmed`; the row itself is kept as
the only record that an ask happened — see §5 on why it's not an FK to
`UnresolvedReference`), backfills `resolved_person_id` on every `Event`/`WorkItem`/`Message`
row carrying that `reference_key`, deletes the `UnresolvedReference` row — one transaction,
so historical cards become correct too, not just future ones. `RosterVersion` is **not**
bumped here: confirming a link to an already-existing `Person` doesn't change the roster
other references are scored against (§4.6) — nothing about `confirm()` inserts a `Person`,
changes `canonical_name`/alias emails/`is_active`, or inserts a `NotSameAs` row.

**`unlink(reference_key, reason)` is the mirror image and a first-class operation, not an
afterthought** — without it, `AUTO_LINK_SCORE`'s near-certain auto-link tier is a one-way
door, and `MergeLog.payload` (added for undo) would have no consumer. One transaction:
deletes the `Identity` row, nulls `resolved_person_id` on every `Event`/`WorkItem`/`Message`
row carrying that `reference_key`, writes `MergeLog(action="split", prev_person_id=...,
payload=...)`, inserts `NotSameAs(reference_key, person_id)` so the same link can never
auto-fire again, bumps `RosterVersion`. A reference that gets unlinked re-enters resolution
as `Unattributed`/`Unconfirmed` on next ingest, same as any other unresolved reference —
never silently re-links to the person it was just split from.

### 4.8 Friday batch

Capped at `FRIDAY_BATCH_CAP` (7), ranked by **distinct-day-seen count** (bumped at most
once per calendar day per `reference_key`, using `core/clock.py`), not raw mention count —
raw `occurrence_count` favors a chatty channel mentioning the same unresolved handle twenty
times in one afternoon over someone who came up in three separate meetings across the week,
which is backwards for "who's actually worth asking about." Excludes anything with a live
`pending` `PendingConfirmation` (card or prior batch) or that has already hit
`MAX_ASKS_PER_CANDIDATE`. The rest keep waiting rather than producing an unbounded list.

## 5. Schema (`app/identity/models.py`)

SQLite for a single-user local install only; **Postgres (Cloud SQL) is the deployed
database** (SQLAlchemy 2.x ORM + Alembic, per `AGENT.md` §3 as revised by §11). Every
table below carries `owner_user_id`, non-nullable, FK to `User.id`, **indexed first** since
it prefixes every query this module runs. `jsonb` for JSON columns, `TIMESTAMP WITH TIME
ZONE` for every timestamp — never a naive datetime (§11.1).

**Rosters are per-user.** If two users both know the same real human, that human is two
unrelated `Person` rows, one per user, each resolved from that user's own private sources.
This is correct product semantics, not a limitation to fix later (§11.2) — it's the same
"Person↔Person dedup is out of scope" statement from §9, generalized: there is no
cross-user identity graph, ever.

```python
class User(Base):                    # the tenant — new in this revision
    id: UUID                         # PK
    created_at: datetime
    # everything else (auth, profile) belongs to a different feature; this table
    # exists here only so every domain table below has something to FK to

class Person(Base):
    id: UUID                        # PK
    owner_user_id: FK(User)         # indexed first; every query in this module filters here
    canonical_name: str
    primary_email: str | None       # UNIQUE(owner_user_id, primary_email) WHERE NOT NULL
    is_self: bool                   # the user this Mentor instance serves
    is_active: bool                 # current roster membership (feeds "-active" prior)
    roster_source: str | None       # which system asserts membership
    created_at: datetime

class Identity(Base):
    id: UUID
    owner_user_id: FK(User)
    person_id: FK(Person)           # must belong to the same owner_user_id — enforced in
                                     # OwnerScope (§11.3), not just by the FK
    source: Enum("calendar", "slack", "linear", "jira")   # linear/jira split — sharing
                                                            # an ID namespace would let two
                                                            # providers collide in the
                                                            # UNIQUE cache index
    external_id: str | None
    reference_key: str              # UNIQUE(owner_user_id, reference_key), key_version stamped
    key_version: int
    tier: int                       # 0-5
    confidence: Enum("verified", "inferred")
    verified_by: Enum("auto", "user_confirmed")
    handle: str | None
    email: str | None
    display_name: str | None
    provenance: JSON                # matcher, score, roster_version at match time
    first_seen: datetime
    last_seen: datetime
    # UNIQUE(owner_user_id, source, external_id) WHERE external_id IS NOT NULL — the real
    #   cache index. NOT UNIQUE(source, external_id) — that would collapse two different
    #   users' identically-numbered Slack IDs onto one Person and leak one user's
    #   identity graph into the other's. This is the single most important constraint
    #   in this schema (§11.2).
    # UNIQUE(owner_user_id, reference_key)
    # invariant (enforced by test, not DB constraint): (owner_user_id, reference_key)
    # never also present in UnresolvedReference

class UnresolvedReference(Base):
    id: UUID
    owner_user_id: FK(User)
    reference_key: str              # UNIQUE(owner_user_id, reference_key)
    key_version: int
    source: Enum("calendar", "slack", "linear", "jira")
    external_id: str | None
    handle: str | None
    email: str | None
    display_name: str | None
    candidates: JSON                # ranked [{person_id, score}], post-NotSameAs —
                                     # every person_id here belongs to this owner_user_id
    best_score: float | None
    margin: float | None
    occurrence_count: int           # raw mention count (diagnostic only)
    distinct_day_count: int         # Friday-batch ranking signal (spec §4.8)
    last_seen_date: date            # used to decide whether to bump distinct_day_count
    ask_count: int                  # times any candidate for this key has been surfaced
    scored_at: datetime
    roster_version: int
    status: Enum("pending", "surfaced", "dropped")
    first_seen: datetime
    last_seen: datetime
    # deleted on confirm(); history lives in MergeLog

class NotSameAs(Base):
    id: UUID
    owner_user_id: FK(User)
    reference_key: str
    person_id: FK(Person)
    rejected_at: datetime
    actor: Enum("user")
    # composite index (owner_user_id, reference_key, person_id)

class PendingConfirmation(Base):
    id: UUID
    owner_user_id: FK(User)
    reference_key: str               # plain indexed column, NOT an FK — confirm() deletes
                                      # the UnresolvedReference row in the same transaction
                                      # that closes this confirmation, but this row survives
                                      # as history: it's the only record that an ask happened
    candidate_person_id: FK(Person)
    candidate_score: float
    surface: Enum("card", "friday_batch")
    card_ref: str | None             # opaque pointer to the delivered card (L7-owned)
    status: Enum("pending", "confirmed", "rejected", "expired")
    asked_at: datetime               # actual delivery time, not creation time
    answered_at: datetime | None
    expires_at: datetime
    # UNIQUE(owner_user_id, reference_key, candidate_person_id) WHERE status='pending'

class MergeLog(Base):                # append-only; also the undo path
    id: UUID
    owner_user_id: FK(User)
    person_id: FK(Person)
    reference_key: str
    action: Enum("auto_link", "user_confirm", "user_reject", "split")
    prev_person_id: UUID | None
    payload: JSON                    # candidates snapshot, score, margin — undo needs this
    tier: int | None
    confidence: str | None
    actor: Enum("system", "user")
    reason: str
    created_at: datetime

class PersonRelationship(Base):      # derived; recomputed nightly from normalized events
    owner_user_id: FK(User)
    person_id_a: FK(Person)
    person_id_b: FK(Person)          # a < b, one row per pair, both within owner_user_id
    co_meeting_count: int
    shared_project_count: int
    last_contact_at: datetime
    computed_at: datetime
    # PK/unique (owner_user_id, person_id_a, person_id_b)
    # recompute is idempotent (tested); 24h staleness is acceptable, step [4] wants a
    # plain indexed read, not a live join

class RosterVersion(Base):           # one row PER OWNER, not a single global row —
                                      # the multi-tenancy revision changes this from a
                                      # CHECK(id=1) singleton to owner-keyed
    owner_user_id: FK(User)          # PK
    version: int
    # bumped only on matching-relevant changes (§4.6) for that owner: Person insert,
    # canonical_name change, alias-email add/remove, is_active flip, NotSameAs insert —
    # NOT on every Identity write, or the counter becomes a global tally and nothing
    # stays fresh. Never bumped for one owner by another owner's activity.
```

Normalized `Event`/`WorkItem`/`Message` rows (L3, outside this module) carry
`owner_user_id: UUID` (non-nullable, indexed first), `actor_reference_key: str` (always
set) and `resolved_person_id: UUID | None` (denormalized, set at ingest when resolution is
`Resolved`, backfilled by `confirm()` otherwise).

## 6. Module structure (`app/identity/`)

Dependency direction is one-way; nothing below `resolve.py`/`friday_batch.py` touches the
DB:

```
config.py → normalize.py → roster.py → matchers.py → resolve.py / friday_batch.py → models.py
                                              ↑
                                          types.py (leaf; imported by matchers, resolve, L5, L6)
```

- **`config.py`** — `AUTO_LINK_SCORE`, `ASK_SCORE`, `MARGIN_MIN`, `SINGLE_CANDIDATE_MARGIN
  (2 × MARGIN_MIN)`, `MIN_ROSTER_SIZE`, `CO_MEETING_PRIOR`, `SHARED_PROJECT_PRIOR`,
  `ACTIVE_CANDIDATES_PENALTY`, `FRIDAY_BATCH_CAP`, `MAX_ASKS_PER_CANDIDATE`, `KEY_VERSION`.
  The only place a threshold literal is allowed to live.
- **`normalize.py`** — `normalize_handle`, `normalize_email`, `build_reference_key`. Pure,
  pinned, golden-tested — the whole cache depends on their output staying stable.
- **`roster.py`** — the only DB reader for matching: `load_snapshot(scope: OwnerScope) ->
  RosterSnapshot(people, alias_emails, priors, not_same_as, roster_version)`, one atomic
  read, filtered to `scope.owner_user_id`, so the stamped version always matches the data
  scored against. Never takes a bare `Session` — see §11.3.
- **`matchers.py`** — pure, no DB access, takes a raw reference + `RosterSnapshot`:
  `match_idp`, `match_primary_email`, `match_alias_email` (tiers 0–2, categorical, always
  `confidence="verified"`); `match_exact_name`, `match_handle_heuristic`, `match_fuzzy_name`
  (tiers 3–5, all scored `MatchCandidate`s through the three gates, context-priors applied
  to tiers 4 and 5); `apply_context_priors`. Fixture-testable in total isolation from the
  DB.
- **`types.py`** — `Resolution` (`Resolved`/`Unconfirmed`/`Unattributed`) and
  `MatchCandidate`. A leaf module so `resolve.py`, `matchers.py`, and L5/L6 can all import
  result types without importing the writer module.
- **`resolve.py`** — the only module with side effects on the core tables. Every function
  takes `scope: OwnerScope` as its first argument: `resolve(scope, raw_reference)`,
  `resolve_for_surface(scope, reference_key)` (re-checks staleness, re-derives
  `Unconfirmed`/`Unattributed` fresh each call, hosts `attach_ask(...)` for card-attached
  asks), `confirm(scope, reference_key, person_id)`, `reject(scope, reference_key,
  person_id)`, `unlink(scope, reference_key, reason)` (§4.7 — the mirror image of
  `confirm`, the undo path). `NotSameAs` filtering and margin recomputation happen here,
  after scoring. No function in this module accepts a bare `Session` (§11.3).
- **`friday_batch.py`** — `build_batch(scope, limit)` (read-only, excludes live pending
  card-asks), `commit_batch(scope, batch, card_ref)` (called by L7 only after send
  succeeds; creates `PendingConfirmation` rows and flips `status → surfaced` in one
  transaction).

## 7. Error handling

| failure | behavior |
|---|---|
| Connector down mid-ingest | `resolve()` not called for that source this cycle — L2's problem, not L4's |
| `roster.load_snapshot()` fails | `resolve()` raises; caller retries the batch — never silently returns `Unattributed` for an infrastructure failure |
| A matcher raises on malformed input | caught per-matcher, logged with `reference_key` only, treated as "did not fire" — see the logging rule below |
| `confirm()` called twice for the same key | idempotent — second call finds nothing left to close, no-ops |
| `reject()` empties the candidate list | `UnresolvedReference` stays `pending`, empty candidates; next read computes `Unattributed`; never auto-deleted (a future roster change may produce a new candidate) |
| Two ingests race on a brand-new `reference_key` | `UNIQUE(owner_user_id, reference_key)` — loser retries as an update |
| `commit_batch()` after a partial send failure | L7 passes only the confirmed-delivered subset; undelivered rows stay `pending`, eligible next week |
| Code path constructs a query without going through `OwnerScope` | Structural failure, not a runtime case to handle gracefully — caught by the scope-leak test (§8) and, in Postgres, by row-level security (§11.3) as a second line of defense |

**Logging rule (replaces the ambiguous "never log raw email/handle" line from earlier
drafts, which was self-contradictory since `reference_key` itself can contain a handle):**
`reference_key` built from a structured `external_id` (`slack:U123`) is safe to log as-is —
it's an opaque provider ID, not personal content. `reference_key` built from a handle
(`slack:handle:sbenali`) must have the handle portion hashed before it reaches any log line
— `slack:handle:<sha256(handle)[:8]>`. Raw emails are never logged in any form, under any
key type. `person_id` and `Person.canonical_name` follow the same rule as ledger
content (`AGENT.md` §6.3) — IDs and counts only, never the name itself, in logs.

## 8. Testing

- **`tests/identity/golden.yaml`** drives `matchers.py`/`resolve.py` against fixture
  rosters, no DB. Adversarial by construction: two people sharing a display name, a name
  change (marriage/transliteration), `@ext` contractor handles, a handle that matches a
  different person's name, unicode near-duplicates. Two gates: precision ≥ 0.99 (existing),
  **false-merge count = 0** (new, tracked separately — precision can average a wrong merge
  away, a false merge silently corrupts every downstream count).
- **`normalize.py`** — its own pinned fixture file; a silent change here orphans the cache.
- **`roster.py`** — one test asserting the version and the data it stamps are read
  atomically.
- **`resolve.py`** integration tests (DB-backed): cache-hit-is-a-no-op, tier 0–2 categorical
  auto-link + `MergeLog` write, tier 3 never auto-links even with a single exact-name match,
  gate boundaries (score/margin just above/below each threshold, `MIN_ROSTER_SIZE` guard),
  `NotSameAs` filtering + margin recompute, `confirm()` backfill onto historical rows,
  `unlink()` reversing a `confirm()` and blocking re-link via `NotSameAs`, `reject()` →
  empty-candidate `Unattributed`, `ask_count`/`MAX_ASKS_PER_CANDIDATE` blocking a repeat ask.
- **Invariant test**: no `reference_key` ever appears in both `Identity` and
  `UnresolvedReference` simultaneously — run after every integration test.
- **`friday_batch.py`**: `build_batch` excludes live pending card-asks; `commit_batch` is
  atomic with delivery (simulated partial-send leaves rows `pending`); a successfully
  committed batch is never re-selected.
- **Gate property test**: for any `(score, margin)` pair exactly one of
  `Resolved(inferred)`/`Unconfirmed`/`Unattributed` is reachable, and the boundaries match
  `config.py` exactly (catches a hand-copied threshold constant).

**Multi-tenancy gates (§11) — these are gates, not nice-to-haves, same status as the
precision/false-merge gates above:**
- **Cross-user isolation test.** Seed two users whose sources contain the *same* Slack
  handle, email, and display name. Run full ingest + resolution for both. Assert zero
  shared `Person` rows, zero shared `Identity` rows, and that each user's
  `roster.load_snapshot(scope)` returns only their own rows.
- **Constraint test.** Insert the same `(source, external_id)` for two different owners —
  must succeed (two independent rows). Insert it twice for the same owner — must raise
  `IntegrityError` on `UNIQUE(owner_user_id, source, external_id)`.
- **Scope-leak test.** Grep (or AST-check) `app/identity/*.py` for `session.query(` /
  `select(` on an owned model outside `resolve.py`'s/`roster.py`'s/`friday_batch.py`'s
  `OwnerScope`-mediated helpers — must find none. This is a static gate, not a runtime
  test: it enforces §11.3's structural rule at CI time.

## 9. Scope

In scope: Calendar, Slack, Linear/Jira (matches `AGENT.md` P2 phasing). Tier 0 (IdP/SSO)
and HRIS-backed matching get the matcher interface but no real connector — `match_idp`
returns `None` until an IdP connector exists. Full 6-tier ladder is designed now; tiers 4–5
+ the confirmation round-trip are built in this pass (not deferred), since ping-fatigue
avoidance depends on them.

Out of scope for this pass: the actual Slack card rendering of the confirm affordance
(L7's concern — this design defines the `attach_ask`/`PendingConfirmation` contract L7
consumes, not the Block Kit layout); HRIS/SSO connector implementation (P4 per phasing);
`PersonRelationship`'s nightly recompute job scheduling mechanics (belongs to the trigger
layer, L1); **Person↔Person dedup within one user's roster** — discovering that two
already-`Resolved` `Person` rows for the *same owner* are actually the same human (e.g. a
roster import created a duplicate) is unsupported. `MergeLog.action="split"` moves one
reference between people; it has no shape for merging two `Person` rows into one. This will
be needed once there's a real roster source feeding `Person` creation, and is called out
explicitly here so it isn't quietly assumed to work.

**Cross-user identity resolution is not merely out of scope — it is architecturally
impossible by construction** (§11.2): two users' `Person` rows for the same real human are
permanently two unrelated rows, never merged, never compared, because rosters are per-user
product semantics, not a missing feature. Also explicitly out of scope: authentication/
signup (this design assumes an already-authenticated `owner_user_id` is available to
construct `OwnerScope` — where that comes from is a different feature), per-user schemas or
per-user databases (one shared schema, owner-scoped rows — §11.1), and any
cross-user feature of any kind.

## 11. Multi-tenancy & persistence

Added 2026-08-06. This section is authoritative over any earlier statement in this doc or
in `AGENT.md` that SQLite is the deployed database, that identity caching is
`UNIQUE(source, external_id)`, or that there is one shared roster — those were correct for
a single-user prototype and are now wrong for a deployed product. `AGENT.md` §3 must be
updated to match (plan item).

### 11.1 Database

**Postgres (Cloud SQL) is the deployed database.** Many concurrent users means many
concurrent writers; SQLite's single-writer whole-database lock and its one-file-per-process
model both break under an autoscaled multi-instance deployment. SQLite remains valid **only**
for a single-user local install — swapping is a `DATABASE_URL` connection-string change and
nothing else, so the ORM layer must not silently depend on SQLite-only behavior.

- One `DATABASE_URL` in `app/config.py` (pydantic-settings, per `core/config.py`'s existing
  pattern from Task 1 of the implementation plan). No hardcoded paths.
- Local dev and CI run against Postgres via Docker Compose (`compose.yaml`, one `db`
  service) — **not** against SQLite — so that real `UNIQUE` semantics, `jsonb`,
  timezone-aware timestamps, and `ON CONFLICT` are actually exercised by the test suite
  that gates this module, not simulated by SQLite's looser typing.
- SQLAlchemy 2.x ORM + Alembic migrations only. Raw SQL is allowed only inside a migration
  file, never in application code.
- `jsonb` for every JSON column (`candidates`, `provenance`, `payload`). `TIMESTAMP WITH
  TIME ZONE` for every timestamp column in §5 — a naive datetime anywhere in this schema is
  a defect.
- Connection pooling sized for a serverless runtime: a small pool, `pool_pre_ping=True` (a
  serverless instance can go idle long enough for Postgres to drop the connection
  underneath a stale pool entry).

### 11.2 Per-user isolation is structural, not conventional

Every domain table in §5 carries `owner_user_id`, non-nullable, FK to `User.id`. The
critical constraint change from the single-tenant version of this design:

- `Identity`: `UNIQUE(owner_user_id, source, external_id)` — **not**
  `UNIQUE(source, external_id)`. Without the owner in this key, two users who both happen
  to interact with the same Slack user ID (entirely possible — Slack IDs are workspace-
  scoped, not global) collapse onto a single `Person` row, and one user's resolved identity
  graph becomes readable through the other's. This is the single worst possible bug this
  system could ship; the constraint is the actual defense, not the application code around
  it.
- `UnresolvedReference`, `NotSameAs`, `PendingConfirmation`, `RosterVersion`,
  `PersonRelationship`: all owner-scoped the same way (§5).
- **Rosters are per-user, by design.** Two users who both know the same real human get two
  independent `Person` rows. Merging them would mean inferring a relationship from data the
  other user never shared with this system — the opposite of what a private execution
  co-pilot is for. See §9's restatement of this as the generalized Person↔Person-dedup
  scope line.

### 11.3 `OwnerScope` — the enforcement mechanism

An `OwnerScope` object is constructed once per request/invocation from the authenticated
user (`owner_user_id` + the DB session) and threaded through every function in `roster.py`,
`resolve.py`, and `friday_batch.py` as their first argument (§6). **No function in this
module accepts a bare `Session`.** No module may call `session.query(...)` / `select(...)`
on an owned model directly — every read and write goes through `OwnerScope`'s helpers,
which filter/stamp `owner_user_id` automatically. Enforced at CI time by the scope-leak
test (§8): a grep/AST check with zero tolerance, not a style guideline.

**Defense-in-depth:** Postgres row-level security, `SET LOCAL app.owner_user_id` per
transaction, is enabled as a second, independent enforcement layer — if it doesn't
meaningfully complicate the Alembic migration story. The application-level `OwnerScope` is
the primary control; RLS is belt-and-braces in case a future code path bypasses it.

### 11.4 Blob storage (GCS) — never a database

`app/storage/blobs.py`: a thin interface (`put`, `get`, `signed_url`) with a
local-filesystem implementation for dev and a GCS implementation for deployment — swappable
the same way the L6 composition interface is swappable.

- Object keys are always owner-prefixed: `users/{owner_user_id}/{kind}/{id}`. Never a flat
  namespace.
- What belongs in GCS: raw ingest payloads (pre-L2 Slack/calendar/tracker JSON as fetched,
  before normalization), generated PDFs/cards, periodic database backup dumps.
- What never belongs in GCS: anything requiring a transaction, a uniqueness constraint, or
  a lock — i.e. everything in §5. Explicitly do not mount a database file on GCS or
  gcsfuse; there is no file locking there, and the failure mode is silent corruption, not
  an error.

## 12. Open questions for the implementation plan

- Exact alias-domain table format and where it's maintained (config file vs. DB table) —
  affects tier 2 matcher shape, not the schema above.
- Whether `PendingConfirmation.expires_at` triggers an automatic status flip to `expired`
  via a scheduled sweep, or is checked lazily on next read of that reference — either is
  consistent with this design; pick during planning.
- **`KEY_VERSION` migration.** Bumping it (a `normalize()` change) orphans every existing
  `reference_key` — in `Identity`, `UnresolvedReference`, `NotSameAs`, and the L3
  `actor_reference_key` columns — plus every `NotSameAs` negative silently stops applying to
  the new key form. A `KEY_VERSION` bump must ship with a one-time backfill job that
  re-derives `reference_key` on all four in one pass, not as an afterthought discovered when
  someone actually changes the normalizer. Not built in this pass (there's only ever been
  one version so far); the backfill job's shape is deferred to whenever `KEY_VERSION` first
  needs to move. Since Aug 2026, `reference_key` uniqueness is owner-scoped (§11.2), so a
  future backfill job must re-derive per `(owner_user_id, reference_key)`, never globally.
- **RLS + Alembic interaction.** §11.3 makes Postgres row-level security conditional on it
  "not meaningfully complicating the Alembic migration story." Whether `SET LOCAL
  app.owner_user_id` composes cleanly with Alembic's own migration transactions (which run
  as a different role than the request-scoped session) needs a spike before committing to
  it as more than a stated intent — if it doesn't compose cleanly, `OwnerScope` alone is
  still the required primary control per §11.3.
- **Migration strategy.** Since there is no production data yet, prefer one clean baseline
  Alembic migration for the owner-scoped schema over an additive patch on top of a
  single-tenant baseline that never shipped.
