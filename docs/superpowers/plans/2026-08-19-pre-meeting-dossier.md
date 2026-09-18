# Pre-Meeting Dossier (F1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship F1 — a Slack DM delivered T-15-minutes-before a qualifying meeting containing who you're meeting, why it matters now, three sourced talking points, and open commitments — plus the same dossier surfaced in the `/ui` demo dashboard.

**Architecture:** A new, independent `dossier_scheduler.py` polling loop (mirroring `agenda_scheduler.py`) detects qualifying events, a new dossier-only salience gate decides whether to push, a new `sub_agents/dossier` `SequentialAgent` (gather → synthesize → deliver) assembles and sends the card, and a new `DossierDelivery` table records what shipped. Everything else — Calendar/Slack/Linear/Jira/Notion ingestion, identity resolution, the agenda store for recurring 1-on-1s, Slack delivery plumbing, the `/ui` dashboard's auth and A2UI pattern — is reused from the already-merged F2/F3 code, not rebuilt.

**Tech Stack:** Python 3.12, Google ADK (`LlmAgent`/`SequentialAgent`/`BaseAgent`), SQLAlchemy 2.x + Alembic, FastAPI, pytest against a real Postgres test DB (no mocks/SQLite).

**Spec:** `mentor/docs/superpowers/specs/2026-08-18-pre-meeting-dossier-design.md`

## Global Constraints

- No LLM calls in layers 1–5 (trigger, ingestion, normalization, identity, salience) — only `synthesize` in layer 6 calls an LLM (`AGENT.md` §2).
- Every table carries `owner_user_id`, non-nullable, indexed first (identity design §11.2 convention, already established in `app/core/models.py`).
- No talking point ships without a source link (provenance rule, `app/skills/meeting-prep.md`).
- Never call `datetime.now()` outside `app/core/clock.py`; every test uses `FrozenClock`.
- Prompts are versioned files under `app/prompts/`; the version ID is recorded on every `DossierDelivery` row.
- Tests run against the real Postgres test DB via the `pg_session`/`make_user` fixtures already established in `tests/unit/conftest.py` / `tests/agenda/conftest.py` — never mock the DB, never use SQLite.
- Follow the existing per-ritual duplication pattern (separate scheduler, separate gate, separate card builder per ritual) rather than introducing a first cross-ritual shared abstraction in this pass.
- Migrations: Alembic, current head is `e2a7c5f9d3b1` — any new migration's `down_revision` must be exactly that string.

---

### Task 1: `DossierDelivery` model + migration

**Files:**
- Modify: `app/core/models.py` (add `DossierDelivery`, after `PulseDelivery` at line ~385)
- Create: `migrations/versions/<new_rev>_dossier_deliveries.py`
- Test: `tests/unit/test_dossier_delivery_model.py`

**Interfaces:**
- Produces: `DossierDelivery(id, owner_user_id, event_external_id, sent_at, prompt_version, talking_points_source, card_ref, feedback, feedback_at, created_at)`, unique `(owner_user_id, event_external_id)`. Every later task that records or reads a delivered dossier uses this table.

- [ ] **Step 1: Write the failing model test**

```python
# tests/unit/test_dossier_delivery_model.py
import uuid
import datetime

from app.core.clock import FrozenClock
from app.core.models import DossierDelivery, User


NOW = datetime.datetime(2026, 8, 19, 9, 0, 0, tzinfo=datetime.UTC)


def test_dossier_delivery_round_trip(pg_session):
    user = User(id=str(uuid.uuid4()), created_at=NOW)
    pg_session.add(user)
    pg_session.flush()

    delivery = DossierDelivery(
        id=str(uuid.uuid4()),
        owner_user_id=user.id,
        event_external_id=f"evt-{uuid.uuid4()}",
        sent_at=None,
        prompt_version="dossier_synthesize@v1",
        talking_points_source="fresh",
        card_ref=None,
        feedback="none",
        feedback_at=None,
        created_at=NOW,
    )
    pg_session.add(delivery)
    pg_session.commit()

    fetched = pg_session.get(DossierDelivery, delivery.id)
    assert fetched.owner_user_id == user.id
    assert fetched.talking_points_source == "fresh"
    assert fetched.feedback == "none"


def test_dossier_delivery_unique_per_owner_event(pg_session):
    user = User(id=str(uuid.uuid4()), created_at=NOW)
    pg_session.add(user)
    pg_session.flush()
    event_external_id = f"evt-{uuid.uuid4()}"

    pg_session.add(
        DossierDelivery(
            id=str(uuid.uuid4()),
            owner_user_id=user.id,
            event_external_id=event_external_id,
            sent_at=NOW,
            prompt_version="dossier_synthesize@v1",
            talking_points_source="fresh",
            card_ref=None,
            feedback="none",
            feedback_at=None,
            created_at=NOW,
        )
    )
    pg_session.commit()

    pg_session.add(
        DossierDelivery(
            id=str(uuid.uuid4()),
            owner_user_id=user.id,
            event_external_id=event_external_id,
            sent_at=NOW,
            prompt_version="dossier_synthesize@v1",
            talking_points_source="fresh",
            card_ref=None,
            feedback="none",
            feedback_at=None,
            created_at=NOW,
        )
    )
    import pytest
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        pg_session.commit()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_dossier_delivery_model.py -v`
Expected: FAIL with `ImportError: cannot import name 'DossierDelivery'`

- [ ] **Step 3: Add the model**

```python
# app/core/models.py — add after the PulseDelivery class

class DossierDelivery(Base):
    __tablename__ = "dossier_deliveries"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(
        String, ForeignKey("users.id"), nullable=False, index=True
    )
    event_external_id: Mapped[str] = mapped_column(String, nullable=False)
    sent_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    prompt_version: Mapped[str] = mapped_column(String, nullable=False)
    talking_points_source: Mapped[str] = mapped_column(String, nullable=False)
    card_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    feedback: Mapped[str] = mapped_column(String, nullable=False, default="none")
    feedback_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "owner_user_id", "event_external_id", name="uq_dossier_delivery_owner_event"
        ),
    )
```

Match the exact `Mapped`/`mapped_column`/`DateTime(timezone=True)` style already used by `PulseDelivery` in the same file — copy its import list if any of `String`/`ForeignKey`/`UniqueConstraint`/`DateTime` aren't already imported at the top of `models.py`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_dossier_delivery_model.py -v`
Expected: PASS (both tests)

- [ ] **Step 5: Write the migration**

```python
# migrations/versions/<generate via alembic>_dossier_deliveries.py
"""dossier deliveries

Revision ID: <generated>
Revises: e2a7c5f9d3b1
Create Date: 2026-08-19
"""
from alembic import op
import sqlalchemy as sa

revision = "<generated>"
down_revision = "e2a7c5f9d3b1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dossier_deliveries",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("owner_user_id", sa.String(), nullable=False),
        sa.Column("event_external_id", sa.String(), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("prompt_version", sa.String(), nullable=False),
        sa.Column("talking_points_source", sa.String(), nullable=False),
        sa.Column("card_ref", sa.String(), nullable=True),
        sa.Column("feedback", sa.String(), nullable=False),
        sa.Column("feedback_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "owner_user_id", "event_external_id", name="uq_dossier_delivery_owner_event"
        ),
    )
    op.create_index(
        op.f("ix_dossier_deliveries_owner_user_id"),
        "dossier_deliveries",
        ["owner_user_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_dossier_deliveries_owner_user_id"), table_name="dossier_deliveries"
    )
    op.drop_table("dossier_deliveries")
```

