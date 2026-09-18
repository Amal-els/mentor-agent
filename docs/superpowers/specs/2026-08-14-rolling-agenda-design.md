# Rolling 1-on-1 Agenda (Feature F2) — Design

## 1. Problem

A 1-on-1 agenda is a rolling object, not a fresh generation per meeting. Its
value is continuity: something raised once and left open should reliably
resurface, not silently vanish, and never be regenerated from scratch. Items
arrive continuously from four independent sources — Jira blockers, unanswered
Slack questions, commitment/accomplishment ledger events, and manual notes
from either party — and converge on one shared store that both the manager
and their report read and write. After each 1-on-1, the meeting's outcome
(decisions, commitments, focus points) is synthesized back into that same
store, and an editable summary is delivered for both people to adjust before
it's final.

This spec covers the persistence layer, the write-path routing logic, the
post-meeting synthesis flow, and the delivery/consent mechanics. It does not
cover the visual design of the A2UI cards themselves (component catalog,
styling) or the Accomplishment Ledger's own query/reporting surface (F5) —
this feature only needs a minimal accomplishment-ledger write target, not the
ledger's read-side reporting, which is out of scope here.

## 2. Governing principle

**One document, appended to and closed, never regenerated.** Every mutation
is an event with a `history` trail. Nothing is silently dropped: an item
either stays open, gets explicitly resolved, or — once it's been surfaced
repeatedly without action — is escalated to a human consent decision rather
than assumed stale. The agenda store is the single source of truth for what
is "on the agenda"; durable ledgers (commitments, accomplishments) are the
system of record for their own domains and are never duplicated wholesale
into the agenda — only a pointer is mirrored so it's visible in the next
1-on-1.

## 3. Multi-tenancy: the agenda is pair-scoped, not owner-scoped

This is the one place this feature's persistence model genuinely departs
from every other table in the system. The identity-resolution work
(`app/identity/`, `app/core/scope.py`) established `OwnerScope` as the sole
access primitive: every table has exactly one `owner_user_id`, and no query
in owner-scoped code ever crosses that boundary. A rolling 1-on-1 agenda is,
by definition, **shared** between two distinct users — the manager and the
report — so it cannot be owned by a single `owner_user_id` without either
duplicating state (drift risk) or granting one side asymmetric ownership
(doesn't match "shared," and the whiteboard's own "current manager / former
manager" framing implies the relationship itself changes over time while the
report's agenda persists).

**Decision:** a new access primitive, `PairScope`, alongside — not instead
of — `OwnerScope`. `AgendaItem` and its supporting tables belong to a
`Pair`, not to either individual's `owner_user_id`. `Commitment` (already
existing, owner-scoped) and the new `Accomplishment` table (§5.4) remain
exactly as owner-scoped as every other domain table — only the *pointer*
that surfaces a ledger event on the agenda is pair-scoped.

### 3.1 `Pair` — the report's manager history

```python
class Pair(Base):
    __tablename__ = "pairs"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    report_user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), index=True)
    manager_user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), index=True)
    started_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
```

The agenda is anchored to `report_user_id` — the stable party across a
manager change — not to a single `Pair` row. `AgendaItem.report_user_id`
denormalizes this so the agenda survives a manager transition without a
migration. At most one `Pair` row per `report_user_id` has `ended_at IS
NULL` (the current manager); enforced by a partial unique index, the same
pattern `Identity`'s cache index uses in `app/identity/models.py`.

**Manager-transition access — resolved: full cutoff.** When a `Pair` ends
(`ended_at` set), the former manager loses *all* access — read and write —
immediately. The current manager and the report have full read/write
access, including history from before the transition.