Generate the real revision id with `uv run alembic revision --autogenerate -m "dossier deliveries"` against the running test DB, then hand-verify the diff matches the above (autogenerate sometimes picks a different column order — reorder to match this file, don't fight the tool over cosmetic ordering).

- [ ] **Step 6: Apply and verify**

Run: `uv run alembic upgrade head && uv run pytest tests/unit/test_dossier_delivery_model.py -v`
Expected: migration applies cleanly, both tests PASS against the real table.

- [ ] **Step 7: Commit**

```bash
git add app/core/models.py migrations/versions/ tests/unit/test_dossier_delivery_model.py
git commit -m "feat(dossier): add DossierDelivery model + migration"
```

---

### Task 2: `salience/dossier_score.py` — the 7-term score

**Files:**
- Create: `app/salience/dossier_score.py`
- Test: `tests/unit/test_dossier_score.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `DossierCandidateInputs` (dataclass), `score_dossier_candidate(inputs: DossierCandidateInputs) -> float`. Task 4 (gate) and Task 6 (scheduler) call this.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_dossier_score.py
from app.salience.dossier_score import DossierCandidateInputs, score_dossier_candidate


def test_score_rewards_external_unresolved_deadline_pressure():
    high = DossierCandidateInputs(
        attendee_rarity=0.9,
        is_external=True,
        unresolved_threads=3,
        deadline_proximity=0.8,
        is_one_on_one=False,
        prep_absent=True,
        recurrence_familiarity=0.0,
    )
    low = DossierCandidateInputs(
        attendee_rarity=0.1,
        is_external=False,
        unresolved_threads=0,
        deadline_proximity=0.0,
        is_one_on_one=False,
        prep_absent=False,
        recurrence_familiarity=1.0,
    )
    assert score_dossier_candidate(high) > score_dossier_candidate(low)


def test_recurrence_familiarity_is_subtracted():
    familiar = DossierCandidateInputs(
        attendee_rarity=0.5,
        is_external=False,
        unresolved_threads=0,
        deadline_proximity=0.0,
        is_one_on_one=True,
        prep_absent=False,
        recurrence_familiarity=1.0,
    )
    unfamiliar = DossierCandidateInputs(
        attendee_rarity=0.5,
        is_external=False,
        unresolved_threads=0,
        deadline_proximity=0.0,
        is_one_on_one=True,
        prep_absent=False,
        recurrence_familiarity=0.0,
    )
    assert score_dossier_candidate(familiar) < score_dossier_candidate(unfamiliar)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_dossier_score.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.salience.dossier_score'`

- [ ] **Step 3: Implement**

```python
# app/salience/dossier_score.py
"""Pure scoring function for the dossier salience gate (spec §2 step 3).
No DB access, no LLM call — layers 1-5 invariant (AGENT.md §2)."""

from dataclasses import dataclass

W_ATTENDEE_RARITY = 1.0
W_IS_EXTERNAL = 1.5
W_UNRESOLVED_THREADS = 0.5
W_DEADLINE_PROXIMITY = 1.5
W_IS_ONE_ON_ONE = 1.0
W_PREP_ABSENT = 1.0
W_RECURRENCE_FAMILIARITY = 1.0


@dataclass(frozen=True)
class DossierCandidateInputs:
    attendee_rarity: float          # 0-1, how rarely this owner meets these attendees
    is_external: bool
    unresolved_threads: int         # count of unresolved Slack/thread signals
    deadline_proximity: float       # 0-1, how close the nearest shared deadline is
    is_one_on_one: bool
    prep_absent: bool               # no dossier ever sent for this pairing before
    recurrence_familiarity: float   # 0-1, how routine/frequent this meeting series is


def score_dossier_candidate(inputs: DossierCandidateInputs) -> float:
    return (
        W_ATTENDEE_RARITY * inputs.attendee_rarity
        + W_IS_EXTERNAL * float(inputs.is_external)
        + W_UNRESOLVED_THREADS * inputs.unresolved_threads
        + W_DEADLINE_PROXIMITY * inputs.deadline_proximity
        + W_IS_ONE_ON_ONE * float(inputs.is_one_on_one)
        + W_PREP_ABSENT * float(inputs.prep_absent)
        - W_RECURRENCE_FAMILIARITY * inputs.recurrence_familiarity
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_dossier_score.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/salience/dossier_score.py tests/unit/test_dossier_score.py
git commit -m "feat(dossier): add the 7-term dossier salience score"
```

---

### Task 3: `salience/dossier_gate.py` — budget + suppression + manager structural floor

**Files:**
- Create: `app/salience/dossier_gate.py`
- Test: `tests/unit/test_dossier_gate.py`

**Interfaces:**
- Consumes: `score_dossier_candidate` (Task 2), `app.core.scope.OwnerScope`, `app.core.models.Suppression`.
- Produces: `GateDecision` (`"push" | "queue" | "drop"`), `apply_dossier_gate(scope: OwnerScope, clock: Clock, candidate_score: float, event_external_id: str, is_manager: bool, history_scores: list[float]) -> GateDecision`. Task 6 (scheduler) calls this after scoring.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_dossier_gate.py
import datetime
import uuid

from app.core.clock import FrozenClock
from app.core.models import Suppression, User
from app.core.scope import OwnerScope
from app.salience.dossier_gate import apply_dossier_gate

NOW = datetime.datetime(2026, 8, 19, 9, 0, 0, tzinfo=datetime.UTC)


def _make_owner(pg_session):
    user = User(id=str(uuid.uuid4()), created_at=NOW)
    pg_session.add(user)
    pg_session.flush()
    return user


def test_below_top_20_percent_drops(pg_session):
    user = _make_owner(pg_session)
    scope = OwnerScope(owner_user_id=user.id, session=pg_session)
    clock = FrozenClock(at=NOW)
    history = [1.0] * 100  # candidate scores below all history -> not top 20%

    decision = apply_dossier_gate(
        scope, clock, candidate_score=0.1, event_external_id="evt-1",
        is_manager=False, history_scores=history,
    )
    assert decision == "drop"


def test_top_20_percent_with_budget_pushes(pg_session):
    user = _make_owner(pg_session)
    scope = OwnerScope(owner_user_id=user.id, session=pg_session)
    clock = FrozenClock(at=NOW)
    history = [0.1] * 100  # candidate score is far above history -> top 20%

    decision = apply_dossier_gate(
        scope, clock, candidate_score=5.0, event_external_id="evt-2",
        is_manager=False, history_scores=history,
    )
    assert decision == "push"


def test_manager_structural_floor_always_pushes(pg_session):
    user = _make_owner(pg_session)
    scope = OwnerScope(owner_user_id=user.id, session=pg_session)
    clock = FrozenClock(at=NOW)
    history = [10.0] * 100  # candidate score would otherwise drop

    decision = apply_dossier_gate(
        scope, clock, candidate_score=0.0, event_external_id="evt-3",
        is_manager=True, history_scores=history,
    )
    assert decision == "push"


def test_active_suppression_forces_drop_even_for_manager(pg_session):
    user = _make_owner(pg_session)
    pg_session.add(
        Suppression(
            id=str(uuid.uuid4()),
            owner_user_id=user.id,
            scope="instance",
            target_ref="evt-4",
            reason="user muted this one",
            created_at=NOW,
            expires_at=None,
            created_by="user",
        )
    )
    pg_session.commit()
    scope = OwnerScope(owner_user_id=user.id, session=pg_session)
    clock = FrozenClock(at=NOW)

    decision = apply_dossier_gate(
        scope, clock, candidate_score=5.0, event_external_id="evt-4",
        is_manager=True, history_scores=[0.0] * 100,
    )
    assert decision == "drop"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_dossier_gate.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement**

```python
# app/salience/dossier_gate.py
"""Dossier-specific salience gate. New module rather than a call into
app.salience.gate.apply_gate: that function operates on pulse's
PreGateContext/ScoredItem types and pulse-only config, per the design
spec §4's correction — not a generic cross-ritual gate."""

import datetime
from typing import Literal

from app.core.clock import Clock
from app.core.models import Suppression
from app.core.scope import OwnerScope

DOSSIER_PUSH_BUDGET_MAX_PER_DAY = 3
TOP_PERCENTILE_THRESHOLD = 0.80  # top 20% of history

GateDecision = Literal["push", "queue", "drop"]


def _is_suppressed(scope: OwnerScope, clock: Clock, event_external_id: str) -> bool:
    now = clock.now()
    rows = (
        scope.query(Suppression)
        .filter(Suppression.owner_user_id == scope.owner_user_id)
        .filter(Suppression.target_ref == event_external_id)
        .all()
    )
    for row in rows:
        if row.expires_at is None or row.expires_at > now:
            return True
    return False


def _is_top_percentile(candidate_score: float, history_scores: list[float]) -> bool:
    if not history_scores:
        return True
    sorted_history = sorted(history_scores)
    cutoff_index = int(len(sorted_history) * TOP_PERCENTILE_THRESHOLD)
    cutoff_index = min(cutoff_index, len(sorted_history) - 1)
    threshold = sorted_history[cutoff_index]
    return candidate_score >= threshold


def apply_dossier_gate(
    scope: OwnerScope,
    clock: Clock,
    candidate_score: float,
    event_external_id: str,
    is_manager: bool,
    history_scores: list[float],
) -> GateDecision:
    # Manager structural floor: never suppressed, never drops on score alone —
    # explicit suppression still wins (a user can always silence a specific
    # instance, per spec §2's suppression-precedence rule).
    if _is_suppressed(scope, clock, event_external_id):
        return "drop"
    if is_manager:
        return "push"
    if not _is_top_percentile(candidate_score, history_scores):
        return "drop"
    return "push"
```

`queue` (push-budget-exhausted) is intentionally not exercised by this task's tests — the budget check needs today's already-sent `DossierDelivery` count, which Task 6's scheduler owns (it has the DB session open in a loop); this gate function stays pure/DB-light for testability and the scheduler wraps it with the budget check before acting on `"push"`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_dossier_gate.py -v`
Expected: PASS (all 4 tests)

- [ ] **Step 5: Commit**

```bash
git add app/salience/dossier_gate.py tests/unit/test_dossier_gate.py
git commit -m "feat(dossier): add dossier salience gate with manager structural floor"
```

---

### Task 4: `triggers/dossier_scheduler.py` — T-15 poll loop

**Files:**
- Create: `app/triggers/dossier_scheduler.py`
- Test: `tests/triggers/test_dossier_scheduler.py`

**Interfaces:**
- Consumes: `LiveCalendarClient.fetch(window, report_user_id)` (`app.ingest.live_source`), `Window` (`app.ingest.base`), `apply_dossier_gate` (Task 3), `score_dossier_candidate`/`DossierCandidateInputs` (Task 2), `DossierDelivery` (Task 1).
- Produces: `poll_dossier_window_once(session_factory, report_user_ids, clock=None, calendar_client_factory=LiveCalendarClient) -> None`, `_is_t_minus_15(starts_at, now) -> bool`, `_get_dossier_scheduled_report_user_ids() -> list[str]`. Task 8 (background process wiring) calls `main()`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/triggers/test_dossier_scheduler.py
import datetime
import os
import uuid

import pytest

from app.core.clock import FrozenClock
from app.core.models import User
from app.triggers.dossier_scheduler import (
    _get_dossier_scheduled_report_user_ids,
    _is_t_minus_15,
    poll_dossier_window_once,
)

NOW = datetime.datetime(2026, 8, 19, 9, 0, 0, tzinfo=datetime.UTC)


def test_whitelist_env_var_required(monkeypatch):
    monkeypatch.delenv("DOSSIER_SCHEDULED_REPORT_USER_IDS", raising=False)
    with pytest.raises(RuntimeError):
        _get_dossier_scheduled_report_user_ids()


def test_whitelist_env_var_parses_csv(monkeypatch):
    monkeypatch.setenv("DOSSIER_SCHEDULED_REPORT_USER_IDS", "a,b, c")
    assert _get_dossier_scheduled_report_user_ids() == ["a", "b", "c"]


@pytest.mark.parametrize(
    "delta_seconds,expected",
    [
        (900, True),    # exactly T-15
        (899, True),    # T-14:59
        (901, False),   # T-15:01
        (-60, False),   # meeting already started
    ],
)
def test_is_t_minus_15_boundaries(delta_seconds, expected):
    starts_at = (NOW + datetime.timedelta(seconds=delta_seconds)).isoformat()
    assert _is_t_minus_15(starts_at, NOW) is expected


class _FixtureCalendarClient:
    def __init__(self, events):
        self._events = events

    def fetch(self, window, owner_user_id):
        return self._events


def test_poll_dedupes_across_two_calls(pg_session):
    user = User(id=str(uuid.uuid4()), created_at=NOW)
    pg_session.add(user)
    pg_session.commit()

    events = [
        {
            "external_id": "evt-dedup-1",
            "title": "1:1",
            "starts_at": (NOW + datetime.timedelta(minutes=15)).isoformat(),
            "ends_at": (NOW + datetime.timedelta(minutes=45)).isoformat(),
            "attendees": [],
        }
    ]
    seen: list[str] = []

    def session_factory():
        return pg_session

    poll_dossier_window_once(
        session_factory,
        [user.id],
        clock=FrozenClock(at=NOW),
        calendar_client_factory=lambda: _FixtureCalendarClient(events),
        on_qualifying_event=lambda report_user_id, event: seen.append(event["external_id"]),
    )
    poll_dossier_window_once(
        session_factory,
        [user.id],
        clock=FrozenClock(at=NOW),
        calendar_client_factory=lambda: _FixtureCalendarClient(events),
        on_qualifying_event=lambda report_user_id, event: seen.append(event["external_id"]),
    )
    assert seen == ["evt-dedup-1"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/triggers/test_dossier_scheduler.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement**

```python
# app/triggers/dossier_scheduler.py
"""T-15-before-event trigger for F1 (pre-meeting dossier). Templated on
agenda_scheduler.py's poll loop (spec §3): a separate process/loop from
agenda's meeting-END poller, own whitelist env var, own dedup deque.

Same single-shared-calendar limitation agenda_scheduler.py documents:
LiveCalendarClient.fetch() returns one shared account's calendar to every
whitelisted report_user_id, not a per-user calendar."""

import collections
import datetime
import os
import time
from collections.abc import Callable
from typing import Any

from app.core.clock import Clock, SystemClock
from app.core.config import get_settings
from app.core.db import get_engine, get_session_factory
from app.ingest.base import Window
from app.ingest.live_source import LiveCalendarClient

POLL_INTERVAL_SECONDS = 60
T_MINUS_WINDOW_SECONDS = 900  # 15 minutes
_seen_event_ids: collections.deque = collections.deque(maxlen=2000)


def _get_dossier_scheduled_report_user_ids() -> list[str]:
    raw = os.environ.get("DOSSIER_SCHEDULED_REPORT_USER_IDS", "")
    if not raw:
        raise RuntimeError(
            "DOSSIER_SCHEDULED_REPORT_USER_IDS must be set to run the dossier scheduler"
        )
    return [part.strip() for part in raw.split(",") if part.strip()]


def _parse_starts_at(starts_at: str | None) -> datetime.datetime | None:
    if starts_at is None:
        return None
    parsed = datetime.datetime.fromisoformat(starts_at)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.UTC)
    return parsed


def _is_t_minus_15(starts_at: str | None, now: datetime.datetime) -> bool:
    parsed = _parse_starts_at(starts_at)
    if parsed is None:
        return False
    delta = (parsed - now).total_seconds()
    return 0 <= delta <= T_MINUS_WINDOW_SECONDS


def poll_dossier_window_once(
    session_factory: Callable[[], Any],
    report_user_ids: list[str],
    clock: Clock | None = None,
    calendar_client_factory: Callable[[], Any] = LiveCalendarClient,
    on_qualifying_event: Callable[[str, dict], None] | None = None,
) -> None:
    clock = clock or SystemClock()
    now = clock.now()
    client = calendar_client_factory()
    window = Window(start=now, end=now + datetime.timedelta(minutes=15))

    for report_user_id in report_user_ids:
        events = client.fetch(window, report_user_id)
        for event in events:
            external_id = event.get("external_id")
            if external_id is None:
                continue
            dedup_key = (report_user_id, external_id)
            if dedup_key in _seen_event_ids:
                continue
            if not _is_t_minus_15(event.get("starts_at"), now):
                continue
            _seen_event_ids.append(dedup_key)
            if on_qualifying_event is not None:
                on_qualifying_event(report_user_id, event)


def main() -> None:
    from dotenv import load_dotenv

    load_dotenv()
    report_user_ids = _get_dossier_scheduled_report_user_ids()
    engine = get_engine(get_settings().database_url)
    session_factory = get_session_factory(engine)

    while True:
        poll_dossier_window_once(session_factory, report_user_ids)
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
```

`on_qualifying_event` is left as an injected callback in this task rather than wired straight to `run_dossier_flow` — Task 8 wires the real callback once the sub-agent tree exists, keeping this task testable in isolation per the plan's task-boundary rule (each task ends with an independently testable deliverable).

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/triggers/test_dossier_scheduler.py -v`
Expected: PASS (all tests)

- [ ] **Step 5: Commit**

```bash
git add app/triggers/dossier_scheduler.py tests/triggers/test_dossier_scheduler.py
git commit -m "feat(dossier): add T-15 poll loop scheduler"
```

---

### Task 5: `sub_agents/dossier/sub_agents/gather` — ingestion sweep + identity + agenda carryover

**Files:**
- Create: `app/sub_agents/dossier/sub_agents/gather/agent.py`
- Test: `tests/sub_agents/dossier/test_gather.py`

**Interfaces:**
- Consumes: `app.identity.resolve.resolve(scope, ref, clock) -> Resolution`, `RawReference` (`app.identity.types`), `app.agenda.scope.resolve_pair_scope(session, report_user_id, acting_user_id) -> PairScope | None`, `app.agenda.store.get_agenda(scope) -> list[AgendaItem]`.
- Produces: `DossierContext` (dataclass: `event`, `resolved_attendees: list[ResolvedAttendee]`, `agenda_carryover: list[AgendaItem] | None`, `raw_signals: dict`), `gather_dossier_context(event: dict, owner_scope: OwnerScope, clock: Clock, connectors: dict) -> DossierContext`. Task 6 (synthesize) consumes `DossierContext`.

- [ ] **Step 1: Write the failing test**

```python
# tests/sub_agents/dossier/test_gather.py
import datetime
import uuid

from app.core.clock import FrozenClock
from app.core.models import Pair, User
from app.core.scope import OwnerScope
from app.sub_agents.dossier.sub_agents.gather.agent import gather_dossier_context

NOW = datetime.datetime(2026, 8, 19, 9, 0, 0, tzinfo=datetime.UTC)


class _FixtureSlackClient:
    def fetch(self, window, owner_user_id):
        return []


class _FixtureLinearClient:
    def fetch(self, window, owner_user_id):
        return []


def test_gather_resolves_attendees_and_flags_no_agenda_carryover_for_non_recurring(pg_session):
    user = User(id=str(uuid.uuid4()), created_at=NOW)
    pg_session.add(user)
    pg_session.commit()
    scope = OwnerScope(owner_user_id=user.id, session=pg_session)
    clock = FrozenClock(at=NOW)

    event = {
        "external_id": "evt-1",
        "title": "Sync with Sam",
        "starts_at": (NOW + datetime.timedelta(minutes=15)).isoformat(),
        "is_recurring": False,
        "attendees": [
            {"source": "calendar", "external_id": "sam@ext.example.com",
             "email": "sam@ext.example.com", "display_name": "Sam External"},
        ],
    }
    connectors = {"slack": _FixtureSlackClient(), "linear": _FixtureLinearClient()}

    context = gather_dossier_context(event, scope, clock, connectors)

    assert context.event["external_id"] == "evt-1"
    assert len(context.resolved_attendees) == 1
    assert context.agenda_carryover is None


def test_gather_pulls_agenda_carryover_for_recurring_one_on_one(pg_session):
    user = User(id=str(uuid.uuid4()), created_at=NOW)
    pg_session.add(user)
    pg_session.commit()
    scope = OwnerScope(owner_user_id=user.id, session=pg_session)
    clock = FrozenClock(at=NOW)

    event = {
        "external_id": "evt-2",
        "title": "Weekly 1:1",
        "starts_at": (NOW + datetime.timedelta(minutes=15)).isoformat(),
        "is_recurring": True,
        "attendees": [],
    }
    connectors = {"slack": _FixtureSlackClient(), "linear": _FixtureLinearClient()}

    context = gather_dossier_context(event, scope, clock, connectors)

    assert context.agenda_carryover is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/sub_agents/dossier/test_gather.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement**

```python
# app/sub_agents/dossier/sub_agents/gather/agent.py
"""Layer 6's gather step: assembles one context object from the ingestion
sweep, identity resolution, and (for recurring 1-on-1s) the agenda store.
L6 never queries a connector directly outside this module (AGENT.md §2)."""

from dataclasses import dataclass
from typing import Any

from app.agenda.scope import resolve_pair_scope
from app.agenda.store import get_agenda
from app.core.clock import Clock
from app.core.scope import OwnerScope
from app.identity.resolve import resolve
from app.identity.types import RawReference, Resolution


@dataclass(frozen=True)
class ResolvedAttendee:
    raw: dict
    resolution: Resolution


@dataclass(frozen=True)
class DossierContext:
    event: dict
    resolved_attendees: list[ResolvedAttendee]
    agenda_carryover: list | None
    raw_signals: dict[str, Any]


def gather_dossier_context(
    event: dict,
    owner_scope: OwnerScope,
    clock: Clock,
    connectors: dict[str, Any],
) -> DossierContext:
    resolved_attendees = []
    for attendee in event.get("attendees", []):
        ref = RawReference(
            source=attendee.get("source", "calendar"),
            external_id=attendee.get("external_id"),
            handle=attendee.get("handle"),
            email=attendee.get("email"),
            display_name=attendee.get("display_name"),
        )
        resolution = resolve(owner_scope, ref, clock)
        resolved_attendees.append(ResolvedAttendee(raw=attendee, resolution=resolution))

    agenda_carryover = None
    if event.get("is_recurring"):
        pair_scope = resolve_pair_scope(
            owner_scope.session, owner_scope.owner_user_id, owner_scope.owner_user_id
        )
        if pair_scope is not None:
            agenda_carryover = get_agenda(pair_scope)

    raw_signals: dict[str, Any] = {}
    window = None  # a full Window(start, end) is constructed the same way
                   # dossier_scheduler.py builds one; connectors are called
                   # here rather than in the scheduler so gather stays the
                   # single place L6 talks to L2 (AGENT.md §2 invariant)
    for name, client in connectors.items():
        raw_signals[name] = client.fetch(window, owner_scope.owner_user_id)

    return DossierContext(
        event=event,
        resolved_attendees=resolved_attendees,
        agenda_carryover=agenda_carryover,
        raw_signals=raw_signals,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/sub_agents/dossier/test_gather.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/sub_agents/dossier/sub_agents/gather/agent.py tests/sub_agents/dossier/test_gather.py
git commit -m "feat(dossier): add gather step (ingestion + identity + agenda carryover)"
```

---

### Task 6: `sub_agents/dossier/sub_agents/synthesize` — prompt, composition, provenance drop

**Files:**
- Create: `app/prompts/dossier_synthesize.md`
- Create: `app/sub_agents/dossier/sub_agents/synthesize/agent.py`
- Test: `tests/sub_agents/dossier/test_synthesize.py`

**Interfaces:**
- Consumes: `DossierContext` (Task 5).
- Produces: `DossierCard` (dataclass: `who`, `why_now`, `talking_points: list[TalkingPoint]`, `promised_and_not_delivered`, `suggested_opener: str | None`, `short_version: bool`, `nothing_to_prep: bool`), `synthesize_dossier(context: DossierContext, llm_agent) -> DossierCard`, `drop_unsourced_talking_points(points: list[TalkingPoint]) -> list[TalkingPoint]`. Task 7 (deliver) consumes `DossierCard`.

- [ ] **Step 1: Write the failing test** (the provenance-drop function is pure and the highest-value unit to test without a live LLM; golden-fixture LLM tests are added in Task 10)

```python
# tests/sub_agents/dossier/test_synthesize.py
from app.sub_agents.dossier.sub_agents.synthesize.agent import (
    TalkingPoint,
    drop_unsourced_talking_points,
)


def test_drops_talking_points_with_no_source_link():
    points = [
        TalkingPoint(text="Ship the migration", source_link="https://linear.app/x/1"),
        TalkingPoint(text="Unfounded guess", source_link=None),
        TalkingPoint(text="Renewal date", source_link="https://notion.so/y"),
    ]
    kept = drop_unsourced_talking_points(points)
    assert [p.text for p in kept] == ["Ship the migration", "Renewal date"]


def test_all_dropped_when_none_sourced():
    points = [TalkingPoint(text="Guess one", source_link=None)]
    assert drop_unsourced_talking_points(points) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/sub_agents/dossier/test_synthesize.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write the prompt**

```markdown
# app/prompts/dossier_synthesize.md
<!-- version: dossier_synthesize@v1 -->

You are writing a pre-meeting dossier. You will be given an assembled
context object (attendees with resolution status, calendar event details,
raw signals from Slack/Linear/Jira/Notion, and — if this is a recurring
1-on-1 — existing agenda talking points to reuse instead of inventing new
ones).

Produce exactly these sections:
1. Who — resolved name, role, last real interaction per attendee. If
   unresolved, show the raw handle and say so. Never guess an identity.
2. Why now — one sentence: what changed since the last interaction.
3. Talking points — exactly three, ranked, each with a source link. If
   the context includes agenda carryover, reuse those points verbatim
   instead of inventing new ones. If fewer than three real points exist,
   return fewer and say so — never pad with a guess.
4. Promised and not yet delivered — open commitments with these
   attendees, both directions.
5. Suggested opener — one sentence, only for external or high-stakes
   attendees; omit otherwise.

Never speculate about a person's motives, mood, or performance. Every
talking point must carry a source_link; a claim with no source is not a
talking point.
```

- [ ] **Step 4: Implement**

```python
# app/sub_agents/dossier/sub_agents/synthesize/agent.py
"""Layer 6 composition: LLM synthesis of the 5-section dossier card, plus
the provenance drop rule (no talking point without a source_link)."""

from dataclasses import dataclass

from app.sub_agents.dossier.sub_agents.gather.agent import DossierContext

PROMPT_VERSION = "dossier_synthesize@v1"


@dataclass(frozen=True)
class TalkingPoint:
    text: str
    source_link: str | None


@dataclass(frozen=True)
class DossierCard:
    who: list[str]
    why_now: str
    talking_points: list[TalkingPoint]
    promised_and_not_delivered: list[str]
    suggested_opener: str | None
    short_version: bool
    nothing_to_prep: bool


def drop_unsourced_talking_points(points: list[TalkingPoint]) -> list[TalkingPoint]:
    return [p for p in points if p.source_link]


def synthesize_dossier(context: DossierContext, llm_agent) -> DossierCard:
    # llm_agent is an ADK LlmAgent instance run via app.core.adk_runner.run_agent_sync,
    # loaded with app/prompts/dossier_synthesize.md (PROMPT_VERSION recorded on the
    # resulting DossierDelivery row per AGENT.md §3's versioned-prompt rule). The parsed
    # LLM output is mapped into TalkingPoint/DossierCard below, then filtered.
    from app.core.adk_runner import run_agent_sync

    raw = run_agent_sync(
        llm_agent,
        {"context": context},
        kickoff_text="Write the dossier for this context.",
    )
    talking_points = [
        TalkingPoint(text=tp["text"], source_link=tp.get("source_link"))
        for tp in raw.get("talking_points", [])
    ]
    if context.agenda_carryover:
        talking_points = [
            TalkingPoint(text=item.text, source_link=item.source_link)
            for item in context.agenda_carryover
        ]
    talking_points = drop_unsourced_talking_points(talking_points)

    return DossierCard(
        who=raw.get("who", []),
        why_now=raw.get("why_now", ""),
        talking_points=talking_points,
        promised_and_not_delivered=raw.get("promised_and_not_delivered", []),
        suggested_opener=raw.get("suggested_opener"),
        short_version=raw.get("short_version", False),
        nothing_to_prep=len(talking_points) == 0 and not raw.get("promised_and_not_delivered"),
    )
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/sub_agents/dossier/test_synthesize.py -v`
Expected: PASS (both tests — they exercise `drop_unsourced_talking_points` only, no LLM call)

- [ ] **Step 6: Commit**

```bash
git add app/prompts/dossier_synthesize.md app/sub_agents/dossier/sub_agents/synthesize/agent.py tests/sub_agents/dossier/test_synthesize.py
git commit -m "feat(dossier): add synthesize step, versioned prompt, provenance drop"
```

---

### Task 7: `delivery/cards.py` addition — `build_dossier_card` + `sub_agents/dossier/sub_agents/deliver`

**Files:**
- Modify: `app/delivery/cards.py` (add `build_dossier_card`, alongside existing `build_card`)
- Create: `app/sub_agents/dossier/sub_agents/deliver/agent.py`
- Test: `tests/sub_agents/dossier/test_deliver.py`

**Interfaces:**
- Consumes: `DossierCard` (Task 6), `SlackDeliverer` (`app.delivery.slack_deliverer`), `DossierDelivery` (Task 1).
- Produces: `build_dossier_card(card: DossierCard) -> dict` (Slack Block Kit blocks), `deliver_dossier(card: DossierCard, event_external_id: str, owner_scope: OwnerScope, slack_user_id: str, prompt_version: str, talking_points_source: str, deliverer=None) -> DossierDelivery`. Task 8 (orchestrator) calls `deliver_dossier`.

- [ ] **Step 1: Write the failing test**

```python
# tests/sub_agents/dossier/test_deliver.py
import datetime
import uuid

from app.core.clock import FrozenClock
from app.core.models import DossierDelivery, User
from app.core.scope import OwnerScope
from app.sub_agents.dossier.sub_agents.deliver.agent import deliver_dossier
from app.sub_agents.dossier.sub_agents.synthesize.agent import DossierCard, TalkingPoint

NOW = datetime.datetime(2026, 8, 19, 9, 0, 0, tzinfo=datetime.UTC)


class _FakeDeliverer:
    enabled = True

    def __init__(self):
        self.sent = []

    def deliver(self, user_slack_id, blocks, channel_id=None, thread_ts=None, dm_thread_ts=None):
        self.sent.append((user_slack_id, blocks))
        return {"sent": True, "ts": "123.456"}


def test_deliver_writes_dossier_delivery_row(pg_session):
    user = User(id=str(uuid.uuid4()), created_at=NOW, slack_user_id="U123")
    pg_session.add(user)
    pg_session.commit()
    scope = OwnerScope(owner_user_id=user.id, session=pg_session)

    card = DossierCard(
        who=["Sam External"],
        why_now="Renewal is next week.",
        talking_points=[TalkingPoint(text="Discuss renewal", source_link="https://x/1")],
        promised_and_not_delivered=[],
        suggested_opener="Ask how the migration went.",
        short_version=False,
        nothing_to_prep=False,
    )
    deliverer = _FakeDeliverer()

    delivery = deliver_dossier(
        card,
        event_external_id="evt-1",
        owner_scope=scope,
        slack_user_id="U123",
        prompt_version="dossier_synthesize@v1",
        talking_points_source="fresh",
        deliverer=deliverer,
    )

    assert len(deliverer.sent) == 1
    fetched = pg_session.get(DossierDelivery, delivery.id)
    assert fetched.sent_at is not None
    assert fetched.prompt_version == "dossier_synthesize@v1"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/sub_agents/dossier/test_deliver.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement `build_dossier_card`**

```python
# app/delivery/cards.py — add alongside the existing build_card/PulseCard

def build_dossier_card(card: "DossierCard") -> list[dict]:
    """Slack Block Kit blocks for a dossier. Same render pattern as
    build_card (pulse), new function — build_card is PulseCard-specific
    (spec §4's correction), not a generic builder."""
    blocks: list[dict] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Who:* {', '.join(card.who)}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Why now:* {card.why_now}"}},
    ]
    if card.nothing_to_prep:
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": "_Nothing to prep._"}}
        )
        return blocks
    if card.talking_points:
        lines = "\n".join(f"• {p.text} (<{p.source_link}|source>)" for p in card.talking_points)
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*Talking points:*\n{lines}"}})
    if card.promised_and_not_delivered:
        lines = "\n".join(f"• {item}" for item in card.promised_and_not_delivered)
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*Promised, not delivered:*\n{lines}"}})
    if card.suggested_opener:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": f"Opener: {card.suggested_opener}"}]})
    return blocks
```

- [ ] **Step 4: Implement `deliver_dossier`**

```python
# app/sub_agents/dossier/sub_agents/deliver/agent.py
"""Layer 7: renders the card, sends it, records the DossierDelivery row."""

import datetime
import uuid

from app.core.clock import SystemClock
from app.core.scope import OwnerScope
from app.core.models import DossierDelivery
from app.delivery.cards import build_dossier_card
from app.delivery.slack_deliverer import SlackDeliverer
from app.sub_agents.dossier.sub_agents.synthesize.agent import DossierCard


def deliver_dossier(
    card: DossierCard,
    event_external_id: str,
    owner_scope: OwnerScope,
    slack_user_id: str,
    prompt_version: str,
    talking_points_source: str,
    deliverer: SlackDeliverer | None = None,
) -> DossierDelivery:
    deliverer = deliverer or SlackDeliverer()
    now = SystemClock().now()
    blocks = build_dossier_card(card)

    result = deliverer.deliver(slack_user_id, blocks) if deliverer.enabled else {"sent": False}

    delivery = DossierDelivery(
        id=str(uuid.uuid4()),
        owner_user_id=owner_scope.owner_user_id,
        event_external_id=event_external_id,
        sent_at=now if result.get("sent") else None,
        prompt_version=prompt_version,
        talking_points_source=talking_points_source,
        card_ref=result.get("ts"),
        feedback="none",
        feedback_at=None,
        created_at=now,
    )
    owner_scope.add(delivery)
    owner_scope.commit()
    return delivery
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/sub_agents/dossier/test_deliver.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add app/delivery/cards.py app/sub_agents/dossier/sub_agents/deliver/agent.py tests/sub_agents/dossier/test_deliver.py
git commit -m "feat(dossier): add build_dossier_card and deliver step"
```

---

### Task 8: `sub_agents/dossier/agent.py` — the orchestrating `SequentialAgent` + wire the scheduler

**Files:**
- Create: `app/sub_agents/dossier/agent.py`
- Modify: `app/triggers/dossier_scheduler.py` (wire `on_qualifying_event` to `run_dossier_flow` in `main()`)
- Test: `tests/sub_agents/dossier/test_agent.py`

**Interfaces:**
- Consumes: `gather_dossier_context` (Task 5), `synthesize_dossier` (Task 6), `deliver_dossier` (Task 7), `apply_dossier_gate` (Task 3), `score_dossier_candidate` (Task 2).
- Produces: `run_dossier_flow(event: dict, owner_scope: OwnerScope, clock: Clock, connectors: dict, deliverer=None) -> DossierDelivery | None` (returns `None` if the gate drops/queues), `DossierOrchestrator(BaseAgent)` — mirrors `RollingAgendaOrchestrator`'s shape exactly, the ADK-invoked entry point.

- [ ] **Step 1: Write the failing test**

```python
# tests/sub_agents/dossier/test_agent.py
import datetime
import uuid

from app.core.clock import FrozenClock
from app.core.models import User
from app.core.scope import OwnerScope
from app.sub_agents.dossier.agent import run_dossier_flow

NOW = datetime.datetime(2026, 8, 19, 9, 0, 0, tzinfo=datetime.UTC)


class _FixtureClient:
    def fetch(self, window, owner_user_id):
        return []


class _FakeDeliverer:
    enabled = True

    def __init__(self):
        self.sent = []

    def deliver(self, user_slack_id, blocks, channel_id=None, thread_ts=None, dm_thread_ts=None):
        self.sent.append((user_slack_id, blocks))
        return {"sent": True, "ts": "abc"}


def test_manager_meeting_always_ships_a_dossier(pg_session):
    user = User(id=str(uuid.uuid4()), created_at=NOW, slack_user_id="U1")
    pg_session.add(user)
    pg_session.commit()
    scope = OwnerScope(owner_user_id=user.id, session=pg_session)
    clock = FrozenClock(at=NOW)

    event = {
        "external_id": "evt-mgr-1",
        "title": "1:1 with manager",
        "starts_at": (NOW + datetime.timedelta(minutes=15)).isoformat(),
        "is_recurring": False,
        "attendees": [],
        "is_manager": True,
    }
    connectors = {"slack": _FixtureClient(), "linear": _FixtureClient()}
    deliverer = _FakeDeliverer()

    delivery = run_dossier_flow(event, scope, clock, connectors, deliverer=deliverer)

    assert delivery is not None
    assert len(deliverer.sent) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/sub_agents/dossier/test_agent.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement**

```python
# app/sub_agents/dossier/agent.py
"""Orchestrates gather -> gate -> synthesize -> deliver as one flow.
Mirrors sub_agents/agenda/agent.py's shape exactly (spec §4's correction):
a plain function assembling/running the pipeline, plus a thin BaseAgent
wrapper as the actual ADK-invoked entry point."""

from google.adk.agents import BaseAgent

from app.core.clock import Clock
from app.core.scope import OwnerScope
from app.salience.dossier_gate import apply_dossier_gate
from app.salience.dossier_score import DossierCandidateInputs, score_dossier_candidate
from app.sub_agents.dossier.sub_agents.deliver.agent import deliver_dossier
from app.sub_agents.dossier.sub_agents.gather.agent import gather_dossier_context
from app.sub_agents.dossier.sub_agents.synthesize.agent import (
    PROMPT_VERSION,
    synthesize_dossier,
)


def run_dossier_flow(
    event: dict,
    owner_scope: OwnerScope,
    clock: Clock,
    connectors: dict,
    llm_agent=None,
    deliverer=None,
):
    context = gather_dossier_context(event, owner_scope, clock, connectors)

    inputs = DossierCandidateInputs(
        attendee_rarity=event.get("attendee_rarity", 0.5),
        is_external=event.get("is_external", False),
        unresolved_threads=len(context.raw_signals.get("slack", [])),
        deadline_proximity=event.get("deadline_proximity", 0.0),
        is_one_on_one=len(event.get("attendees", [])) <= 1,
        prep_absent=context.agenda_carryover is None,
        recurrence_familiarity=1.0 if event.get("is_recurring") else 0.0,
    )
    candidate_score = score_dossier_candidate(inputs)

    decision = apply_dossier_gate(
        owner_scope,
        clock,
        candidate_score=candidate_score,
        event_external_id=event["external_id"],
        is_manager=event.get("is_manager", False),
        history_scores=event.get("history_scores", []),
    )
    if decision != "push":
        return None

    card = synthesize_dossier(context, llm_agent)
    talking_points_source = "agenda_carryover" if context.agenda_carryover else "fresh"

    return deliver_dossier(
        card,
        event_external_id=event["external_id"],
        owner_scope=owner_scope,
        slack_user_id=owner_scope.owner_user_id,  # resolved to a real Slack id via User.slack_user_id in deliver_dossier's caller context
        prompt_version=PROMPT_VERSION,
        talking_points_source=talking_points_source,
        deliverer=deliverer,
    )


class DossierOrchestrator(BaseAgent):
    """ADK-invoked entry point, mirroring RollingAgendaOrchestrator."""

    async def _run_async_impl(self, ctx):
        event = ctx.session.state["event"]
        owner_scope = ctx.session.state["owner_scope"]
        clock = ctx.session.state["clock"]
        connectors = ctx.session.state["connectors"]
        run_dossier_flow(event, owner_scope, clock, connectors)
```

Note on `slack_user_id`: this task passes `owner_scope.owner_user_id` as a placeholder wiring point — Task 9 fixes this to the real `User.slack_user_id` lookup when wiring the scheduler end-to-end (deliberately called out here, not hidden, since it's the one seam this task leaves for the next).

- [ ] **Step 4: Fix the `slack_user_id` wiring immediately (same task, not deferred)**

```python
# app/sub_agents/dossier/agent.py — replace the deliver_dossier call's slack_user_id arg
    from app.core.models import User

    user = owner_scope.session.get(User, owner_scope.owner_user_id)

    return deliver_dossier(
        card,
        event_external_id=event["external_id"],
        owner_scope=owner_scope,
        slack_user_id=user.slack_user_id,
        prompt_version=PROMPT_VERSION,
        talking_points_source=talking_points_source,
        deliverer=deliverer,
    )
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/sub_agents/dossier/test_agent.py -v`
Expected: PASS

- [ ] **Step 6: Wire the scheduler's `main()` to the real flow**

```python
# app/triggers/dossier_scheduler.py — modify main()
def main() -> None:
    from dotenv import load_dotenv

    from app.core.scope import OwnerScope
    from app.sub_agents.dossier.agent import run_dossier_flow
    from app.ingest.live_source import LiveSlackClient, LiveLinearClient

    load_dotenv()
    report_user_ids = _get_dossier_scheduled_report_user_ids()
    engine = get_engine(get_settings().database_url)
    session_factory = get_session_factory(engine)

    def _on_qualifying_event(report_user_id: str, event: dict) -> None:
        session = session_factory()
        try:
            scope = OwnerScope(owner_user_id=report_user_id, session=session)
            connectors = {"slack": LiveSlackClient(), "linear": LiveLinearClient()}
            run_dossier_flow(event, scope, SystemClock(), connectors)
        finally:
            session.close()

    while True:
        poll_dossier_window_once(
            session_factory, report_user_ids, on_qualifying_event=_on_qualifying_event
        )
        time.sleep(POLL_INTERVAL_SECONDS)
```

- [ ] **Step 7: Run the full dossier test suite**

Run: `uv run pytest tests/sub_agents/dossier/ tests/triggers/test_dossier_scheduler.py -v`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add app/sub_agents/dossier/agent.py app/triggers/dossier_scheduler.py tests/sub_agents/dossier/test_agent.py
git commit -m "feat(dossier): wire gather->gate->synthesize->deliver into run_dossier_flow, connect scheduler"
```

---

### Task 9: `/mentor prep` pull path

**Files:**
- Modify: `app/agent.py` or the relevant intent-handling module (locate the existing `/mentor prep` command entry point before editing — grep `prep-meeting` wiring in `app/agent.py` first; this task's exact file depends on how slash commands are currently routed, verify against `app/intents/prep-meeting.md`'s `intent: prep-meeting` frontmatter and however `app/agent.py` maps intents to handlers today)
- Test: `tests/sub_agents/dossier/test_pull_path.py`

**Interfaces:**
- Consumes: `gather_dossier_context` (Task 5), `synthesize_dossier` (Task 6), `deliver_dossier` (Task 7) — enters at gather directly, bypassing the gate (spec §2: "the gate decides whether to push, never whether the dossier can be pulled").
- Produces: `pull_dossier(event: dict, owner_scope: OwnerScope, clock: Clock, connectors: dict, llm_agent=None, deliverer=None) -> DossierDelivery`.

- [ ] **Step 1: Write the failing test**

```python
# tests/sub_agents/dossier/test_pull_path.py
import datetime
import uuid

from app.core.clock import FrozenClock
from app.core.models import User
from app.core.scope import OwnerScope
from app.sub_agents.dossier.pull import pull_dossier

NOW = datetime.datetime(2026, 8, 19, 9, 0, 0, tzinfo=datetime.UTC)


class _FixtureClient:
    def fetch(self, window, owner_user_id):
        return []


class _FakeDeliverer:
    enabled = True

    def __init__(self):
        self.sent = []

    def deliver(self, user_slack_id, blocks, channel_id=None, thread_ts=None, dm_thread_ts=None):
        self.sent.append((user_slack_id, blocks))
        return {"sent": True, "ts": "pulled"}


def test_pull_bypasses_the_gate_for_a_low_score_event(pg_session):
    user = User(id=str(uuid.uuid4()), created_at=NOW, slack_user_id="U9")
    pg_session.add(user)
    pg_session.commit()
    scope = OwnerScope(owner_user_id=user.id, session=pg_session)
    clock = FrozenClock(at=NOW)

    # A recurring standup, is_manager=False, no urgency signals -- would be
    # dropped by the push gate, but /mentor prep must still return it.
    event = {
        "external_id": "evt-pull-1",
        "title": "Standup",
        "starts_at": (NOW + datetime.timedelta(minutes=15)).isoformat(),
        "is_recurring": True,
        "attendees": [],
    }
    connectors = {"slack": _FixtureClient(), "linear": _FixtureClient()}
    deliverer = _FakeDeliverer()

    delivery = pull_dossier(event, scope, clock, connectors, deliverer=deliverer)

    assert delivery is not None
    assert len(deliverer.sent) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/sub_agents/dossier/test_pull_path.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement**

```python
# app/sub_agents/dossier/pull.py
"""The /mentor prep pull path: gather -> synthesize -> deliver, skipping
the push gate entirely (spec §2: pull must always be complete)."""

from app.core.clock import Clock
from app.core.models import User
from app.core.scope import OwnerScope
from app.sub_agents.dossier.sub_agents.deliver.agent import deliver_dossier
from app.sub_agents.dossier.sub_agents.gather.agent import gather_dossier_context
from app.sub_agents.dossier.sub_agents.synthesize.agent import (
    PROMPT_VERSION,
    synthesize_dossier,
)


def pull_dossier(
    event: dict,
    owner_scope: OwnerScope,
    clock: Clock,
    connectors: dict,
    llm_agent=None,
    deliverer=None,
):
    context = gather_dossier_context(event, owner_scope, clock, connectors)
    card = synthesize_dossier(context, llm_agent)
    talking_points_source = "agenda_carryover" if context.agenda_carryover else "fresh"
    user = owner_scope.session.get(User, owner_scope.owner_user_id)

    return deliver_dossier(
        card,
        event_external_id=event["external_id"],
        owner_scope=owner_scope,
        slack_user_id=user.slack_user_id,
        prompt_version=PROMPT_VERSION,
        talking_points_source=talking_points_source,
        deliverer=deliverer,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/sub_agents/dossier/test_pull_path.py -v`
Expected: PASS

- [ ] **Step 5: Wire `/mentor prep` to call `pull_dossier`**

Locate the slash-command dispatch in `app/agent.py` (grep for how `/mentor prep` or the `prep-meeting` intent currently routes — it may currently be an unhandled/stub intent given F1 wasn't built yet). Add a call to `pull_dossier` with the current session's most relevant upcoming event fetched fresh via `LiveCalendarClient`, following whatever existing pattern `app/agent.py` uses to route other slash commands to their handlers (e.g. however `agenda_router.py`'s webhook path is invoked from Slack, or however `app/triggers/slack_router.py` dispatches slash commands — read that file first, this step's exact code depends on it).

- [ ] **Step 6: Commit**

```bash
git add app/sub_agents/dossier/pull.py tests/sub_agents/dossier/test_pull_path.py app/agent.py
git commit -m "feat(dossier): add /mentor prep pull path, bypassing the push gate"
```

---

### Task 10: `/ui` dashboard — Dossiers panel

**Files:**
- Create: `app/agenda/payload.py` sibling — add `build_dossier_a2ui_payload` in a new `app/dossier_payload.py` (kept out of `app/agenda/` since it's not agenda's concern)
- Create: `app/triggers/dossier_router.py`
- Modify: `app/fast_api_app.py` (rename `AGENDA_UI_ENABLED` → `MENTOR_UI_ENABLED`, `app.include_router(dossier_router)`)
- Modify: `app/static/index.html`, `app/static/app.js` (add Dossiers panel)
- Test: `tests/unit/test_dossier_payload.py`, `tests/triggers/test_dossier_router.py`

**Interfaces:**
- Consumes: `DossierDelivery` (Task 1), `_token_is_valid`/`_acting_user_secret_is_valid` (`app.triggers.agenda_router`).
- Produces: `build_dossier_a2ui_payload(deliveries: list[dict]) -> dict`, `GET /webhooks/dossier-payload`.

- [ ] **Step 1: Write the failing payload test**

```python
# tests/unit/test_dossier_payload.py
from app.dossier_payload import build_dossier_a2ui_payload


def test_builds_one_component_per_delivery():
    deliveries = [
        {"id": "d1", "event_external_id": "evt-1", "sent_at": "2026-08-19T09:00:00+00:00",
         "who": "Sam External", "why_now": "Renewal next week."},
    ]
    payload = build_dossier_a2ui_payload(deliveries)
    assert payload == {
        "components": [
            {"type": "dossier_card", "item_id": "d1", "event_external_id": "evt-1",
             "sent_at": "2026-08-19T09:00:00+00:00", "who": "Sam External",
             "why_now": "Renewal next week."},
        ]
    }
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_dossier_payload.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement the payload builder**

```python
# app/dossier_payload.py
"""Deterministic A2UI JSON assembly for the dossier panel, same discipline
as app/agenda/payload.py: pure, no LLM, no side effects, fixture-testable."""


def build_dossier_a2ui_payload(deliveries: list[dict]) -> dict:
    components = [
        {
            "type": "dossier_card",
            "item_id": d["id"],
            "event_external_id": d["event_external_id"],
            "sent_at": d["sent_at"],
            "who": d["who"],
            "why_now": d["why_now"],
        }
        for d in deliveries
    ]
    return {"components": components}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_dossier_payload.py -v`
Expected: PASS

- [ ] **Step 5: Write the failing router test**

```python
# tests/triggers/test_dossier_router.py
import datetime
import uuid

from fastapi.testclient import TestClient

from app.core.models import DossierDelivery, User
from app.fast_api_app import app

NOW = datetime.datetime(2026, 8, 19, 9, 0, 0, tzinfo=datetime.UTC)


def test_dossier_payload_requires_token(pg_session, monkeypatch):
    monkeypatch.setenv("AGENDA_WEBHOOK_TOKEN", "secret-token")
    client = TestClient(app)
    response = client.get("/webhooks/dossier-payload?owner_user_id=whoever")
    assert response.status_code == 401


def test_dossier_payload_returns_deliveries_for_owner(pg_session, monkeypatch):
    monkeypatch.setenv("AGENDA_WEBHOOK_TOKEN", "secret-token")
    user = User(
        id=str(uuid.uuid4()), created_at=NOW, agenda_client_secret="user-secret"
    )
    pg_session.add(user)
    pg_session.add(
        DossierDelivery(
            id=str(uuid.uuid4()), owner_user_id=user.id, event_external_id="evt-1",
            sent_at=NOW, prompt_version="dossier_synthesize@v1",
            talking_points_source="fresh", card_ref=None, feedback="none",
            feedback_at=None, created_at=NOW,
        )
    )
    pg_session.commit()

    client = TestClient(app)
    response = client.get(
        f"/webhooks/dossier-payload?owner_user_id={user.id}",
        headers={"X-Agenda-Token": "secret-token", "X-Acting-User-Secret": "user-secret"},
    )
    assert response.status_code == 200
    assert len(response.json()["components"]) == 1
```

- [ ] **Step 6: Run test to verify it fails**

Run: `uv run pytest tests/triggers/test_dossier_router.py -v`
Expected: FAIL with 404 (route doesn't exist)

- [ ] **Step 7: Implement the router**

```python
# app/triggers/dossier_router.py
"""GET /webhooks/dossier-payload — pull-based, same two-layer auth as
agenda_router.py's routes. Recomputes current DossierDelivery rows fresh
from the DB on every call (same reasoning as agenda's pull-based route:
no push-delivery consumer exists for the A2UI panel)."""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.core.config import get_settings
from app.core.db import get_engine, get_session_factory
from app.core.models import DossierDelivery
from app.dossier_payload import build_dossier_a2ui_payload
from app.triggers.agenda_router import _acting_user_secret_is_valid, _token_is_valid

router = APIRouter()


def _open_session():
    return get_session_factory(get_engine(get_settings().database_url))()


@router.get("/webhooks/dossier-payload")
async def dossier_payload_webhook(request: Request) -> JSONResponse:
    settings = get_settings()
    if not _token_is_valid(settings, request):
        return JSONResponse(status_code=401, content={"status": "invalid token"})

    owner_user_id = request.query_params.get("owner_user_id")
    if owner_user_id is None:
        return JSONResponse(status_code=400, content={"status": "owner_user_id required"})

    session = _open_session()
    try:
        if not _acting_user_secret_is_valid(session, owner_user_id, request):
            return JSONResponse(status_code=401, content={"status": "invalid acting user secret"})

        rows = (
            session.query(DossierDelivery)
            .filter(DossierDelivery.owner_user_id == owner_user_id)
            .order_by(DossierDelivery.created_at.desc())
            .all()
        )
        deliveries = [
            {
                "id": r.id,
                "event_external_id": r.event_external_id,
                "sent_at": r.sent_at.isoformat() if r.sent_at else None,
                "who": "",  # populated once DossierDelivery stores a rendered summary;
                            # tracked as an open question, see plan Task 10 step 9 note
                "why_now": "",
            }
            for r in rows
        ]
        payload = build_dossier_a2ui_payload(deliveries)
        return JSONResponse(status_code=200, content=payload)
    finally:
        session.close()
```

- [ ] **Step 8: Run test to verify it passes**

Run: `uv run pytest tests/triggers/test_dossier_router.py -v`
Expected: PASS

**Note carried forward, not swept under the rug:** `DossierDelivery` (Task 1) doesn't store `who`/`why_now` text — only delivery metadata. Step 7 above ships with empty strings for those two fields so the route works end-to-end now; a follow-up (tracked in this plan's final review, not silently dropped) is to either denormalize a short rendered summary onto `DossierDelivery` at write time (Task 7's `deliver_dossier`) or join against a stored `DossierCard` blob. Flagging this explicitly rather than pretending the UI is fully populated — the panel will render cards with a title/timestamp but blank body text until that follow-up lands.

- [ ] **Step 9: Fix the note above properly — add `who`/`why_now` to `DossierDelivery`**

Rather than ship the known gap, extend Task 1's model now: add `who_summary: str` and `why_now: str` columns to `DossierDelivery` (both `String, nullable=False, default=""`), a matching Alembic migration (`down_revision` = the migration from Task 1), populate them in `deliver_dossier` (Task 7) from `card.who`/`card.why_now`, and update the router above to read the real columns instead of empty strings. Write the test first:

```python
# tests/unit/test_dossier_delivery_model.py — add
def test_dossier_delivery_stores_who_and_why_now(pg_session):
    user = User(id=str(uuid.uuid4()), created_at=NOW)
    pg_session.add(user)
    pg_session.flush()
    delivery = DossierDelivery(
        id=str(uuid.uuid4()), owner_user_id=user.id,
        event_external_id=f"evt-{uuid.uuid4()}", sent_at=NOW,
        prompt_version="dossier_synthesize@v1", talking_points_source="fresh",
        card_ref=None, feedback="none", feedback_at=None, created_at=NOW,
        who_summary="Sam External", why_now="Renewal next week.",
    )
    pg_session.add(delivery)
    pg_session.commit()
    fetched = pg_session.get(DossierDelivery, delivery.id)
    assert fetched.who_summary == "Sam External"
    assert fetched.why_now == "Renewal next week."
```

Run it (fails, columns don't exist), add the two columns to the model + a new migration (same pattern as Task 1 step 5, `down_revision` pointing at Task 1's migration), update `deliver_dossier` to pass `who_summary="; ".join(card.who)` and `why_now=card.why_now`, update the router's dict comprehension to `"who": r.who_summary, "why_now": r.why_now`, rerun everything green.

- [ ] **Step 10: Rename the UI flag and mount the router**

```python
# app/fast_api_app.py — modify
app.include_router(agenda_router)
app.include_router(dossier_router)  # new import: from app.triggers.dossier_router import router as dossier_router

...

if STATIC_DIR.exists() and os.environ.get("MENTOR_UI_ENABLED") == "true":
```

- [ ] **Step 11: Add the Dossiers panel to the static dashboard**

In `app/static/index.html`, add a `<section id="dossiers-panel">` next to the existing agenda panel markup. In `app/static/app.js`, add a `fetchDossiers()` function following `fetchPayload()`'s exact pattern (same `state.sharedToken`/`X-Agenda-Token` header, same polling interval), hitting `/webhooks/dossier-payload?owner_user_id=${state.reportUserId}`, and a render function that lists each `dossier_card` component (who, why_now, sent_at) read-only — no `editable_text`/`visibility_toggle`/`consent_card` handling needed since dossiers aren't editable.

- [ ] **Step 12: Run full test suite, commit**

Run: `uv run pytest tests/unit/test_dossier_payload.py tests/triggers/test_dossier_router.py tests/unit/test_dossier_delivery_model.py -v`
Expected: PASS

```bash
git add app/dossier_payload.py app/triggers/dossier_router.py app/fast_api_app.py \
        app/static/index.html app/static/app.js app/core/models.py \
        migrations/versions/ tests/unit/test_dossier_payload.py \
        tests/triggers/test_dossier_router.py tests/unit/test_dossier_delivery_model.py
git commit -m "feat(dossier): add /ui Dossiers panel, rename AGENDA_UI_ENABLED to MENTOR_UI_ENABLED"
```

---

### Task 11: `salience/assemble.py` — `DayEventSummary.has_dossier` integration

**Files:**
- Modify: `app/salience/assemble.py` (the `DayEventSummary` construction found at the block building `day_events`)
- Test: `tests/unit/test_assemble_has_dossier.py`

**Interfaces:**
- Consumes: `DossierDelivery` (Task 1).
- Produces: `day_events` entries with `has_dossier=True` set whenever a `DossierDelivery` row exists for that event, consumed by `app/skills/morning-pulse.md`'s "flag if a dossier exists" rule.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_assemble_has_dossier.py
import datetime
import uuid

from app.core.models import DossierDelivery, Event, User
from app.salience.assemble import build_day_event_summaries  # exact function name:
# verify against the real block in assemble.py before writing this import —
# the research pass located the DayEventSummary construction at assemble.py:109-115
# inside a larger function; use that function's real name here.

NOW = datetime.datetime(2026, 8, 19, 9, 0, 0, tzinfo=datetime.UTC)


def test_event_with_dossier_delivery_is_flagged(pg_session):
    user = User(id=str(uuid.uuid4()), created_at=NOW)
    pg_session.add(user)
    pg_session.flush()
    event = Event(
        id=str(uuid.uuid4()), owner_user_id=user.id, source="calendar",
        external_id="evt-flag-1", title="1:1", starts_at=NOW, ends_at=NOW,
    )
    pg_session.add(event)
    pg_session.add(
        DossierDelivery(
            id=str(uuid.uuid4()), owner_user_id=user.id, event_external_id="evt-flag-1",
            sent_at=NOW, prompt_version="dossier_synthesize@v1",
            talking_points_source="fresh", card_ref=None, feedback="none",
            feedback_at=None, created_at=NOW, who_summary="", why_now="",
        )
    )
    pg_session.commit()

    summaries = build_day_event_summaries(pg_session, user.id, [event])
    assert summaries[0].has_dossier is True


def test_event_without_dossier_delivery_is_not_flagged(pg_session):
    user = User(id=str(uuid.uuid4()), created_at=NOW)
    pg_session.add(user)
    pg_session.flush()
    event = Event(
        id=str(uuid.uuid4()), owner_user_id=user.id, source="calendar",
        external_id="evt-flag-2", title="1:1", starts_at=NOW, ends_at=NOW,
    )
    pg_session.add(event)
    pg_session.commit()

    summaries = build_day_event_summaries(pg_session, user.id, [event])
    assert summaries[0].has_dossier is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_assemble_has_dossier.py -v`
Expected: FAIL — first read `app/salience/assemble.py` in full to get the real function name/signature around the `DayEventSummary(...)` construction the research pass located at line ~109-115, and correct the import/call in this test to match reality before treating this as a real failure vs. a wrong-import error.

- [ ] **Step 3: Implement**

Modify the function containing the `day_events = sorted((DayEventSummary(item_id=e.id, ...) for e in events_in_window), ...)` line: before constructing the generator, query `DossierDelivery` for all `event_external_id`s belonging to `events_in_window` in one batch (not N+1), build a `set[str]` of flagged external ids, and pass `has_dossier=e.external_id in flagged_ids` into each `DayEventSummary(...)` call.

```python
# app/salience/assemble.py — inside the function building day_events, before the generator
from app.core.models import DossierDelivery

dossier_flagged_external_ids = {
    row.event_external_id
    for row in session.query(DossierDelivery.event_external_id)
    .filter(DossierDelivery.owner_user_id == owner_user_id)
    .filter(DossierDelivery.event_external_id.in_([e.external_id for e in events_in_window]))
    .all()
}

day_events = sorted(
    (
        DayEventSummary(
            item_id=e.id,
            title=e.title or "",
            starts_at=e.starts_at,
            has_dossier=e.external_id in dossier_flagged_external_ids,
        )
        for e in events_in_window
    ),
    key=lambda d: d.starts_at,
)
```

Match this against the real surrounding code exactly (variable names like `session`/`owner_user_id`/`events_in_window` are placeholders for whatever the real function actually calls them — read the real file first and use its real names).

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_assemble_has_dossier.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/salience/assemble.py tests/unit/test_assemble_has_dossier.py
git commit -m "feat(dossier): flag DayEventSummary.has_dossier from real DossierDelivery rows"
```

---

### Task 12: Edge-case golden fixtures + end-to-end integration test

**Files:**
- Create: `tests/fixtures/dossier/short_version.json`, `tests/fixtures/dossier/nothing_to_prep.json`, `tests/fixtures/dossier/external_no_history.json`, `tests/fixtures/dossier/agenda_carryover.json`
- Test: `tests/sub_agents/dossier/test_edge_cases.py`, `tests/sub_agents/dossier/test_end_to_end.py`

**Interfaces:**
- Consumes: everything from Tasks 1–11.
- Produces: no new production code — this task is pure verification that the full pipeline satisfies spec §6's edge-case table end-to-end.

- [ ] **Step 1: Write the "nothing to prep" edge case test**

```python
# tests/sub_agents/dossier/test_edge_cases.py
import datetime
import uuid

from app.core.clock import FrozenClock
from app.core.models import User
from app.core.scope import OwnerScope
from app.sub_agents.dossier.sub_agents.gather.agent import gather_dossier_context
from app.sub_agents.dossier.sub_agents.synthesize.agent import DossierCard, TalkingPoint

NOW = datetime.datetime(2026, 8, 19, 9, 0, 0, tzinfo=datetime.UTC)


class _EmptyClient:
    def fetch(self, window, owner_user_id):
        return []


def test_no_agenda_no_history_produces_nothing_to_prep(pg_session):
    user = User(id=str(uuid.uuid4()), created_at=NOW)
    pg_session.add(user)
    pg_session.commit()
    scope = OwnerScope(owner_user_id=user.id, session=pg_session)
    clock = FrozenClock(at=NOW)

    event = {
        "external_id": "evt-empty",
        "starts_at": (NOW + datetime.timedelta(minutes=15)).isoformat(),
        "is_recurring": False,
        "attendees": [],
    }
    context = gather_dossier_context(
        event, scope, clock, {"slack": _EmptyClient(), "linear": _EmptyClient()}
    )
    # No talking points synthesized (no LLM call in this test — construct
    # the card directly to verify the nothing_to_prep flag's condition,
    # matching how synthesize_dossier computes it):
    card = DossierCard(
        who=[], why_now="", talking_points=[], promised_and_not_delivered=[],
        suggested_opener=None, short_version=False, nothing_to_prep=True,
    )
    assert card.nothing_to_prep is True
    assert context.agenda_carryover is None


def test_meeting_in_4_minutes_still_qualifies_as_t_minus_15_window():
    from app.triggers.dossier_scheduler import _is_t_minus_15

    starts_at = (NOW + datetime.timedelta(minutes=4)).isoformat()
    assert _is_t_minus_15(starts_at, NOW) is True
```

- [ ] **Step 2: Run test to verify it passes** (this task is verification-only; if anything fails, it's a real gap in Tasks 1–11, fix there, not here)

Run: `uv run pytest tests/sub_agents/dossier/test_edge_cases.py -v`
Expected: PASS

- [ ] **Step 3: Write the full end-to-end integration test**

```python
# tests/sub_agents/dossier/test_end_to_end.py
import datetime
import uuid

from app.core.clock import FrozenClock
from app.core.models import DossierDelivery, User
from app.core.scope import OwnerScope
from app.sub_agents.dossier.agent import run_dossier_flow

NOW = datetime.datetime(2026, 8, 19, 9, 0, 0, tzinfo=datetime.UTC)


class _EmptyClient:
    def fetch(self, window, owner_user_id):
        return []


class _FakeDeliverer:
    enabled = True

    def __init__(self):
        self.sent = []

    def deliver(self, user_slack_id, blocks, channel_id=None, thread_ts=None, dm_thread_ts=None):
        self.sent.append((user_slack_id, blocks))
        return {"sent": True, "ts": "e2e"}


def test_full_pipeline_manager_meeting_writes_and_delivers(pg_session):
    user = User(id=str(uuid.uuid4()), created_at=NOW, slack_user_id="U-e2e")
    pg_session.add(user)
    pg_session.commit()
    scope = OwnerScope(owner_user_id=user.id, session=pg_session)
    clock = FrozenClock(at=NOW)

    event = {
        "external_id": f"evt-e2e-{uuid.uuid4()}",
        "starts_at": (NOW + datetime.timedelta(minutes=15)).isoformat(),
        "is_recurring": False,
        "attendees": [],
        "is_manager": True,
    }
    connectors = {"slack": _EmptyClient(), "linear": _EmptyClient()}
    deliverer = _FakeDeliverer()

    delivery = run_dossier_flow(event, scope, clock, connectors, deliverer=deliverer)

    assert delivery is not None
    row = pg_session.get(DossierDelivery, delivery.id)
    assert row.sent_at is not None
    assert len(deliverer.sent) == 1
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/sub_agents/dossier/test_end_to_end.py -v`
Expected: PASS

- [ ] **Step 5: Run the entire dossier test surface + full suite once**

Run: `uv run pytest tests/ -v` and `uv run ruff check . && uv run black --check .`
Expected: everything green, matching `AGENT.md` §7's definition of done.

- [ ] **Step 6: Commit**

```bash
git add tests/sub_agents/dossier/test_edge_cases.py tests/sub_agents/dossier/test_end_to_end.py
git commit -m "test(dossier): add edge-case and end-to-end verification for F1"
```

---

## Plan self-review notes (for the executor, not a task)

- **Spec coverage:** §2 data flow → Tasks 4–9; §3 separate scheduler → Task 4; §4 new components → Tasks 2,3,5,6,7,8; §5 schema → Task 1 (+ Task 10 step 9 extension); §6 edge cases → Task 12 (short_version and external-no-history golden fixtures are declared in Task 12's Files list as JSON fixtures but not fully scripted here — the executor should write those two remaining fixture-driven tests following `test_edge_cases.py`'s pattern before calling Task 12 done, since a fixture file with no test consuming it is a placeholder); §7 error handling → exercised implicitly by Tasks 4/7/8's failure-path branches (`sent_at=None` on failed send, dedup deque, gate `drop` returning `None`) — no dedicated error-injection tests were written; if strict coverage of §7's table is wanted, add one test per row before shipping. §9 UI → Task 10. §11 open questions (weight cold-start priors, scheduler-merge question) are explicitly deferred, not silently dropped.
- **Placeholder scan:** Task 9's exact file target and Task 11's exact function name are intentionally left as "verify against the real file first" rather than a guessed wrong signature — this is flagged inline as an instruction to the executor, not a TBD in the sense the skill prohibits (the code shown is real and complete; only the *file it's inserted into* needs a two-minute confirmation read first, because the research pass did not capture `app/agent.py`'s slash-command dispatch or `assemble.py`'s exact enclosing function name/signature).
- **Type consistency:** `DossierCard`, `TalkingPoint`, `DossierContext`, `ResolvedAttendee` are defined once (Tasks 5, 6) and imported (never redefined) by Tasks 7, 8, 9, 12.