This was a real back-and-forth, worth recording: a read-only carve-out for
former managers (skip-level continuity, "what were you working on with
X") was considered and briefly specified, then dropped. Reasoning: that
need is better served by the ledger surfaces this system already has
(`Commitment`, `Accomplishment`) than by raw agenda access, and standing
ex-manager visibility into an ongoing 1-on-1 relationship — even limited to
`shared` items — creates a quiet chilling effect where a report
self-censors knowing visibility never goes away. This matches how
comparable 1-on-1 tools scope access strictly to the *current* reporting
relationship.

### 3.2 `PairScope` — the enforcement mechanism

Mirrors `OwnerScope`'s shape and role exactly (`app/core/scope.py`):
membership is binary (a valid `PairScope` or none at all), the same as
`OwnerScope` — no read/write split needed now that former managers have no
access at all rather than a reduced tier of it.

```python
@dataclass(frozen=True)
class PairScope:
    report_user_id: str  # anchor — stable across manager transitions
    acting_user_id: str  # must be report_user_id or the CURRENT manager_user_id
    session: Session

    def query(self, model): ...   # auto-filters .where(model.report_user_id == self.report_user_id)
    def add(self, instance): ...  # stamps report_user_id, rejects mismatch
    def commit(self): ...
```

**`report_user_id`, not `pair_id`, is still the anchor** — even with full
cutoff, the *report's own* view needs to span every `Pair` era they've ever
had, and the current manager needs the report's full history too, not just
what accumulated since they became manager. Scoping queries to a single
`pair_id` can't express "the report's whole agenda, across transitions";
every query in `app/agenda/` filters on `report_user_id` instead, matching
`AgendaItem.report_user_id`'s denormalization in §5.1. Only *membership* —
who is allowed to construct a `PairScope` at all — checks the current
`Pair` row.

**Building a `PairScope` for a given `(report_user_id, acting_user_id)`**
(a small resolver function, not part of the dataclass itself — call it
`resolve_pair_scope(session, report_user_id, acting_user_id) -> PairScope |
None`):

- `acting_user_id == report_user_id` → a `PairScope`
- `acting_user_id == manager_user_id` of the *current* (`ended_at IS NULL`)
  `Pair` for this `report_user_id` → a `PairScope`
- anything else — including a former manager, i.e. `acting_user_id ==
  manager_user_id` of a *past* `Pair` for this `report_user_id` — → `None`

`None` is rejected outright, the same "halt, don't guess" discipline
`one-on-one-agenda.md`'s skill doc already states for unresolved identity —
there is no reduced-access path to fall into.

Same discipline as `OwnerScope` otherwise: no function in the new
`app/agenda/` module accepts a bare `Session`, and — following the
precedent set by identity resolution's Task 15 — a static AST scope-leak
check is part of this feature's own test suite from the start, not bolted
on at the end.

## 4. Architecture

### 4.1 `RollingAgendaOrchestrator` (custom `BaseAgent`)

Pure routing, no LLM, no business logic. Reads `trigger_type` from session
state and dispatches:

| `trigger_type` | Path |
|---|---|
| `ledger_event` | function-tool path, §4.2 |
| `manual_note` | function-tool path, §4.3 |
| `meeting_end` | `SequentialAgent`, §4.4 |

If you're tempted to put dedup logic, schema validation, or consent-threshold
logic in the orchestrator, it belongs one layer down in the store (§4.5)
instead — the orchestrator stays thin enough to read in one sitting.

### 4.2 Ledger/signal event path (function tools, no agent)

The four source kinds split into two fundamentally different write shapes,
and conflating them was an explicit mistake to avoid repeating:

- **`kind ∈ {accomplishment, commitment}`** → `append_ledger_item(scope,
  item)`. Writes to the durable, owner-scoped ledger (`Commitment` today;
  `Accomplishment`, §5.4, new) — a commitment logged in March must still be
  independently queryable in June, long after any related agenda item is
  resolved. After the durable write, `append_ledger_item` mirrors a
  lightweight pointer into the agenda store via `append_agenda_item`
  internally — never a second full copy, just `{source: "commitment_ledger"
  | "accomplishment_ledger", source_link: <ledger row id>}`.

- **`kind ∈ {jira_blocker, slack_q}`** → `append_agenda_item(scope, item)`
  directly. No durable ledger copy — Jira/Slack are already the system of
  record, and a second permanent copy would drift out of sync. `source_link`
  points at the real Jira/Slack object.

`append_ledger_item`'s `if kind in (accomplishment, commitment)` branch is
the load-bearing line of this whole path; the two functions are not merged
into one generic `append(kind, item)`.

### 4.3 Manual note (standalone, not part of post-meeting review)

Either party can add a note at any time — "I thought of something Tuesday,
don't want to forget it by Friday's meeting" — independent of the meeting
cadence. `add_manual_note(scope, acting_user_id, text, visibility)`:

- `source = "manual"`, `source_link = None` (usually nothing external to
  point at)
- `created_by = {user_id: acting_user_id, role}` — role derived from
  `Pair.report_user_id`/`manager_user_id`, not user-supplied
- `visibility` defaults to `"shared"` unless the author explicitly marks it
  `manager_only`/`report_only` — **opt-in privacy, not opt-in sharing**
- `status = "open"`; no `surfaced_count` threshold applies at creation (that
  logic only fires once an item has actually been surfaced and gone
  unaddressed, §4.3.1)
- Skips the dedup-on-`source_link` check the ledger/signal path uses — a
  manual note usually has nothing external to dedup against

Entry surface: reuses whatever channel delivers the main summary (a small
A2UI quick-add card, or a slash command) — one consistent entry point, not a
separate bespoke UI.

### 4.3.1 Consent-on-stale-items

When `surfaced_count` crosses `PENDING_CONSENT_THRESHOLD` (config constant,
default 5 — following the identity-resolution precedent of never inlining
gate thresholds, this lives in `app/agenda/config.py`), the store does
**not** silently drop the item and does **not** keep re-surfacing it
forever. It flips `status → "pending_consent"` and waits for a human
decision — age alone doesn't mean an item stopped mattering, but five
straight unaddressed resurfacings is a strong enough signal to ask rather
than assume.

The threshold check is a plain conditional inside `append_agenda_item` /
`update_agenda_item`, at the point `surfaced_count` increments (which
happens whenever `SynthesizeOutcomeAgent` matches a new meeting's output
back to an existing item, §4.4b) — not a separate agent:

```python
if item.surfaced_count >= config.PENDING_CONSENT_THRESHOLD:
    item.status = "pending_consent"
```

`build_a2ui_payload` (§4.4c) renders one consent card per pending-consent
item as part of the same batched editable summary — never as separate
one-off prompts; batching is exactly what A2UI is meant to avoid needing.

Handling the response (via the AG-UI signal handler, §4.6): "Keep" resets
`surfaced_count → 0`, `status → "open"`, appends a `history` entry. "Drop"
sets `status → "resolved"`, `resolved_at`, and a `history` entry that
records this was a *consent* resolution, distinct from a genuine
completion — that distinction matters for ever reporting "how many things
just faded out" vs. "how many things actually got done." If the item's
`visibility` is `manager_only`/`report_only`, only the visible party's
response resolves it. A former manager can't respond at all — `§3.2`'s
`resolve_pair_scope` returns `None` for them, so they never get far enough
to see the card, let alone respond to it.

### 4.4 Post-meeting flow (`SequentialAgent`, 3 sub-agents)

Runs once per meeting, fixed order, each step depending on the last:

**a. `CaptureOutputAgent`** (`LlmAgent`) — tool `fetch_transcript(meeting_id)`.
No transcript ⇒ fall back to a prompt-for-input tool. Never hallucinate
meeting content; asking is strictly better than guessing.

**b. `SynthesizeOutcomeAgent`** (`LlmAgent`) — tools, in order:
1. `get_agenda(scope)` — read first. Dedup against this: if synthesized
   output restates something already open (matched primarily on
   `source_link`, text similarity only as a fallback), bump the existing
   item's `surfaced_count` instead of creating a duplicate.
2. `append_ledger_item` / `append_agenda_item` — the identical functions
   from §4.2, reused, not duplicated. A new commitment or accomplishment
   surfaced mid-meeting routes through the same branch.

Output: structured `{decisions, commitments, focus_points}`.

**c. `DeliverSummaryAgent`** — hybrid, not a single `LlmAgent`:
- A small `LlmAgent` phrases item text for readability.
- A separate, plain Python function `build_a2ui_payload(items,
  pending_consent_items)` assembles the actual A2UI JSON tree
  deterministically. The LLM never emits A2UI JSON directly — malformed
  structure breaks rendering, and a deterministic assembly function is
  unit-testable against fixtures in a way arbitrary LLM JSON isn't.
- Includes: editable text fields, an add-item control, a per-item
  visibility toggle, and consent cards for pending-consent items.

### 4.5 Agenda store (tool/data layer, not an agent)

`get_agenda(scope)`, `append_agenda_item(scope, item)`,
`append_ledger_item(scope, item)`, `update_agenda_item(scope, item_id,
changes)`, `mark_resolved(scope, item_id)`. Every mutation appends a
`history` entry — the audit trail is not optional, matching the precedent
`MergeLog` set in identity resolution for exactly this reason.

### 4.6 AG-UI signal handler (webhook, not part of the ADK agent tree)

Receives interaction events from the A2UI client (field edits, keep/drop
clicks, resolve clicks) and calls `update_agenda_item` directly. Inbound —
the orchestrator does not poll for it; this is a separate small service
sharing the same storage layer and `PairScope` enforcement. Every inbound
signal carries the acting user's identity; the handler calls
`resolve_pair_scope(session, report_user_id, acting_user_id)` before
calling into the store and rejects outright if it returns `None` — a
former manager's edit/keep/drop signal is rejected here the same way an
unresolved-identity signal would be, not via a separate authorization
check.

### 4.7 What this feature explicitly does not use

No `LoopAgent` (nothing here iterates against a critic). No `ParallelAgent`
(ledger writes are independent events, not a batch fan-out). No `AgentTool`
(no agent calls another agent as a tool mid-reasoning). If a real need for
one of these emerges during implementation, flag it explicitly rather than
adding proactively.

## 5. Schema (`app/agenda/models.py`)

### 5.1 `AgendaItem`

```python
class AgendaItem(Base):
    __tablename__ = "agenda_items"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    report_user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), index=True)  # PairScope's query anchor (§3.2) — survives manager transitions
    pair_id: Mapped[str] = mapped_column(String, ForeignKey("pairs.id"), index=True)  # provenance only: which manager-era this item was created under
    text: Mapped[str] = mapped_column(String)
    source: Mapped[str] = mapped_column(String)  # jira | slack | accomplishment_ledger | commitment_ledger | manual | meeting_synthesis
    source_link: Mapped[str | None] = mapped_column(String, nullable=True)
    visibility: Mapped[str] = mapped_column(String, default="shared")  # shared | manager_only | report_only
    status: Mapped[str] = mapped_column(String, default="open")  # open | resolved | pending_consent
    surfaced_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by_user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"))
    created_by_role: Mapped[str] = mapped_column(String)  # manager | report
```

### 5.2 `AgendaItemHistory`

One row per mutation — never mutate `history` as a JSON blob in place (the
same reasoning that kept `MergeLog` a separate append-only table rather than
a JSON column on `Identity`):

```python
class AgendaItemHistory(Base):
    __tablename__ = "agenda_item_history"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    agenda_item_id: Mapped[str] = mapped_column(String, ForeignKey("agenda_items.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    changed_by_user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"))
    changed_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))
    diff: Mapped[dict] = mapped_column(JSON)
```

### 5.3 Dedup constraint

Partial unique index on `(report_user_id, source_link) WHERE source_link
IS NOT NULL AND status != 'resolved'` — anchored the same way `PairScope`
is (§3.2), not on `pair_id`, so a dedup check works correctly across a
manager transition. Same `sqlalchemy.text(...)` `sqlite_where`/
`postgresql_where` pattern `Identity` uses. This is what makes
`get_agenda`'s dedup-by-`source_link` check race-safe rather than purely a
courtesy check in application code.

### 5.4 `Accomplishment` (new, minimal — owner-scoped)

`append_ledger_item`'s `kind = accomplishment` branch needs a durable target
and none exists yet (F5, the Accomplishment Ledger, is a separate unshipped
feature per `AGENT.md`'s feature table). Rather than block this feature on
F5's full scope, this spec adds the minimal owner-scoped table F2 actually
needs to write to — F5 can extend it later, but F2 must not invent a
throwaway parallel store:

```python
class Accomplishment(Base):
    __tablename__ = "accomplishments"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), index=True)
    description: Mapped[str] = mapped_column(String)
    source_reference_key: Mapped[str] = mapped_column(String)
    occurred_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))
```

Mirrors `Commitment`'s shape deliberately. Flagged in §12 in case you'd
rather this feature stub `kind=accomplishment` out entirely and defer it to
F5's own plan.

## 5.5 HRIS ingestion (Layer 2/3) — how `Pair` rows actually get created

Confirmed via `AGENT.md`/`.agents/workflows/phase-gate.md`: HRIS is a
planned Layer-2 connector, not yet built ("Not started," P4 phasing).
Unlike identity-resolution's deferred IdP connector, this plan **does**
include a live vendor: **BambooHR**, via the `bamboohr-mcp` community MCP
server (`npx -y bamboohr-mcp` — full API coverage across 11 modules incl.
employees, 71 tests claimed in its README; chosen over the narrower
`@aot-tech/bamboohr-mcp-server` for headroom if a later feature needs more
than the employee directory). Exact tool names/argument shapes will be
confirmed via live `session.list_tools()` introspection during
implementation — the same discipline every existing connector in
`app/ingest/live_source.py` followed (its own docstring is explicit that
each adapter was "written defensively" against a server's *documented or
typical* shape, then proven against a real session) — not guessed at spec
time.

**Layer 2 — `app/ingest/hris_source.py`:** conforms to the existing
`SourceClient` Protocol (`app/ingest/base.py`) — `fetch(window,
owner_user_id) -> list[dict]`, `health()`. Raw payload shape (one dict per
manager-report edge — field names are BambooHR's actual "Get Employees"
report fields, confirmed at implementation time, not invented here):
`{employee_hris_id, manager_hris_id, effective_date}`.

- `FixtureHrisClient` — `app/ingest/fixture_source.py` pattern, drives all
  tests (§8, §9's build order keeps this first).
- `LiveBambooHrClient` — `app/ingest/live_source.py` pattern, backed by a
  new `bamboohr_mcp_spec(api_key, subdomain)` added to
  `app/tools/mcp_config.py` (BambooHR's REST API auths via an API key +
  company subdomain, the same "base_url/email/api_token"-shaped credential
  bundle `jira_mcp_spec` already takes — no new credential-handling pattern
  needed). `health()` follows the existing convention: checks the API
  key/subdomain env vars are set, not that a live call has succeeded — same
  caveat `LiveCalendarClient.health()` already documents for OAuth.

**New `User` field, following the `slack_user_id` precedent exactly:**
`User` currently has no email or external-directory identifier at all —
`slack_user_id` is the one precedent, nullable, set via an explicit
`link-slack` step rather than auto-matched. HRIS gets the same treatment:

```python
hris_employee_id: Mapped[str | None] = mapped_column(String, unique=True, nullable=True)
```

set via an equivalent explicit `link-hris` step (CLI command, mirrors
`link-slack`). This deliberately avoids inventing a new cross-system
identity-matching mechanism at this layer — that's what the identity graph
(`app/identity/`) is for, and it resolves *within* one user's private
roster, not to system `User` accounts, which is a different problem this
field solves directly instead.

**Layer 3 — `sync_hris_edge(session, raw, clock) -> Pair | None`** (in
`app/agenda/store.py` or a new `app/ingest/hris_normalize.py` — your call
at plan time, no strong reason either way): idempotent upsert into `pairs`,
resolving `employee_hris_id`/`manager_hris_id` to `User.id` via
`hris_employee_id`. Returns `None` and logs (never raises) if either side
hasn't been linked yet — a real, expected steady-state condition, not an
error, since `link-hris` runs independently of the ingestion schedule.
When the manager for a `report_user_id` changes, this closes the prior
active `Pair` (`ended_at = effective_date`) and opens a new one — the
partial-unique-active-`Pair`-per-report constraint (§3.1) is what makes
this safe under a re-run.

**Deliberate scope exception — this is the one write path in this feature
that does NOT go through `OwnerScope` or `PairScope`.** Creating a `Pair`
is inherently a cross-user, system-level bootstrapping operation — neither
party's scope object can exist yet to gate it, the same reason raw `User`
row creation itself sits outside `OwnerScope`. This gets its own dedicated
test coverage (idempotency, manager-transition correctness, unlinked-user
handling) rather than being swept under the `app/agenda/` scope-leak AST
gate, which stays scoped to `app/agenda/*.py`'s `PairScope`-mediated code —
`hris_normalize.py`, if split out, is deliberately outside that glob.

**Adjacent opportunity, explicitly out of scope for this plan:** identity
resolution's `match_idp` (`app/identity/matchers.py`) currently always
returns `None` — "HRIS-backed matching gets the matcher interface but no
real connector" per that spec's own scope section. Once `hris_employee_id`
exists and is populated, tier-0 IdP/directory matching becomes buildable.
Flagging this so it isn't lost, but wiring it up is a separate, later
change to `app/identity/`, not part of this plan.

## 6. Module structure (`app/agenda/`)

Following the `app/identity/` precedent (`config.py` / `types.py` /
`models.py` / the business-logic modules), plus the ADK agent tree:

- `app/agenda/config.py` — `PENDING_CONSENT_THRESHOLD`, any other gate
  constants
- `app/agenda/models.py` — `Pair`, `AgendaItem`, `AgendaItemHistory`,
  `Accomplishment`
- `app/agenda/store.py` — `get_agenda`, `append_agenda_item`,
  `append_ledger_item`, `update_agenda_item`, `mark_resolved` — all take
  `PairScope`/`OwnerScope` first, per the "no bare `Session`" convention
- `app/agenda/payload.py` — `build_a2ui_payload(items,
  pending_consent_items)`, the deterministic, unit-testable assembly
  function
- `app/sub_agents/agenda/` — `RollingAgendaOrchestrator` (custom
  `BaseAgent`), the post-meeting `SequentialAgent` and its three
  sub-agents (`CaptureOutputAgent`, `SynthesizeOutcomeAgent`,
  `DeliverSummaryAgent`), following the existing `app/sub_agents/pulse/`
  layout convention
- `app/triggers/agenda_signal_handler.py` (or similar) — the AG-UI webhook
  endpoint, outside the ADK agent tree, per §4.6
- `app/ingest/hris_source.py` — `HrisSourceClient` Protocol conformance +
  `FixtureHrisClient`, following `app/ingest/fixture_source.py`'s pattern
  (§5.5)
- `app/ingest/hris_normalize.py` — `sync_hris_edge`, the one write path in
  this feature deliberately outside `OwnerScope`/`PairScope` (§5.5)

## 7. Error handling

- No transcript for a meeting ⇒ prompt-for-input tool, never fabricate
  content (§4.4a).
- `add_manual_note` with no resolvable `Pair` for the acting user ⇒ reject,
  same "halt, don't guess" discipline `one-on-one-agenda.md`'s skill doc
  already states for unresolved identity.
- A former manager (or anyone with no relationship to the report, past or
  present) attempting any read or write ⇒ `resolve_pair_scope` returns
  `None`, rejected outright before any query or write happens (§3.2).
- Concurrent writes to the same `source_link` ⇒ caught by §5.3's partial
  unique index; `append_ledger_item`/`append_agenda_item` treat the
  resulting constraint violation as "already exists, dedup" rather than
  propagating an error — same shape as identity resolution's cache-hit
  handling in `resolve()`.
- A `visibility`-restricted item's consent response from the non-visible
  party ⇒ ignored, not an error (§4.3.1).

## 8. Testing

- `PairScope` gets the same treatment `OwnerScope` got: a cross-report
  isolation test (two reports, same `source_link` text, never leak into
  each other's agenda) and a static AST scope-leak check over
  `app/agenda/*.py`, from the start rather than retrofitted.
- Manager-transition access, specifically: `resolve_pair_scope` returns
  `None` for a former manager (no read, no write, full cutoff); current
  manager and report both resolve successfully and see full history
  across the transition; `resolve_pair_scope` also returns `None` for a
  user with no relationship to the report at all.
- `append_ledger_item` vs `append_agenda_item` branching tested in
  isolation with fake data before any LLM involvement (per the build order
  in §9).
- `build_a2ui_payload` tested against fixture data, never live LLM output —
  same reasoning `app/fixtures/pulse/` already established for this
  codebase's other composition-layer testing.
- Consent-threshold flow: a dedicated test seeding `surfaced_count` at
  `PENDING_CONSENT_THRESHOLD - 1`, bumping it once more, asserting
  `status == "pending_consent"`; a second test for the Keep/Drop response
  paths, including the `visibility`-restricted-party-ignored case.
- Dedup: two `append_ledger_item`/`append_agenda_item` calls with the same
  `source_link` produce one row, `surfaced_count` incremented, not two
  rows.

## 9. Build order

1. `User.hris_employee_id` + `link-hris` step; `Pair`, `PairScope`,
   `AgendaItem`/`AgendaItemHistory`/`Accomplishment` schema + migration
2. `FixtureHrisClient` + `sync_hris_edge` — idempotency and
   manager-transition tests, fake data, no LLM, no scope object (§5.5)
3. `LiveBambooHrClient` + `bamboohr_mcp_spec` — real `session.list_tools()`
   introspection first (confirm the actual tool/argument shape, per the
   discipline every other live connector followed), then a live round-trip
   against a real BambooHR account
4. `append_ledger_item` / `append_agenda_item` — branching logic in
   isolation, fake data, no LLM
5. `add_manual_note` — auth/role attribution
6. `CaptureOutputAgent` + `SynthesizeOutcomeAgent` — against canned
   transcripts
7. `build_a2ui_payload` — against fixture data
8. `SequentialAgent` wiring (post-meeting flow)
9. `RollingAgendaOrchestrator` routing — thinnest layer, built last
10. AG-UI signal handler — needs a real renderer to test against

## 10. Scope

**In scope:** everything in §4–§9 above — persistence, routing, dedup,
consent flow, post-meeting synthesis, A2UI payload assembly, the signal
handler, and fixture-backed HRIS ingestion (§5.5) to actually populate
`Pair` rows.

**Out of scope for this plan:**
- A2UI component catalog/visual design (consumes whatever the standard ADK
  A2UI catalog provides; no custom component work here)
- The Accomplishment Ledger's own reporting/query surface (F5) — this plan
  only adds the minimal write target F2 needs
- Slack/Jira ingestion themselves (assumed to already produce `WorkItem`/
  `Message` rows upstream, per the existing 7-layer pipeline; this feature
  only consumes signals already normalized by Layers 2–3)
- Notification budget/suppression gate interaction (Layer 5) — assumed the
  post-meeting summary delivery goes through the same salience/budget path
  every other card does; not re-specified here
- Wiring `match_idp` to the new `hris_employee_id` field (§5.5's "adjacent
  opportunity") — a separate, later change to `app/identity/`, even though
  this plan's live BambooHR connector makes the data available

## 11. Relationship to existing skill docs

`app/skills/one-on-one-agenda.md` and `app/skills/commitment-capture.md`
already describe the *prompt-level* behavior this feature implements
(card shape, edge cases like "agenda past 8 items," "user edits an item").
Those skill docs are the Layer 6 composition instructions; this spec is the
Layer 4–7 plumbing (data model, routing, delivery mechanics) that makes
those instructions executable. No conflict was found between the two during
this review — `one-on-one-agenda.md`'s "Append and close; never regenerate"
and "identity unresolved for the counterpart ⇒ halt" rules are both already
reflected in §2 and §7 above.

## 12. Open questions for the implementation plan

1. ~~**Former-manager access after a `Pair` transition**~~ — **Resolved:**
   full cutoff, no read or write access at all (§3.1, §3.2). A read-only
   carve-out was considered and rejected in favor of routing any
   continuity/audit need through the existing ledger surfaces instead.
2. **`Accomplishment` table scope** (§5.4): this spec adds the minimal
   version F2 needs. Confirm that's preferable to stubbing
   `kind=accomplishment` out of F2 entirely and deferring to F5.
3. ~~**`Pair` creation**~~ — **Resolved:** in scope, including a live
   connector. §5.5 adds HRIS ingestion (`SourceClient` conformance,
   `sync_hris_edge`, the new `User.hris_employee_id`/`link-hris`
   precedent-following field), backed by BambooHR via the `bamboohr-mcp`
   community MCP server — unlike identity-resolution's deferred IdP
   connector, this one ships live in this plan.
4. ~~**A2UI first use**~~ — **Resolved:** intentional. No change needed.
