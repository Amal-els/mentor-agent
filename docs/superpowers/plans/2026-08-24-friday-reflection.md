# Friday Reflection / Review (F4) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship F4 — a weekly Slack DM ritual (Friday afternoon, per-owner local time) that mirrors back what shipped, what moved an OKR, what slipped and why, one concrete adjustment, a skill-distribution read, agenda 1-on-1 resolution status, and a "Confirm & log" write-back that turns freshly-evidenced-but-not-yet-recorded wins into real `Accomplishment` rows only with explicit consent — plus a `/mentor review` on-demand pull path.

**Architecture:** A new, independent `friday_review_scheduler.py` polling loop (mirroring `cron_scheduler.py`/`dossier_scheduler.py`) fires per-owner at Friday-afternoon local time, a new `sub_agents/friday_review` tree (gather → synthesize → deliver, mirroring `sub_agents/dossier`'s real shape) assembles and sends the card, and a new `FridayReviewDelivery` table records what shipped and what was proposed for the ledger. No new connector, no new salience gate — `gather` does real aggregation directly against tables F1/F2/F3 already populate (`Commitment`, `WorkItem`, `Goal`, `AgendaItem`, `Accomplishment`, `PulseDelivery`), and a direct `Suppression` query stands in for a push/drop score. The "Confirm & log" write-back is new code, not a reuse of `app.agenda.store.append_ledger_item` — that function mirrors onto the shared, manager-visible agenda by construction, which is wrong for a ritual whose own privacy rule is "private, always" (design spec §2).

**Tech Stack:** Python 3.12, Google ADK (`Agent`/`BaseAgent`), SQLAlchemy 2.x + Alembic, pytest against a real Postgres test DB (no mocks/SQLite), this worktree's own `mentor_f4` database.

**Spec:** `mentor/docs/superpowers/specs/2026-08-24-friday-reflection-design.md`

## Global Constraints

- No LLM calls outside `synthesize` (`AGENT.md` §2). `gather` computes skill distribution counts and OKR progress directly from `Goal`/`Accomplishment` rows — never asks the LLM to eyeball or aggregate. The LLM's only jobs: phrase each win + map it to a goal, phrase exactly one adjustment, write the one-paragraph career narrative, and classify `skill_category` for any `Accomplishment`-shaped evidence that's missing one.
- Every table carries `owner_user_id`, non-nullable, indexed first.
- No win ships without a `source_reference_key` resolving back to a real DB row (provenance rule) — enforced in code (`drop_unsourced_wins`), never trusted from the LLM's own text.
- Never call `datetime.now()` outside `app/core/clock.py`; every test uses `FrozenClock`.
- Prompts are versioned files under `app/prompts/`; the version ID is recorded on every `FridayReviewDelivery` row.
- Tests run against the real Postgres test DB via the `pg_session` fixture (`tests/unit/conftest.py`) — never mock the DB, never use SQLite. Real test file locations mirror the codebase's actual current layout, `tests/unit/...`, not the `tests/sub_agents/...`/`tests/triggers/...` shape an earlier F1 planning draft used before its own tests landed.
- Follow the existing per-ritual duplication pattern: own scheduler, own delivery table, own card builder. Do not generalize across `cron_scheduler.py`/`agenda_scheduler.py`/`dossier_scheduler.py`/`friday_review_scheduler.py` in this pass.
- `owner_user_id`/`status`/`trigger`/`scope`-shaped columns are always plain `String` in this codebase (`PulseDelivery.trigger`, `Commitment.status`, `Suppression.scope`, `AgendaItem.source`), never a SQLAlchemy `Enum` type — matched here for `FridayReviewDelivery.trigger`/`.skipped_reason` and `Accomplishment.skill_category`, resolving design spec §11's first open question.
- `FRIDAY_REVIEW_SCHEDULED_OWNER_IDS` is its own independent whitelist env var, not inherited from `PULSE_SCHEDULED_OWNER_IDS` — resolving design spec §11's third open question, matching every other ritual's own separate whitelist.
- A failed Slack send is not retried automatically within the same poll cycle in this pass (design spec §8/§11's documented gap, same posture F1's own design left its analogous gap in).
- Migrations: Alembic, current head is `7f3a9c1e6b02` — the first new migration's `down_revision` must be exactly that string.
- Environment for this worktree: `export PATH="$PATH:/c/Users/ameni/AppData/Local/Microsoft/WinGet/Packages/astral-sh.uv_Microsoft.Winget.Source_8wekyb3d8bbwe"` before any `uv run ...`, and a `.env` already pointing `DATABASE_URL` at `mentor_f4`.

---

### Task 1: Schema — `FridayReviewDelivery`, `User.friday_review_fire_time_local`, `Accomplishment.skill_category`

**Files:**
- Modify: `app/core/models.py` (add `FridayReviewDelivery` after `DossierDelivery`; add `friday_review_fire_time_local` to `User`, after `late_cutoff_local`)
- Modify: `app/agenda/models.py` (add `skill_category` to `Accomplishment`)
- Create: `migrations/versions/<new_rev>_friday_review_schema.py`
- Test: `tests/unit/test_friday_review_delivery_model.py`

**Interfaces:**
- Produces: `FridayReviewDelivery(id, owner_user_id, week_start_date, trigger, sent_at, skipped_reason, prompt_version, card_ref, proposed_ledger_items, ledger_confirmed_at, created_at)`, unique `(owner_user_id, week_start_date, trigger)`; `User.friday_review_fire_time_local: time` (default 16:00); `Accomplishment.skill_category: str | None`. Every later task reads or writes through these.

- [ ] **Step 1: Write the failing model tests**

```python
# tests/unit/test_friday_review_delivery_model.py
import datetime
import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from app.agenda.models import Accomplishment
from app.core.models import FridayReviewDelivery, User

NOW = datetime.datetime(2026, 8, 24, 16, 0, 0, tzinfo=datetime.UTC)
WEEK_START = datetime.date(2026, 8, 24)  # a Monday


def _make_user(pg_session) -> User:
    user = User(id=str(uuid.uuid4()), created_at=NOW)
    pg_session.add(user)
    pg_session.commit()
    return user


def test_friday_review_delivery_round_trip(pg_session):
    user = _make_user(pg_session)

    delivery = FridayReviewDelivery(
        id=str(uuid.uuid4()),
        owner_user_id=user.id,
        week_start_date=WEEK_START,
        trigger="scheduled",
        sent_at=NOW,
        skipped_reason=None,
        prompt_version="friday_review_synthesize@v1",
        card_ref="123.456",
        proposed_ledger_items=[
            {"description": "Shipped the migration", "source_reference_key": "wi-1"}
        ],
        ledger_confirmed_at=None,
        created_at=NOW,
    )
    pg_session.add(delivery)
    pg_session.commit()

    fetched = pg_session.get(FridayReviewDelivery, delivery.id)
    assert fetched.trigger == "scheduled"
    assert fetched.proposed_ledger_items[0]["source_reference_key"] == "wi-1"
    assert fetched.ledger_confirmed_at is None


def test_friday_review_delivery_unique_per_owner_week_trigger(pg_session):
    user = _make_user(pg_session)
    pg_session.add(
        FridayReviewDelivery(
            id=str(uuid.uuid4()),
            owner_user_id=user.id,
            week_start_date=WEEK_START,
            trigger="scheduled",
            sent_at=NOW,
            skipped_reason=None,
            prompt_version="friday_review_synthesize@v1",
            card_ref=None,
            proposed_ledger_items=[],
            ledger_confirmed_at=None,
            created_at=NOW,
        )
    )
    pg_session.commit()

    pg_session.add(
        FridayReviewDelivery(
            id=str(uuid.uuid4()),
            owner_user_id=user.id,
            week_start_date=WEEK_START,
            trigger="scheduled",
            sent_at=NOW,
            skipped_reason=None,
            prompt_version="friday_review_synthesize@v1",
            card_ref=None,
            proposed_ledger_items=[],
            ledger_confirmed_at=None,
            created_at=NOW,
        )
    )
    with pytest.raises(IntegrityError):
        pg_session.commit()


def test_pull_trigger_does_not_collide_with_scheduled_same_week(pg_session):
    pg_session.rollback()
    user = _make_user(pg_session)
    pg_session.add(
        FridayReviewDelivery(
            id=str(uuid.uuid4()), owner_user_id=user.id, week_start_date=WEEK_START,
            trigger="scheduled", sent_at=NOW, skipped_reason=None,
            prompt_version="friday_review_synthesize@v1", card_ref=None,
            proposed_ledger_items=[], ledger_confirmed_at=None, created_at=NOW,
        )
    )
    pg_session.add(
        FridayReviewDelivery(
            id=str(uuid.uuid4()), owner_user_id=user.id, week_start_date=WEEK_START,
            trigger="pull", sent_at=NOW, skipped_reason=None,
            prompt_version="friday_review_synthesize@v1", card_ref=None,
            proposed_ledger_items=[], ledger_confirmed_at=None, created_at=NOW,
        )
    )
    pg_session.commit()  # both rows coexist — different trigger, same week


def test_user_friday_review_fire_time_defaults_to_4pm(pg_session):
    user = User(id=str(uuid.uuid4()), created_at=NOW)
    pg_session.add(user)
    pg_session.commit()
    fetched = pg_session.get(User, user.id)
    assert fetched.friday_review_fire_time_local == datetime.time(16, 0)


def test_accomplishment_skill_category_nullable_and_settable(pg_session):
    user = _make_user(pg_session)
    row = Accomplishment(
        id=str(uuid.uuid4()), owner_user_id=user.id, description="Shipped X",
        source_reference_key="wi-1", occurred_at=NOW, skill_category=None,
    )
    pg_session.add(row)
    pg_session.commit()
    fetched = pg_session.get(Accomplishment, row.id)
    assert fetched.skill_category is None

    fetched.skill_category = "technical_execution"
    pg_session.commit()
    assert pg_session.get(Accomplishment, row.id).skill_category == "technical_execution"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_friday_review_delivery_model.py -v`
Expected: FAIL with `ImportError: cannot import name 'FridayReviewDelivery'`

- [ ] **Step 3: Add the models**

```python
# app/core/models.py — User class, after late_cutoff_local
    # Per-user local time the scheduled Friday review fires (design spec
    # §3 step 1) — same "Fri 16:00" window F1's own design names as the
    # deferred-dossier catch-up slot. Mirrors pulse_fire_time_local's
    # shape exactly: per-owner, own column, own default, no cross-ritual
    # scheduling table.
    friday_review_fire_time_local: Mapped[datetime.time] = mapped_column(
        Time, default=datetime.time(16, 0)
    )
```

```python
# app/core/models.py — add after DossierDelivery

class FridayReviewDelivery(Base):
    """One row per (owner, week, trigger) — mirrors DossierDelivery's role
    for F1. trigger discriminates a scheduled push from an on-demand
    `/mentor review` pull (design spec §6's correction over an earlier
    draft with no discriminator): at most one SCHEDULED delivery per
    owner/week (the idempotency the poll loop needs), while a pull is
    always answered fresh and, on a repeat pull the same week, updates
    this same row in place rather than colliding — same shape
    dispatch.py's handle_trigger already uses for PulseDelivery's
    cron-vs-pull distinction."""

    __tablename__ = "friday_review_deliveries"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(
        String, ForeignKey("users.id"), nullable=False, index=True
    )
    week_start_date: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    trigger: Mapped[str] = mapped_column(String, nullable=False)  # "scheduled" | "pull"
    sent_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    skipped_reason: Mapped[str | None] = mapped_column(String, nullable=True)  # "on_leave" | None
    prompt_version: Mapped[str] = mapped_column(String, nullable=False)
    card_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    # [{description, source_reference_key, skill_category}, ...] — evidence
    # not yet mirrored into Accomplishment, the "Confirm & log" checklist's
    # contents (design spec §2/§4's correction: written directly via
    # OwnerScope on confirm, never through append_ledger_item's shared-
    # agenda-mirroring path).
    proposed_ledger_items: Mapped[list] = mapped_column(JSONB, default=list)
    ledger_confirmed_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "owner_user_id", "week_start_date", "trigger",
            name="uq_friday_review_delivery_owner_week_trigger",
        ),
    )
```

```python
# app/agenda/models.py — Accomplishment, add one nullable column
    # Populated lazily by synthesize_friday_review the first time this row
    # is read with a null value (design spec §4/§6) — no backfill
    # migration, no categorization pipeline (F5 isn't built yet). Closed
    # set enforced in code (app.sub_agents.friday_review.sub_agents.
    # synthesize.agent.SKILL_CATEGORIES), stored as free String — matches
    # this file's own AgendaItem.source convention rather than
    # introducing this codebase's first real DB-level enum.
    skill_category: Mapped[str | None] = mapped_column(String, nullable=True)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_friday_review_delivery_model.py -v`
Expected: PASS (all 5 tests)

- [ ] **Step 5: Write the migration**

```python
# migrations/versions/<generate via alembic>_friday_review_schema.py
"""friday review schema

Revision ID: <generated>
Revises: 7f3a9c1e6b02
Create Date: 2026-08-24
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "<generated>"
down_revision = "7f3a9c1e6b02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "friday_review_fire_time_local",
            sa.Time(),
            nullable=False,
            server_default="16:00:00",
        ),
    )
    op.create_table(
        "friday_review_deliveries",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("owner_user_id", sa.String(), nullable=False),
        sa.Column("week_start_date", sa.Date(), nullable=False),
        sa.Column("trigger", sa.String(), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("skipped_reason", sa.String(), nullable=True),
        sa.Column("prompt_version", sa.String(), nullable=False),
        sa.Column("card_ref", sa.String(), nullable=True),
        sa.Column(
            "proposed_ledger_items",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("ledger_confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "owner_user_id", "week_start_date", "trigger",
            name="uq_friday_review_delivery_owner_week_trigger",
        ),
    )
    op.create_index(
        op.f("ix_friday_review_deliveries_owner_user_id"),
        "friday_review_deliveries",
        ["owner_user_id"],
        unique=False,
    )
    op.add_column(
        "accomplishments", sa.Column("skill_category", sa.String(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("accomplishments", "skill_category")
    op.drop_index(
        op.f("ix_friday_review_deliveries_owner_user_id"),
        table_name="friday_review_deliveries",
    )
    op.drop_table("friday_review_deliveries")
    op.drop_column("users", "friday_review_fire_time_local")
```

Generate the real revision id with `uv run alembic revision --autogenerate -m "friday review schema"` against `mentor_f4` (this worktree's `.env` already points there), then hand-verify the diff matches the above — reorder columns to match rather than fighting autogenerate over cosmetic ordering. `server_default="16:00:00"` is required on `friday_review_fire_time_local` because it's `NOT NULL` on a table (`users`) that already has rows.

- [ ] **Step 6: Apply and verify**

Run: `uv run alembic upgrade head && uv run pytest tests/unit/test_friday_review_delivery_model.py -v`
Expected: migration applies cleanly, all 5 tests PASS against the real table.

- [ ] **Step 7: Commit**

```bash
git add app/core/models.py app/agenda/models.py migrations/versions/ tests/unit/test_friday_review_delivery_model.py
git commit -m "feat(friday-review): add FridayReviewDelivery, fire-time column, skill_category"
```

---

### Task 2: `sub_agents/friday_review/sub_agents/gather` — pure aggregation over existing tables

**Files:**
- Create: `app/sub_agents/friday_review/__init__.py`, `app/sub_agents/friday_review/sub_agents/__init__.py`, `app/sub_agents/friday_review/sub_agents/gather/__init__.py`
- Create: `app/sub_agents/friday_review/sub_agents/gather/agent.py`
- Test: `tests/unit/sub_agents/friday_review/sub_agents/gather/test_agent.py`

**Interfaces:**
- Consumes: `app.agenda.scope.resolve_pair_scope`, `app.agenda.store.get_agenda`, `app.identity.friday_batch.build_batch`, `app.core.models.{Commitment, WorkItem, Goal}`, `app.agenda.models.Accomplishment`.
- Produces: `WinEvidence`, `SlippedItem`, `AgendaSummary`, `OkrProgress`, `SkillDistribution`, `FridayReviewContext` (dataclasses), `gather_friday_review_context(owner_scope: OwnerScope, clock: Clock) -> FridayReviewContext`, `week_start_monday(now: datetime.datetime) -> datetime.date`. Task 3 (synthesize) consumes `FridayReviewContext`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/sub_agents/friday_review/sub_agents/gather/test_agent.py
import datetime
import uuid

from app.agenda.models import Accomplishment, AgendaItem, Pair
from app.core.clock import FrozenClock
from app.core.models import Commitment, Goal, PulseDelivery, User, WorkItem
from app.core.scope import OwnerScope
from app.sub_agents.friday_review.sub_agents.gather.agent import (
    gather_friday_review_context,
    week_start_monday,
)

NOW = datetime.datetime(2026, 8, 28, 16, 0, 0, tzinfo=datetime.UTC)  # a Friday


def _make_owner(pg_session) -> User:
    user = User(id=str(uuid.uuid4()), created_at=NOW)
    pg_session.add(user)
    pg_session.commit()
    return user


def test_week_start_monday_from_a_friday():
    assert week_start_monday(NOW) == datetime.date(2026, 8, 24)


def test_wins_include_accomplishments_from_past_week_already_logged(pg_session):
    user = _make_owner(pg_session)
    scope = OwnerScope(owner_user_id=user.id, session=pg_session)
    pg_session.add(
        Accomplishment(
            id=str(uuid.uuid4()), owner_user_id=user.id, description="Shipped X",
            source_reference_key="acc-1", occurred_at=NOW - datetime.timedelta(days=2),
        )
    )
    pg_session.commit()

    context = gather_friday_review_context(scope, FrozenClock(at=NOW))

    wins = [w for w in context.wins if w.source_reference_key == "acc-1"]
    assert len(wins) == 1
    assert wins[0].already_logged is True


def test_wins_include_delivered_commitments_not_yet_logged(pg_session):
    user = _make_owner(pg_session)
    scope = OwnerScope(owner_user_id=user.id, session=pg_session)
    commitment_id = str(uuid.uuid4())
    pg_session.add(
        Commitment(
            id=commitment_id, owner_user_id=user.id, promised_to_person_id=None,
            description="Deliver the report", source_reference_key="src-1",
            promised_at=NOW - datetime.timedelta(days=10),
            due_at=NOW - datetime.timedelta(days=3),
            delivered_at=NOW - datetime.timedelta(days=2), status="delivered",
        )
    )
    pg_session.commit()

    context = gather_friday_review_context(scope, FrozenClock(at=NOW))

    wins = [w for w in context.wins if w.source_reference_key == commitment_id]
    assert len(wins) == 1
    assert wins[0].already_logged is False
    assert wins[0].kind == "commitment"


def test_wins_include_closed_work_items_not_yet_logged(pg_session):
    user = _make_owner(pg_session)
    scope = OwnerScope(owner_user_id=user.id, session=pg_session)
    item_id = str(uuid.uuid4())
    pg_session.add(
        WorkItem(
            id=item_id, owner_user_id=user.id, actor_reference_key="me",
            resolved_person_id=None, source="linear", external_id="LIN-1",
            title="Migrate the pipeline", status="Done", url="https://linear.app/x/1",
            due_at=None, updated_at=NOW - datetime.timedelta(days=1),
        )
    )
    pg_session.commit()

    context = gather_friday_review_context(scope, FrozenClock(at=NOW))

    wins = [w for w in context.wins if w.source_reference_key == item_id]
    assert len(wins) == 1
    assert wins[0].already_logged is False
    assert wins[0].source_link == "https://linear.app/x/1"


def test_open_work_item_is_not_a_win(pg_session):
    user = _make_owner(pg_session)
    scope = OwnerScope(owner_user_id=user.id, session=pg_session)
    pg_session.add(
        WorkItem(
            id=str(uuid.uuid4()), owner_user_id=user.id, actor_reference_key="me",
            resolved_person_id=None, source="linear", external_id="LIN-2",
            title="Still in progress", status="In Progress", url="https://x/2",
            due_at=None, updated_at=NOW - datetime.timedelta(days=1),
        )
    )
    pg_session.commit()

    context = gather_friday_review_context(scope, FrozenClock(at=NOW))
    assert context.wins == []


def test_slipped_is_overdue_open_commitment(pg_session):
    user = _make_owner(pg_session)
    scope = OwnerScope(owner_user_id=user.id, session=pg_session)
    pg_session.add(
        Commitment(
            id=str(uuid.uuid4()), owner_user_id=user.id, promised_to_person_id=None,
            description="Send the doc", source_reference_key="src-2",
            promised_at=NOW - datetime.timedelta(days=10),
            due_at=NOW - datetime.timedelta(days=1), delivered_at=None, status="open",
        )
    )
    pg_session.commit()

    context = gather_friday_review_context(scope, FrozenClock(at=NOW))
    assert len(context.slipped) == 1
    assert context.slipped[0].description == "Send the doc"


def test_three_week_pattern_boundary(pg_session):
    user = _make_owner(pg_session)
    scope = OwnerScope(owner_user_id=user.id, session=pg_session)
    pg_session.add_all(
        [
            Commitment(
                id=str(uuid.uuid4()), owner_user_id=user.id, promised_to_person_id=None,
                description="Old enough", source_reference_key="src-old",
                promised_at=NOW - datetime.timedelta(days=21), due_at=None,
                delivered_at=None, status="open",
            ),
            Commitment(
                id=str(uuid.uuid4()), owner_user_id=user.id, promised_to_person_id=None,
                description="Not old enough", source_reference_key="src-new",
                promised_at=NOW - datetime.timedelta(days=20), due_at=None,
                delivered_at=None, status="open",
            ),
        ]
    )
    pg_session.commit()

    context = gather_friday_review_context(scope, FrozenClock(at=NOW))
    by_desc = {s.description: s.weeks_running for s in context.slipped}
    assert by_desc["Old enough"] is True
    assert by_desc["Not old enough"] is False


def test_agenda_resolved_carried_and_stuck(pg_session):
    user = _make_owner(pg_session)
    manager = User(id=str(uuid.uuid4()), created_at=NOW)
    pg_session.add(manager)
    pair = Pair(
        id=str(uuid.uuid4()), report_user_id=user.id, manager_user_id=manager.id,
        started_at=NOW - datetime.timedelta(days=100), ended_at=None,
    )
    pg_session.add(pair)
    pg_session.add_all(
        [
            AgendaItem(
                id=str(uuid.uuid4()), report_user_id=user.id, pair_id=pair.id,
                text="Resolved this week", source="manual", source_link=None,
                visibility="shared", status="resolved", surfaced_count=1,
                created_at=NOW - datetime.timedelta(days=5),
                resolved_at=NOW - datetime.timedelta(days=1),
                created_by_user_id=user.id, created_by_role="report",
            ),
            AgendaItem(
                id=str(uuid.uuid4()), report_user_id=user.id, pair_id=pair.id,
                text="Still carried", source="manual", source_link=None,
                visibility="shared", status="open", surfaced_count=1,
                created_at=NOW - datetime.timedelta(days=5), resolved_at=None,
                created_by_user_id=user.id, created_by_role="report",
            ),
            AgendaItem(
                id=str(uuid.uuid4()), report_user_id=user.id, pair_id=pair.id,
                text="Stuck", source="manual", source_link=None,
                visibility="shared", status="open", surfaced_count=3,
                created_at=NOW - datetime.timedelta(days=20), resolved_at=None,
                created_by_user_id=user.id, created_by_role="report",
            ),
        ]
    )
    pg_session.commit()
    scope = OwnerScope(owner_user_id=user.id, session=pg_session)

    context = gather_friday_review_context(scope, FrozenClock(at=NOW))

    assert [i.text for i in context.agenda.resolved_this_week] == ["Resolved this week"]
    carried_texts = {i.text for i in context.agenda.carried}
    assert carried_texts == {"Still carried", "Stuck"}
    assert [i.text for i in context.agenda.stuck] == ["Stuck"]


def test_okr_progress_and_career_goal(pg_session):
    user = _make_owner(pg_session)
    scope = OwnerScope(owner_user_id=user.id, session=pg_session)
    pg_session.add_all(
        [
            Goal(
                id=str(uuid.uuid4()), owner_user_id=user.id, title="Ship the migration",
                status="active", external_ref=None, created_at=NOW,
                goal_type="key_result", progress=0.6, current_value=6, target_value=10,
            ),
            Goal(
                id=str(uuid.uuid4()), owner_user_id=user.id, title="Become a tech lead",
                status="active", external_ref=None, created_at=NOW,
                goal_type="career_goal", progress=None, current_value=None, target_value=None,
            ),
        ]
    )
    pg_session.commit()

    context = gather_friday_review_context(scope, FrozenClock(at=NOW))
    assert len(context.okr_progress) == 1
    assert context.okr_progress[0].title == "Ship the migration"
    assert context.career_goal is not None
    assert context.career_goal.title == "Become a tech lead"


def test_skill_distribution_only_categorized_within_trailing_8_weeks(pg_session):
    user = _make_owner(pg_session)
    scope = OwnerScope(owner_user_id=user.id, session=pg_session)
    pg_session.add_all(
        [
            Accomplishment(
                id=str(uuid.uuid4()), owner_user_id=user.id, description="a",
                source_reference_key="a", occurred_at=NOW - datetime.timedelta(days=10),
                skill_category="technical_execution",
            ),
            Accomplishment(
                id=str(uuid.uuid4()), owner_user_id=user.id, description="b",
                source_reference_key="b", occurred_at=NOW - datetime.timedelta(days=10),
                skill_category=None,
            ),
            Accomplishment(
                id=str(uuid.uuid4()), owner_user_id=user.id, description="c",
                source_reference_key="c", occurred_at=NOW - datetime.timedelta(days=60),
                skill_category="technical_execution",
            ),
        ]
    )
    pg_session.commit()

    context = gather_friday_review_context(scope, FrozenClock(at=NOW))
    assert context.skill_distribution.counts == {"technical_execution": 1}


def test_daily_pulse_pattern_requires_three_distinct_days(pg_session):
    user = _make_owner(pg_session)
    scope = OwnerScope(owner_user_id=user.id, session=pg_session)
    for i in range(3):
        pg_session.add(
            PulseDelivery(
                id=str(uuid.uuid4()), owner_user_id=user.id, ritual="pulse",
                local_date=NOW.date() - datetime.timedelta(days=i), trigger="cron",
                delivered_at=NOW - datetime.timedelta(days=i), item_ids=["item-x", "item-y"],
                context_hash="h", prompt_version="p",
            )
        )
    pg_session.add(
        PulseDelivery(
            id=str(uuid.uuid4()), owner_user_id=user.id, ritual="pulse",
            local_date=NOW.date() - datetime.timedelta(days=5), trigger="cron",
            delivered_at=NOW - datetime.timedelta(days=5), item_ids=["item-y"],
            context_hash="h", prompt_version="p",
        )
    )
    pg_session.commit()

    context = gather_friday_review_context(scope, FrozenClock(at=NOW))
    assert context.daily_pulse_patterns == ["item-x"]


def test_identity_batch_is_empty_when_nothing_pending(pg_session):
    user = _make_owner(pg_session)
    scope = OwnerScope(owner_user_id=user.id, session=pg_session)
    context = gather_friday_review_context(scope, FrozenClock(at=NOW))
    assert context.identity_batch == []


def test_cross_owner_isolation(pg_session):
    owner_a = _make_owner(pg_session)
    owner_b = _make_owner(pg_session)
    pg_session.add(
        Accomplishment(
            id=str(uuid.uuid4()), owner_user_id=owner_b.id, description="b's win",
            source_reference_key="acc-b", occurred_at=NOW - datetime.timedelta(days=1),
        )
    )
    pg_session.commit()

    scope_a = OwnerScope(owner_user_id=owner_a.id, session=pg_session)
    context = gather_friday_review_context(scope_a, FrozenClock(at=NOW))
    assert context.wins == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/sub_agents/friday_review/sub_agents/gather/test_agent.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement**

```python
# app/sub_agents/friday_review/sub_agents/gather/agent.py
"""Layer 6's gather step for F4: pure aggregation over tables F1/F2/F3
already populate — no connector, no LLM call (AGENT.md §2). Almost every
query here is a direct OwnerScope-scoped SQL read; the one exception is
AgendaItem, which goes through PairScope (agenda's own scoping rule) via
resolve_pair_scope(session, owner, owner) — the owner viewing their own
agenda, the same self-access case app.sub_agents.dossier.sub_agents.
gather.agent already uses for recurring-1:1 carryover."""

import datetime
from collections import Counter
from dataclasses import dataclass, field

from sqlalchemy import select

from app.agenda.models import Accomplishment, AgendaItem
from app.agenda.scope import resolve_pair_scope
from app.agenda.store import get_agenda
from app.core.clock import Clock
from app.core.models import Commitment, Goal, PulseDelivery, WorkItem
from app.core.scope import OwnerScope
from app.identity.friday_batch import FridayBatchItem, build_batch

EVIDENCE_WINDOW_DAYS = 7
THREE_WEEK_PATTERN_DAYS = 21
SKILL_DISTRIBUTION_WINDOW_DAYS = 56  # trailing 8 weeks
STUCK_SURFACED_COUNT_THRESHOLD = 3
DAILY_PULSE_PATTERN_MIN_DISTINCT_DAYS = 3

# Case-insensitive containment check against real Jira/Linear/GitHub status
# text (app/ingest/live_source.py passes each source's raw status string
# straight through — WorkItem.status is never normalized to a fixed case
# or vocabulary). A pragmatic closed set, not an exhaustive one; documented
# rather than hidden, same posture as this codebase's other scoped
# simplifications.
_CLOSED_WORK_ITEM_STATUSES = {"done", "closed", "merged", "resolved", "complete", "completed"}


def _is_closed_status(status: str | None) -> bool:
    return status is not None and status.strip().lower() in _CLOSED_WORK_ITEM_STATUSES


def week_start_monday(now: datetime.datetime) -> datetime.date:
    return now.date() - datetime.timedelta(days=now.weekday())


@dataclass(frozen=True)
class WinEvidence:
    description: str
    source_reference_key: str
    source_link: str | None
    kind: str  # "accomplishment" | "commitment" | "work_item"
    # False means this evidence has no Accomplishment row yet — a
    # candidate for the "Confirm & log" proposal (design spec §2's
    # correction: proposed here, never auto-written).
    already_logged: bool
    existing_skill_category: str | None = None


@dataclass(frozen=True)
class SlippedItem:
    description: str
    due_at: datetime.datetime | None
    source_reference_key: str
    weeks_running: bool


@dataclass(frozen=True)
class AgendaSummary:
    resolved_this_week: list[AgendaItem] = field(default_factory=list)
    carried: list[AgendaItem] = field(default_factory=list)
    stuck: list[AgendaItem] = field(default_factory=list)


@dataclass(frozen=True)
class OkrProgress:
    title: str
    goal_type: str
    progress: float | None
    current_value: float | None
    target_value: float | None


@dataclass(frozen=True)
class SkillDistribution:
    counts: dict[str, int]


@dataclass(frozen=True)
class FridayReviewContext:
    week_start: datetime.date
    wins: list[WinEvidence]
    slipped: list[SlippedItem]
    agenda: AgendaSummary
    okr_progress: list[OkrProgress]
    career_goal: OkrProgress | None
    daily_pulse_patterns: list[str]
    skill_distribution: SkillDistribution
    identity_batch: list[FridayBatchItem]


def _gather_wins(owner_scope: OwnerScope, now: datetime.datetime) -> list[WinEvidence]:
    window_start = now - datetime.timedelta(days=EVIDENCE_WINDOW_DAYS)
    wins: list[WinEvidence] = []

    accomplishments = (
        owner_scope.session.execute(
            owner_scope.query(Accomplishment).where(
                Accomplishment.occurred_at >= window_start,
                Accomplishment.occurred_at <= now,
            )
        )
        .scalars()
        .all()
    )
    for row in accomplishments:
        wins.append(
            WinEvidence(
                description=row.description,
                source_reference_key=row.source_reference_key,
                source_link=None,
                kind="accomplishment",
                already_logged=True,
                existing_skill_category=row.skill_category,
            )
        )

    commitments = (
        owner_scope.session.execute(
            owner_scope.query(Commitment).where(
                Commitment.status == "delivered",
                Commitment.delivered_at.is_not(None),
                Commitment.delivered_at >= window_start,
                Commitment.delivered_at <= now,
            )
        )
        .scalars()
        .all()
    )
    for row in commitments:
        wins.append(
            WinEvidence(
                description=row.description,
                source_reference_key=row.id,
                source_link=None,
                kind="commitment",
                already_logged=False,
            )
        )

    work_items = (
        owner_scope.session.execute(
            owner_scope.query(WorkItem).where(
                WorkItem.source.in_(["jira", "github", "linear"]),
                WorkItem.updated_at.is_not(None),
                WorkItem.updated_at >= window_start,
                WorkItem.updated_at <= now,
            )
        )
        .scalars()
        .all()
    )
    for row in work_items:
        if not _is_closed_status(row.status):
            continue
        wins.append(
            WinEvidence(
                description=row.title or row.external_id or row.id,
                source_reference_key=row.id,
                source_link=row.url,
                kind="work_item",
                already_logged=False,
            )
        )

    return wins


def _gather_slipped(owner_scope: OwnerScope, now: datetime.datetime) -> list[SlippedItem]:
    pattern_cutoff = now - datetime.timedelta(days=THREE_WEEK_PATTERN_DAYS)
    rows = (
        owner_scope.session.execute(
            owner_scope.query(Commitment).where(
                Commitment.status == "open",
                Commitment.due_at.is_not(None),
                Commitment.due_at < now,
            )
        )
        .scalars()
        .all()
    )
    slipped = [
        SlippedItem(
            description=row.description,
            due_at=row.due_at,
            source_reference_key=row.id,
            weeks_running=row.promised_at <= pattern_cutoff,
        )
        for row in rows
    ]

    # A promised_at <= 21d-ago open commitment can lack a due_at (open-
    # ended promise) and so never show up in the overdue query above —
    # still worth naming as a running pattern (design spec §3's own
    # `promised_at <= now-21d` filter is independent of the due_at query).
    already_included_ids = {s.source_reference_key for s in slipped}
    pattern_rows = (
        owner_scope.session.execute(
            owner_scope.query(Commitment).where(
                Commitment.status == "open",
                Commitment.promised_at <= pattern_cutoff,
            )
        )
        .scalars()
        .all()
    )
    for row in pattern_rows:
        if row.id in already_included_ids:
            continue
        slipped.append(
            SlippedItem(
                description=row.description,
                due_at=row.due_at,
                source_reference_key=row.id,
                weeks_running=True,
            )
        )
    return slipped


def _gather_agenda(owner_scope: OwnerScope, now: datetime.datetime) -> AgendaSummary:
    pair_scope = resolve_pair_scope(
        owner_scope.session, owner_scope.owner_user_id, owner_scope.owner_user_id
    )
    if pair_scope is None:
        return AgendaSummary()

    window_start = now - datetime.timedelta(days=EVIDENCE_WINDOW_DAYS)
    items = get_agenda(pair_scope)
    resolved_this_week = [
        item
        for item in items
        if item.status == "resolved"
        and item.resolved_at is not None
        and window_start <= item.resolved_at <= now
    ]
    carried = [item for item in items if item.status == "open"]
    stuck = [
        item for item in carried if item.surfaced_count >= STUCK_SURFACED_COUNT_THRESHOLD
    ]
    return AgendaSummary(resolved_this_week=resolved_this_week, carried=carried, stuck=stuck)


def _gather_okr_and_career_goal(
    owner_scope: OwnerScope,
) -> tuple[list[OkrProgress], OkrProgress | None]:
    rows = (
        owner_scope.session.execute(
            owner_scope.query(Goal).where(Goal.status == "active")
        )
        .scalars()
        .all()
    )
    okr_progress = [
        OkrProgress(
            title=row.title, goal_type=row.goal_type, progress=row.progress,
            current_value=row.current_value, target_value=row.target_value,
        )
        for row in rows
        if row.goal_type in ("objective", "key_result")
    ]
    career_rows = [row for row in rows if row.goal_type == "career_goal"]
    career_goal = (
        OkrProgress(
            title=career_rows[0].title, goal_type="career_goal",
            progress=career_rows[0].progress, current_value=career_rows[0].current_value,
            target_value=career_rows[0].target_value,
        )
        if career_rows
        else None
    )
    return okr_progress, career_goal


def _gather_skill_distribution(
    owner_scope: OwnerScope, now: datetime.datetime
) -> SkillDistribution:
    window_start = now - datetime.timedelta(days=SKILL_DISTRIBUTION_WINDOW_DAYS)
    rows = (
        owner_scope.session.execute(
            owner_scope.query(Accomplishment).where(
                Accomplishment.occurred_at >= window_start,
                Accomplishment.occurred_at <= now,
                Accomplishment.skill_category.is_not(None),
            )
        )
        .scalars()
        .all()
    )
    return SkillDistribution(counts=dict(Counter(row.skill_category for row in rows)))


def _gather_daily_pulse_patterns(owner_scope: OwnerScope, now: datetime.datetime) -> list[str]:
    window_start = (now - datetime.timedelta(days=EVIDENCE_WINDOW_DAYS)).date()
    rows = (
        owner_scope.session.execute(
            owner_scope.query(PulseDelivery).where(
                PulseDelivery.ritual == "pulse",
                PulseDelivery.local_date >= window_start,
                PulseDelivery.local_date <= now.date(),
            )
        )
        .scalars()
        .all()
    )
    day_sets: dict[str, set[datetime.date]] = {}
    for row in rows:
        for item_id in row.item_ids:
            day_sets.setdefault(item_id, set()).add(row.local_date)
    return sorted(
        item_id
        for item_id, days in day_sets.items()
        if len(days) >= DAILY_PULSE_PATTERN_MIN_DISTINCT_DAYS
    )


def gather_friday_review_context(owner_scope: OwnerScope, clock: Clock) -> FridayReviewContext:
    now = clock.now()
    okr_progress, career_goal = _gather_okr_and_career_goal(owner_scope)
    return FridayReviewContext(
        week_start=week_start_monday(now),
        wins=_gather_wins(owner_scope, now),
        slipped=_gather_slipped(owner_scope, now),
        agenda=_gather_agenda(owner_scope, now),
        okr_progress=okr_progress,
        career_goal=career_goal,
        daily_pulse_patterns=_gather_daily_pulse_patterns(owner_scope, now),
        skill_distribution=_gather_skill_distribution(owner_scope, now),
        identity_batch=build_batch(owner_scope),
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/sub_agents/friday_review/sub_agents/gather/test_agent.py -v`
Expected: PASS (all tests)

- [ ] **Step 5: Commit**

```bash
git add app/sub_agents/friday_review/__init__.py app/sub_agents/friday_review/sub_agents/__init__.py app/sub_agents/friday_review/sub_agents/gather/ tests/unit/sub_agents/friday_review/
git commit -m "feat(friday-review): add gather step (aggregation over F1/F2/F3 tables)"
```

---

### Task 3: `sub_agents/friday_review/sub_agents/synthesize` — prompt, phrasing, provenance drop, skill classification

**Files:**
- Create: `app/prompts/friday_review_synthesize.md`
- Create: `app/sub_agents/friday_review/sub_agents/synthesize/__init__.py`, `app/sub_agents/friday_review/sub_agents/synthesize/agent.py`
- Test: `tests/unit/sub_agents/friday_review/sub_agents/synthesize/test_agent.py`

**Interfaces:**
- Consumes: `FridayReviewContext` (Task 2).
- Produces: `SKILL_CATEGORIES`, `ScoredWin`, `FridayReviewCard` (dataclasses), `build_synthesize_agent() -> Agent`, `synthesize_friday_review(context, llm_agent) -> FridayReviewCard`, `drop_unsourced_wins(llm_wins, context_wins) -> list[ScoredWin]`, `build_proposed_ledger_items(card) -> list[dict]`. Task 5 (deliver) consumes `FridayReviewCard`.

- [ ] **Step 1: Write the failing tests** (pure functions only — no live LLM; matches `tests/unit/sub_agents/dossier/sub_agents/synthesize/test_agent.py`'s own scope)

```python
# tests/unit/sub_agents/friday_review/sub_agents/synthesize/test_agent.py
import datetime

from app.sub_agents.friday_review.sub_agents.gather.agent import (
    AgendaSummary,
    FridayReviewContext,
    SkillDistribution,
    WinEvidence,
)
from app.sub_agents.friday_review.sub_agents.synthesize.agent import (
    WinOutput,
    build_proposed_ledger_items,
    drop_unsourced_wins,
    format_okr_progress,
    is_quiet_week,
)


def test_drops_wins_with_no_matching_evidence():
    context_wins = [
        WinEvidence(
            description="Shipped X", source_reference_key="wi-1", source_link="https://x/1",
            kind="work_item", already_logged=False,
        )
    ]
    llm_wins = [
        WinOutput(source_reference_key="wi-1", phrased_text="Shipped the migration."),
        WinOutput(source_reference_key="hallucinated", phrased_text="Made something up."),
    ]
    kept = drop_unsourced_wins(llm_wins, context_wins)
    assert [w.source_reference_key for w in kept] == ["wi-1"]
    assert kept[0].source_link == "https://x/1"


def test_build_proposed_ledger_items_excludes_already_logged():
    from app.sub_agents.friday_review.sub_agents.synthesize.agent import ScoredWin

    wins = [
        ScoredWin(
            text="Shipped X", source_link="https://x/1", source_reference_key="wi-1",
            moved_goal_title=None, already_logged=False, skill_category="technical_execution",
        ),
        ScoredWin(
            text="Already on the ledger", source_link=None, source_reference_key="acc-1",
            moved_goal_title=None, already_logged=True, skill_category=None,
        ),
    ]
    proposed = build_proposed_ledger_items(wins)
    assert len(proposed) == 1
    assert proposed[0]["source_reference_key"] == "wi-1"
    assert proposed[0]["skill_category"] == "technical_execution"


def test_quiet_week_when_nothing_at_all():
    context = FridayReviewContext(
        week_start=datetime.date(2026, 8, 24), wins=[], slipped=[],
        agenda=AgendaSummary(), okr_progress=[], career_goal=None,
        daily_pulse_patterns=[], skill_distribution=SkillDistribution(counts={}),
        identity_batch=[],
    )
    assert is_quiet_week(context, kept_wins=[]) is True


def test_not_quiet_week_when_okr_progress_exists():
    from app.sub_agents.friday_review.sub_agents.gather.agent import OkrProgress

    context = FridayReviewContext(
        week_start=datetime.date(2026, 8, 24), wins=[], slipped=[],
        agenda=AgendaSummary(), okr_progress=[
            OkrProgress(title="Ship it", goal_type="key_result", progress=0.5,
                        current_value=5, target_value=10)
        ], career_goal=None, daily_pulse_patterns=[],
        skill_distribution=SkillDistribution(counts={}), identity_batch=[],
    )
    assert is_quiet_week(context, kept_wins=[]) is False


def test_format_okr_progress_is_deterministic_not_llm():
    from app.sub_agents.friday_review.sub_agents.gather.agent import OkrProgress

    lines = format_okr_progress(
        [OkrProgress(title="Ship it", goal_type="key_result", progress=0.6,
                      current_value=6, target_value=10)]
    )
    assert lines == ["Ship it: 60%"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/sub_agents/friday_review/sub_agents/synthesize/test_agent.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write the prompt**

```markdown
<!-- app/prompts/friday_review_synthesize.md -->
<!-- version: friday_review_synthesize@v1 -->

You are writing a Friday afternoon reflection: a mirror on the week, not a
performance report. You will be given an assembled context object — wins
already computed from real evidence rows (each with a stable
`source_reference_key`), current OKR/goal progress, a career goal (if
any), and a skill-category distribution. Everything numeric or aggregate
is already computed for you; do not recompute or restate counts, and
never invent a win, a goal mapping, or a trend not present in the input.

Your job has exactly four parts:

1. **Phrase each win** — for every item in `wins`, write one plain,
   neutral sentence describing what happened (no praise inflation, no
   scolding — the evidence carries the tone). Reference it by its
   `source_reference_key` exactly as given; never invent a
   `source_reference_key` that isn't in the input.
2. **Map wins to goals** — for any win that plausibly moved one of the
   given `okr_progress`/`career_goal` entries, name which one
   (`moved_goal_title`). Leave it null if there's no real connection —
   guessing a connection is worse than omitting one.
3. **One adjustment** — if `slipped` is non-empty, write exactly one
   concrete, actionable change for next week, derived only from the
   slipped evidence. If nothing slipped, or the week has no real content
   at all (`quiet_week`-shaped input), omit this entirely — never pad
   with generic advice.
4. **Career narrative** — only if a `career_goal` is given: one paragraph
   (2-4 sentences) connecting this week's skill distribution and the
   stated career goal, grounded only in the given counts and any real
   gaps they imply. Omit if no `career_goal` is given.

Additionally, **classify skill_category** for any win in the input whose
`existing_skill_category` is null: choose exactly one of
`technical_execution`, `cross_team_collab`, `mentorship`,
`leadership_docs`, based only on the win's own description. If none
plausibly fits, omit the classification for that item rather than
guessing.

Never speculate about performance, mood, or motive. No trend claims —
that judgment is made outside this prompt, from data you are not given
here.
```

- [ ] **Step 4: Implement**

```python
# app/sub_agents/friday_review/sub_agents/synthesize/agent.py
"""Layer 6 composition for F4: the ONLY LLM call in the whole ritual
(AGENT.md §2). Everything else in this module — okr formatting, quiet-
week detection, agenda/slipped rendering, proposed-ledger-item assembly —
is deterministic Python over FridayReviewContext, matching design spec
§4's "skill distribution and OKR progress are computed in gather, not
asked of the LLM" decision. The model's only jobs: phrase each win, map
it to a goal, write one adjustment, write the career narrative, and
classify a missing skill_category — see the prompt file itself."""

import dataclasses
import datetime
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from google.adk.agents import Agent
from google.genai import types as genai_types
from pydantic import BaseModel

from app.sub_agents.friday_review.sub_agents.gather.agent import (
    FridayReviewContext,
    OkrProgress,
    WinEvidence,
)

PROMPT_VERSION = "friday_review_synthesize@v1"
PROMPT_PATH = Path(__file__).resolve().parents[4] / "prompts" / "friday_review_synthesize.md"
MODEL = "gemini-2.5-flash"
OUTPUT_KEY = "friday_review_synthesize_result"

SKILL_CATEGORIES = (
    "technical_execution",
    "cross_team_collab",
    "mentorship",
    "leadership_docs",
)


@dataclass(frozen=True)
class ScoredWin:
    text: str
    source_link: str | None
    source_reference_key: str
    moved_goal_title: str | None
    already_logged: bool
    skill_category: str | None = None


@dataclass(frozen=True)
class FridayReviewCard:
    week_start: datetime.date
    wins: list[ScoredWin]
    slipped_lines: list[str]
    one_adjustment: str | None
    agenda_resolved_lines: list[str]
    agenda_stuck_lines: list[str]
    okr_progress_lines: list[str]
    career_narrative: str | None
    daily_pulse_patterns: list[str]
    skill_distribution_summary: str | None
    quiet_week: bool
    identity_asks_count: int


class WinOutput(BaseModel):
    source_reference_key: str
    phrased_text: str
    moved_goal_title: str | None = None


class SkillClassificationOutput(BaseModel):
    source_reference_key: str
    skill_category: str


class SynthesizeOutput(BaseModel):
    wins: list[WinOutput] = []
    one_adjustment: str | None = None
    career_narrative: str | None = None
    skill_classifications: list[SkillClassificationOutput] = []


def build_synthesize_agent() -> Agent:
    """Factory for the real synthesis Agent, mirrors app.sub_agents.
    dossier.sub_agents.synthesize.agent.build_synthesize_agent's shape
    exactly, including the appended {context_json} placeholder ADK needs
    to actually substitute the seeded state key into the prompt."""
    instruction = (
        PROMPT_PATH.read_text(encoding="utf-8") + "\n\nContext:\n{context_json}"
    )
    return Agent(
        name="friday_review_synthesize",
        model=MODEL,
        instruction=instruction,
        output_schema=SynthesizeOutput,
        output_key=OUTPUT_KEY,
        generate_content_config=genai_types.GenerateContentConfig(temperature=0),
    )


def drop_unsourced_wins(
    llm_wins: list[WinOutput], context_wins: list[WinEvidence]
) -> list[ScoredWin]:
    """The provenance rule, enforced against real DB rows rather than
    trusted from the LLM's own text: an LLM-phrased win only survives if
    its source_reference_key matches a real WinEvidence gather actually
    produced. A hallucinated key (one not in context_wins) is dropped —
    mirrors app.sub_agents.dossier.sub_agents.synthesize.agent's
    drop_unsourced_talking_points, adapted to key off real evidence
    instead of a bare source_link presence check."""
    by_key = {w.source_reference_key: w for w in context_wins}
    kept: list[ScoredWin] = []
    for llm_win in llm_wins:
        evidence = by_key.get(llm_win.source_reference_key)
        if evidence is None:
            continue
        kept.append(
            ScoredWin(
                text=llm_win.phrased_text,
                source_link=evidence.source_link,
                source_reference_key=evidence.source_reference_key,
                moved_goal_title=llm_win.moved_goal_title,
                already_logged=evidence.already_logged,
                skill_category=evidence.existing_skill_category,
            )
        )
    return kept


def _apply_skill_classifications(
    wins: list[ScoredWin], classifications: list[SkillClassificationOutput]
) -> list[ScoredWin]:
    by_key = {
        c.source_reference_key: c.skill_category
        for c in classifications
        if c.skill_category in SKILL_CATEGORIES
    }
    return [
        dataclasses.replace(w, skill_category=by_key[w.source_reference_key])
        if w.skill_category is None and w.source_reference_key in by_key
        else w
        for w in wins
    ]


def build_proposed_ledger_items(wins: list[ScoredWin]) -> list[dict]:
    """The "Confirm & log" checklist's contents (design spec §2's
    correction) — only evidence with no Accomplishment row yet, never
    wins already on the ledger. Written directly onto FridayReviewDelivery
    by deliver_friday_review; turned into real Accomplishment rows only by
    confirm_and_log_ledger_items, on explicit consent."""
    return [
        {
            "description": w.text,
            "source_reference_key": w.source_reference_key,
            "skill_category": w.skill_category,
        }
        for w in wins
        if not w.already_logged
    ]


def format_okr_progress(items: list[OkrProgress]) -> list[str]:
    lines = []
    for item in items:
        if item.progress is not None:
            lines.append(f"{item.title}: {round(item.progress * 100)}%")
        elif item.current_value is not None and item.target_value:
            lines.append(f"{item.title}: {item.current_value}/{item.target_value}")
        else:
            lines.append(f"{item.title}: in progress")
    return lines


def _format_slipped(context: FridayReviewContext) -> list[str]:
    return [s.description for s in context.slipped]


def _format_skill_distribution(context: FridayReviewContext) -> str | None:
    counts = context.skill_distribution.counts
    if not counts:
        return None
    total = sum(counts.values())
    parts = [f"{k}: {round(v / total * 100)}%" for k, v in sorted(counts.items())]
    return ", ".join(parts)


def is_quiet_week(context: FridayReviewContext, kept_wins: list[ScoredWin]) -> bool:
    return not (
        kept_wins
        or context.okr_progress
        or context.agenda.resolved_this_week
        or context.agenda.carried
        or context.slipped
    )


def _context_to_state(context: FridayReviewContext) -> dict[str, Any]:
    return {
        "context_json": json.dumps(
            {
                "wins": [dataclasses.asdict(w) for w in context.wins],
                "okr_progress": [dataclasses.asdict(g) for g in context.okr_progress],
                "career_goal": (
                    dataclasses.asdict(context.career_goal) if context.career_goal else None
                ),
                "slipped": [dataclasses.asdict(s) for s in context.slipped],
                "skill_distribution": context.skill_distribution.counts,
            },
            default=str,
        )
    }


def synthesize_friday_review(context: FridayReviewContext, llm_agent) -> FridayReviewCard:
    from app.core.adk_runner import run_agent_sync

    state = run_agent_sync(
        llm_agent,
        _context_to_state(context),
        kickoff_text="Write the Friday reflection for this context.",
    )
    result = SynthesizeOutput.model_validate(state[OUTPUT_KEY])

    wins = drop_unsourced_wins(result.wins, context.wins)
    wins = _apply_skill_classifications(wins, result.skill_classifications)
    quiet = is_quiet_week(context, wins)

    return FridayReviewCard(
        week_start=context.week_start,
        wins=wins,
        slipped_lines=_format_slipped(context),
        one_adjustment=None if quiet else result.one_adjustment,
        agenda_resolved_lines=[i.text for i in context.agenda.resolved_this_week],
        agenda_stuck_lines=[i.text for i in context.agenda.stuck],
        okr_progress_lines=format_okr_progress(context.okr_progress),
        career_narrative=result.career_narrative if context.career_goal else None,
        daily_pulse_patterns=context.daily_pulse_patterns,
        skill_distribution_summary=_format_skill_distribution(context),
        quiet_week=quiet,
        identity_asks_count=len(context.identity_batch),
    )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/unit/sub_agents/friday_review/sub_agents/synthesize/test_agent.py -v`
Expected: PASS (all tests — no LLM call exercised)

- [ ] **Step 6: Commit**

```bash
git add app/prompts/friday_review_synthesize.md app/sub_agents/friday_review/sub_agents/synthesize/ tests/unit/sub_agents/friday_review/sub_agents/synthesize/
git commit -m "feat(friday-review): add synthesize step, versioned prompt, provenance drop"
```

---

### Task 4: `delivery/cards.py` addition — `build_friday_review_card`

**Files:**
- Modify: `app/delivery/cards.py` (add `build_friday_review_card`, `FRIDAY_REVIEW_CONFIRM_LOG_ACTION_ID`)
- Test: `tests/unit/delivery/test_friday_review_cards.py`

**Interfaces:**
- Consumes: `FridayReviewCard` (Task 3).
- Produces: `build_friday_review_card(card: FridayReviewCard, delivery_id: str, proposed_ledger_items: list[dict]) -> list[dict]` (Slack Block Kit blocks), `FRIDAY_REVIEW_CONFIRM_LOG_ACTION_ID`. Task 5 (deliver) calls this.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/delivery/test_friday_review_cards.py
import datetime

from app.delivery.cards import FRIDAY_REVIEW_CONFIRM_LOG_ACTION_ID, build_friday_review_card
from app.sub_agents.friday_review.sub_agents.synthesize.agent import FridayReviewCard, ScoredWin


def _base_card(**overrides) -> FridayReviewCard:
    defaults = dict(
        week_start=datetime.date(2026, 8, 24), wins=[], slipped_lines=[],
        one_adjustment=None, agenda_resolved_lines=[], agenda_stuck_lines=[],
        okr_progress_lines=[], career_narrative=None, daily_pulse_patterns=[],
        skill_distribution_summary=None, quiet_week=False, identity_asks_count=0,
    )
    defaults.update(overrides)
    return FridayReviewCard(**defaults)


def test_quiet_week_renders_a_plain_statement_and_skips_adjustment():
    card = _base_card(quiet_week=True)
    blocks = build_friday_review_card(card, delivery_id="d1", proposed_ledger_items=[])
    text_blob = " ".join(
        b["text"]["text"] for b in blocks if b.get("type") == "section" and "text" in b
    )
    assert "quiet week" in text_blob.lower()
    assert "adjustment" not in text_blob.lower()


def test_confirm_log_button_only_rendered_when_items_proposed():
    card = _base_card(
        wins=[
            ScoredWin(text="Shipped X", source_link="https://x/1", source_reference_key="wi-1",
                      moved_goal_title=None, already_logged=False, skill_category=None)
        ]
    )
    blocks_with_items = build_friday_review_card(
        card, delivery_id="d1",
        proposed_ledger_items=[{"description": "Shipped X", "source_reference_key": "wi-1"}],
    )
    action_ids = [
        el.get("action_id")
        for b in blocks_with_items if b.get("type") == "actions"
        for el in b.get("elements", [])
    ]
    assert FRIDAY_REVIEW_CONFIRM_LOG_ACTION_ID in action_ids

    blocks_without_items = build_friday_review_card(card, delivery_id="d1", proposed_ledger_items=[])
    action_ids_without = [
        el.get("action_id")
        for b in blocks_without_items if b.get("type") == "actions"
        for el in b.get("elements", [])
    ]
    assert FRIDAY_REVIEW_CONFIRM_LOG_ACTION_ID not in action_ids_without


def test_empty_skill_distribution_renders_not_enough_history():
    card = _base_card(skill_distribution_summary=None)
    blocks = build_friday_review_card(card, delivery_id="d1", proposed_ledger_items=[])
    text_blob = " ".join(
        b["text"]["text"] for b in blocks if b.get("type") == "section" and "text" in b
    )
    assert "not enough categorized history yet" in text_blob.lower()


def test_identity_asks_section_omitted_when_zero():
    card = _base_card(identity_asks_count=0)
    blocks = build_friday_review_card(card, delivery_id="d1", proposed_ledger_items=[])
    text_blob = " ".join(
        b["text"]["text"] for b in blocks if b.get("type") == "section" and "text" in b
    )
    assert "who is this" not in text_blob.lower()


def test_stuck_agenda_items_rendered():
    card = _base_card(agenda_stuck_lines=["Renegotiate the deadline with Sam"])
    blocks = build_friday_review_card(card, delivery_id="d1", proposed_ledger_items=[])
    text_blob = " ".join(
        b["text"]["text"] for b in blocks if b.get("type") == "section" and "text" in b
    )
    assert "Renegotiate the deadline with Sam" in text_blob
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/delivery/test_friday_review_cards.py -v`
Expected: FAIL with `ImportError`

- [ ] **Step 3: Implement**

```python
# app/delivery/cards.py — add alongside build_dossier_card

FRIDAY_REVIEW_CONFIRM_LOG_ACTION_ID = "friday_review_confirm_log"


def build_friday_review_card(
    card: "FridayReviewCard", delivery_id: str, proposed_ledger_items: list[dict]
) -> list[dict]:
    """Slack Block Kit blocks for the Friday reflection. New function, own
    render pattern — same "one builder per ritual" precedent as
    build_dossier_card/build_card, not a generic composer."""
    blocks: list[dict] = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Friday Reflection — week of {card.week_start.isoformat()}*",
            },
        },
        {"type": "divider"},
    ]

    if card.quiet_week:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "_Quiet week — not much to show, but here's what's real._",
                },
            }
        )

    if card.wins:
        lines = []
        for w in card.wins:
            suffix = f" (<{w.source_link}|source>)" if w.source_link else ""
            goal = f" — moved *{w.moved_goal_title}*" if w.moved_goal_title else ""
            lines.append(f"• {w.text}{suffix}{goal}")
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": "*Shipped:*\n" + "\n".join(lines)}}
        )

    if card.slipped_lines:
        lines = "\n".join(f"• {line}" for line in card.slipped_lines)
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": f"*Slipped:*\n{lines}"}}
        )

    if card.one_adjustment and not card.quiet_week:
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*One adjustment:* {card.one_adjustment}"},
            }
        )

    if card.agenda_resolved_lines or card.agenda_stuck_lines:
        lines = [f"✅ {line}" for line in card.agenda_resolved_lines]
        lines += [f"⏳ {line} (worth raising directly)" for line in card.agenda_stuck_lines]
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "*Agenda 1-on-1s:*\n" + "\n".join(lines)},
            }
        )

    if card.okr_progress_lines:
        lines = "\n".join(f"• {line}" for line in card.okr_progress_lines)
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": f"*OKR progress:*\n{lines}"}}
        )

    if card.career_narrative:
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Toward your goal:* {card.career_narrative}"},
            }
        )

    skill_text = card.skill_distribution_summary or "Not enough categorized history yet."
    blocks.append(
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Skill distribution:* {skill_text}"}}
    )

    if card.daily_pulse_patterns:
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": "Pattern from your daily briefs: "
                        + ", ".join(card.daily_pulse_patterns),
                    }
                ],
            }
        )

    if card.identity_asks_count > 0:
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"{card.identity_asks_count} \"who is this\" question(s) below.",
                    }
                ],
            }
        )

    if proposed_ledger_items:
        lines = "\n".join(f"• {item['description']}" for item in proposed_ledger_items)
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Log these {len(proposed_ledger_items)} to your ledger?*\n{lines}"},
            }
        )
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Confirm & log"},
                        "action_id": FRIDAY_REVIEW_CONFIRM_LOG_ACTION_ID,
                        "value": delivery_id,
                    }
                ],
            }
        )

    return blocks
```

Add `from app.sub_agents.friday_review.sub_agents.synthesize.agent import FridayReviewCard` to `cards.py`'s `TYPE_CHECKING` imports (matching how `DossierCard` is already imported there) — a runtime import would create an import cycle (`synthesize/agent.py` doesn't import `cards.py`, but keep it under `TYPE_CHECKING` for consistency with the existing `build_dossier_card` pattern).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/delivery/test_friday_review_cards.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/delivery/cards.py tests/unit/delivery/test_friday_review_cards.py
git commit -m "feat(friday-review): add build_friday_review_card"
```

---

### Task 5: `sub_agents/friday_review/sub_agents/deliver` — `deliver_friday_review` + `confirm_and_log_ledger_items`

**Files:**
- Create: `app/sub_agents/friday_review/sub_agents/deliver/__init__.py`, `app/sub_agents/friday_review/sub_agents/deliver/agent.py`
- Test: `tests/unit/sub_agents/friday_review/sub_agents/deliver/test_agent.py`

**Interfaces:**
- Consumes: `FridayReviewCard` (Task 3), `build_friday_review_card` (Task 4), `SlackDeliverer`, `FridayReviewDelivery` (Task 1).
- Produces: `deliver_friday_review(card, week_start, trigger, owner_scope, slack_user_id, prompt_version, deliverer=None, clock=None) -> FridayReviewDelivery`, `confirm_and_log_ledger_items(owner_scope, clock, delivery_id) -> list[Accomplishment]`. Task 6/7 (flow/pull) call `deliver_friday_review`; Task 10 (Slack wiring) calls `confirm_and_log_ledger_items`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/sub_agents/friday_review/sub_agents/deliver/test_agent.py
import datetime
import uuid

from app.agenda.models import Accomplishment, AgendaItem
from app.core.clock import FrozenClock
from app.core.models import FridayReviewDelivery, User
from app.core.scope import OwnerScope
from app.sub_agents.friday_review.sub_agents.deliver.agent import (
    confirm_and_log_ledger_items,
    deliver_friday_review,
)
from app.sub_agents.friday_review.sub_agents.synthesize.agent import FridayReviewCard, ScoredWin

NOW = datetime.datetime(2026, 8, 28, 16, 0, 0, tzinfo=datetime.UTC)
WEEK_START = datetime.date(2026, 8, 24)


class _FakeDeliverer:
    enabled = True

    def __init__(self):
        self.sent = []

    def deliver(self, user_slack_id, blocks, channel_id=None, thread_ts=None, dm_thread_ts=None):
        self.sent.append((user_slack_id, blocks))
        return {"sent": True, "dm_ts": "123.456", "dm_channel": "D1"}


def _base_card(**overrides) -> FridayReviewCard:
    defaults = dict(
        week_start=WEEK_START, wins=[], slipped_lines=[], one_adjustment=None,
        agenda_resolved_lines=[], agenda_stuck_lines=[], okr_progress_lines=[],
        career_narrative=None, daily_pulse_patterns=[], skill_distribution_summary=None,
        quiet_week=False, identity_asks_count=0,
    )
    defaults.update(overrides)
    return FridayReviewCard(**defaults)


def _make_owner(pg_session) -> User:
    user = User(id=str(uuid.uuid4()), created_at=NOW, slack_user_id="U123")
    pg_session.add(user)
    pg_session.commit()
    return user


def test_deliver_writes_row_and_sends(pg_session):
    user = _make_owner(pg_session)
    scope = OwnerScope(owner_user_id=user.id, session=pg_session)
    deliverer = _FakeDeliverer()

    delivery = deliver_friday_review(
        _base_card(), week_start=WEEK_START, trigger="scheduled", owner_scope=scope,
        slack_user_id="U123", prompt_version="friday_review_synthesize@v1",
        deliverer=deliverer, clock=FrozenClock(at=NOW),
    )

    assert len(deliverer.sent) == 1
    fetched = pg_session.get(FridayReviewDelivery, delivery.id)
    assert fetched.sent_at is not None
    assert fetched.trigger == "scheduled"
    assert fetched.card_ref == "123.456"


def test_deliver_stores_proposed_ledger_items(pg_session):
    user = _make_owner(pg_session)
    scope = OwnerScope(owner_user_id=user.id, session=pg_session)
    card = _base_card(
        wins=[
            ScoredWin(text="Shipped X", source_link="https://x/1", source_reference_key="wi-1",
                      moved_goal_title=None, already_logged=False, skill_category="technical_execution")
        ]
    )

    delivery = deliver_friday_review(
        card, week_start=WEEK_START, trigger="scheduled", owner_scope=scope,
        slack_user_id="U123", prompt_version="friday_review_synthesize@v1",
        deliverer=_FakeDeliverer(), clock=FrozenClock(at=NOW),
    )

    assert delivery.proposed_ledger_items == [
        {"description": "Shipped X", "source_reference_key": "wi-1", "skill_category": "technical_execution"}
    ]


def test_second_scheduled_delivery_same_week_is_idempotent(pg_session):
    user = _make_owner(pg_session)
    scope = OwnerScope(owner_user_id=user.id, session=pg_session)
    deliverer = _FakeDeliverer()

    first = deliver_friday_review(
        _base_card(), week_start=WEEK_START, trigger="scheduled", owner_scope=scope,
        slack_user_id="U123", prompt_version="friday_review_synthesize@v1",
        deliverer=deliverer, clock=FrozenClock(at=NOW),
    )
    second = deliver_friday_review(
        _base_card(), week_start=WEEK_START, trigger="scheduled", owner_scope=scope,
        slack_user_id="U123", prompt_version="friday_review_synthesize@v1",
        deliverer=deliverer, clock=FrozenClock(at=NOW),
    )

    assert first.id == second.id
    assert len(deliverer.sent) == 1  # no second Slack send


def test_second_pull_same_week_updates_in_place(pg_session):
    user = _make_owner(pg_session)
    scope = OwnerScope(owner_user_id=user.id, session=pg_session)
    deliverer = _FakeDeliverer()

    first = deliver_friday_review(
        _base_card(), week_start=WEEK_START, trigger="pull", owner_scope=scope,
        slack_user_id="U123", prompt_version="friday_review_synthesize@v1",
        deliverer=deliverer, clock=FrozenClock(at=NOW),
    )
    second = deliver_friday_review(
        _base_card(one_adjustment="Renegotiate the deadline"), week_start=WEEK_START,
        trigger="pull", owner_scope=scope, slack_user_id="U123",
        prompt_version="friday_review_synthesize@v1", deliverer=deliverer,
        clock=FrozenClock(at=NOW + datetime.timedelta(minutes=5)),
    )

    assert first.id == second.id
    assert len(deliverer.sent) == 2  # a pull always re-sends
    fetched = pg_session.get(FridayReviewDelivery, first.id)
    assert fetched.id == second.id


def test_confirm_and_log_writes_accomplishments(pg_session):
    user = _make_owner(pg_session)
    scope = OwnerScope(owner_user_id=user.id, session=pg_session)
    delivery = FridayReviewDelivery(
        id=str(uuid.uuid4()), owner_user_id=user.id, week_start_date=WEEK_START,
        trigger="scheduled", sent_at=NOW, skipped_reason=None,
        prompt_version="friday_review_synthesize@v1", card_ref="123.456",
        proposed_ledger_items=[
            {"description": "Shipped X", "source_reference_key": "wi-1", "skill_category": "technical_execution"}
        ],
        ledger_confirmed_at=None, created_at=NOW,
    )
    pg_session.add(delivery)
    pg_session.commit()

    created = confirm_and_log_ledger_items(scope, FrozenClock(at=NOW), delivery.id)

    assert len(created) == 1
    assert created[0].description == "Shipped X"
    assert created[0].skill_category == "technical_execution"
    assert created[0].owner_user_id == user.id
    refreshed = pg_session.get(FridayReviewDelivery, delivery.id)
    assert refreshed.ledger_confirmed_at is not None

    # §2's correction, as a real regression test: no AgendaItem side effect.
    agenda_rows = pg_session.query(AgendaItem).filter(AgendaItem.report_user_id == user.id).all()
    assert agenda_rows == []


def test_confirm_and_log_is_idempotent_on_second_click(pg_session):
    user = _make_owner(pg_session)
    scope = OwnerScope(owner_user_id=user.id, session=pg_session)
    delivery = FridayReviewDelivery(
        id=str(uuid.uuid4()), owner_user_id=user.id, week_start_date=WEEK_START,
        trigger="scheduled", sent_at=NOW, skipped_reason=None,
        prompt_version="friday_review_synthesize@v1", card_ref="123.456",
        proposed_ledger_items=[{"description": "Shipped X", "source_reference_key": "wi-1", "skill_category": None}],
        ledger_confirmed_at=None, created_at=NOW,
    )
    pg_session.add(delivery)
    pg_session.commit()

    confirm_and_log_ledger_items(scope, FrozenClock(at=NOW), delivery.id)
    second = confirm_and_log_ledger_items(scope, FrozenClock(at=NOW), delivery.id)

    assert second == []
    count = (
        pg_session.query(Accomplishment)
        .filter(Accomplishment.owner_user_id == user.id)
        .count()
    )
    assert count == 1


def test_confirm_and_log_no_op_when_nothing_proposed(pg_session):
    user = _make_owner(pg_session)
    scope = OwnerScope(owner_user_id=user.id, session=pg_session)
    delivery = FridayReviewDelivery(
        id=str(uuid.uuid4()), owner_user_id=user.id, week_start_date=WEEK_START,
        trigger="scheduled", sent_at=NOW, skipped_reason=None,
        prompt_version="friday_review_synthesize@v1", card_ref=None,
        proposed_ledger_items=[], ledger_confirmed_at=None, created_at=NOW,
    )
    pg_session.add(delivery)
    pg_session.commit()

    result = confirm_and_log_ledger_items(scope, FrozenClock(at=NOW), delivery.id)
    assert result == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/sub_agents/friday_review/sub_agents/deliver/test_agent.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement**

```python
# app/sub_agents/friday_review/sub_agents/deliver/agent.py
"""Layer 7 for F4: renders the card, sends it, records FridayReviewDelivery,
and — only on explicit "Confirm & log" — writes real Accomplishment rows.

deliver_friday_review's dedupe follows dispatch.py's handle_trigger shape
exactly (cron-vs-pull), not deliver_dossier's check-then-return-existing
shape: a "scheduled" trigger idempotent-skips (returns the existing row,
sends nothing) if one already exists for (owner, week); a "pull" trigger
always re-sends and updates the existing (owner, week, "pull") row in
place rather than colliding on the unique constraint — same reasoning
PulseDelivery's own (owner, ritual, local_date, trigger) uniqueness uses
to "let a same-day pull render deltas" (its own docstring).

Known, stated gap (design spec §11): if a second pull the same week
refreshes proposed_ledger_items on a row whose ledger_confirmed_at is
already set, confirm_and_log_ledger_items will still no-op on it — the
newly-refreshed proposals from that second pull won't get a fresh
confirmation opportunity this pass. Not solved here, same posture as
every other explicitly-scoped-out gap in this design."""

import logging
import uuid

from sqlalchemy import select

from app.agenda.models import Accomplishment
from app.core.clock import Clock, SystemClock
from app.core.models import FridayReviewDelivery
from app.core.scope import OwnerScope
from app.delivery.cards import build_friday_review_card
from app.delivery.slack_deliverer import SlackDeliverer
from app.sub_agents.friday_review.sub_agents.synthesize.agent import (
    FridayReviewCard,
    build_proposed_ledger_items,
)

logger = logging.getLogger(__name__)


def _existing_delivery(owner_scope: OwnerScope, week_start, trigger: str):
    return owner_scope.session.execute(
        select(FridayReviewDelivery).where(
            FridayReviewDelivery.owner_user_id == owner_scope.owner_user_id,
            FridayReviewDelivery.week_start_date == week_start,
            FridayReviewDelivery.trigger == trigger,
        )
    ).scalar_one_or_none()


def deliver_friday_review(
    card: FridayReviewCard,
    week_start,
    trigger: str,
    owner_scope: OwnerScope,
    slack_user_id: str,
    prompt_version: str,
    deliverer: SlackDeliverer | None = None,
    clock: Clock | None = None,
) -> FridayReviewDelivery:
    clock = clock or SystemClock()
    now = clock.now()
    existing = _existing_delivery(owner_scope, week_start, trigger)

    if trigger == "scheduled" and existing is not None:
        return existing

    deliverer = deliverer or SlackDeliverer()
    proposed_ledger_items = build_proposed_ledger_items(card.wins)
    delivery_id = existing.id if existing is not None else str(uuid.uuid4())
    blocks = build_friday_review_card(card, delivery_id, proposed_ledger_items)

    result = deliverer.deliver(slack_user_id, blocks) if deliverer.enabled else {"sent": False}

    if existing is not None:
        existing.sent_at = now if result.get("sent") else None
        existing.prompt_version = prompt_version
        existing.card_ref = result.get("dm_ts")
        existing.proposed_ledger_items = proposed_ledger_items
        delivery = existing
    else:
        delivery = FridayReviewDelivery(
            id=delivery_id,
            owner_user_id=owner_scope.owner_user_id,
            week_start_date=week_start,
            trigger=trigger,
            sent_at=now if result.get("sent") else None,
            skipped_reason=None,
            prompt_version=prompt_version,
            card_ref=result.get("dm_ts"),
            proposed_ledger_items=proposed_ledger_items,
            ledger_confirmed_at=None,
            created_at=now,
        )
        owner_scope.add(delivery)
    owner_scope.commit()

    if result.get("sent") and result.get("dm_channel") and result.get("dm_ts"):
        from app.delivery.tts import deliver_friday_review_audio

        try:
            deliver_friday_review_audio(deliverer, card, result["dm_channel"], result["dm_ts"])
        except Exception:
            logger.exception(
                "deliver_friday_review: audio companion failed, text card already sent"
            )

    return delivery


def confirm_and_log_ledger_items(
    owner_scope: OwnerScope, clock: Clock, delivery_id: str
) -> list[Accomplishment]:
    """The write-back (design spec §2's correction): writes directly via
    OwnerScope only — no PairScope, no agenda mirror, unlike
    app.agenda.store.append_ledger_item. Idempotent on ledger_confirmed_at."""
    delivery = owner_scope.session.execute(
        select(FridayReviewDelivery).where(
            FridayReviewDelivery.id == delivery_id,
            FridayReviewDelivery.owner_user_id == owner_scope.owner_user_id,
        )
    ).scalar_one_or_none()
    if delivery is None or delivery.ledger_confirmed_at is not None:
        return []
    if not delivery.proposed_ledger_items:
        return []

    now = clock.now()
    created: list[Accomplishment] = []
    for item in delivery.proposed_ledger_items:
        row = Accomplishment(
            id=str(uuid.uuid4()),
            owner_user_id=owner_scope.owner_user_id,
            description=item["description"],
            source_reference_key=item["source_reference_key"],
            occurred_at=now,
            skill_category=item.get("skill_category"),
        )
        owner_scope.add(row)
        created.append(row)
    delivery.ledger_confirmed_at = now
    owner_scope.commit()
    return created
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/sub_agents/friday_review/sub_agents/deliver/test_agent.py -v`
Expected: PASS (all tests). Task 9 adds `deliver_friday_review_audio`; until then the `try/except` around it means this task's tests (which never set `dm_channel`/`dm_ts` truthy in a way requiring the real function — `_FakeDeliverer.deliver` above always returns both) will hit an `ImportError` inside the `try` — acceptable since it's caught and logged, but confirm by temporarily stubbing `app.delivery.tts.deliver_friday_review_audio` as a no-op function in this task if Task 9 hasn't landed yet in your working order. If tasks are done in order (this plan's order), skip this note — Task 9 doesn't exist yet at this point, so add a temporary no-op stub now and let Task 9 replace it:

```python
# app/delivery/tts.py — temporary stub, replaced for real in Task 9
def deliver_friday_review_audio(deliverer, card, channel_id: str, thread_ts: str) -> None:
    pass
```

- [ ] **Step 5: Commit**

```bash
git add app/sub_agents/friday_review/sub_agents/deliver/ app/delivery/tts.py tests/unit/sub_agents/friday_review/sub_agents/deliver/
git commit -m "feat(friday-review): add deliver step and confirm-and-log write-back"
```

---

### Task 6: `sub_agents/friday_review/agent.py` — the scheduled-push flow + suppression + identity batch commit

**Files:**
- Create: `app/sub_agents/friday_review/agent.py`
- Test: `tests/unit/sub_agents/friday_review/test_agent.py`

**Interfaces:**
- Consumes: `gather_friday_review_context` (Task 2), `synthesize_friday_review`/`build_synthesize_agent` (Task 3), `deliver_friday_review` (Task 5), `app.identity.friday_batch.commit_batch`, `app.core.models.Suppression`.
- Produces: `run_friday_review_flow(owner_scope, clock, llm_agent=None, deliverer=None) -> FridayReviewDelivery | None`, `FridayReviewOrchestrator(BaseAgent)`. Task 8 (scheduler) calls `run_friday_review_flow`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/sub_agents/friday_review/test_agent.py
import datetime
import uuid

from app.core.clock import FrozenClock
from app.core.models import FridayReviewDelivery, Suppression, User
from app.core.scope import OwnerScope
from app.sub_agents.friday_review.agent import run_friday_review_flow
from app.sub_agents.friday_review.sub_agents.synthesize.agent import SynthesizeOutput

NOW = datetime.datetime(2026, 8, 28, 16, 0, 0, tzinfo=datetime.UTC)


class _FakeDeliverer:
    enabled = True

    def __init__(self):
        self.sent = []

    def deliver(self, user_slack_id, blocks, channel_id=None, thread_ts=None, dm_thread_ts=None):
        self.sent.append((user_slack_id, blocks))
        return {"sent": True, "dm_ts": "123.456", "dm_channel": "D1"}


class _FakeLlmAgent:
    pass


def _make_owner(pg_session) -> User:
    user = User(id=str(uuid.uuid4()), created_at=NOW, slack_user_id="U123")
    pg_session.add(user)
    pg_session.commit()
    return user


def test_on_leave_suppression_skips_entirely(pg_session, monkeypatch):
    user = _make_owner(pg_session)
    pg_session.add(
        Suppression(
            id=str(uuid.uuid4()), owner_user_id=user.id, scope="temporal",
            target_ref="friday_review", reason="on leave", created_at=NOW,
            expires_at=NOW + datetime.timedelta(days=7), created_by="user",
        )
    )
    pg_session.commit()
    scope = OwnerScope(owner_user_id=user.id, session=pg_session)
    deliverer = _FakeDeliverer()

    delivery = run_friday_review_flow(scope, FrozenClock(at=NOW), deliverer=deliverer)

    assert delivery is not None
    assert delivery.sent_at is None
    assert delivery.skipped_reason == "on_leave"
    assert deliverer.sent == []


def test_normal_run_delivers_and_commits_identity_batch(pg_session, monkeypatch):
    user = _make_owner(pg_session)
    scope = OwnerScope(owner_user_id=user.id, session=pg_session)
    deliverer = _FakeDeliverer()

    def _fake_run_agent_sync(llm_agent, state, kickoff_text=None):
        return {
            "friday_review_synthesize_result": SynthesizeOutput().model_dump()
        }

    monkeypatch.setattr(
        "app.sub_agents.friday_review.sub_agents.synthesize.agent.run_agent_sync",
        _fake_run_agent_sync,
        raising=False,
    )
    import app.core.adk_runner as adk_runner

    monkeypatch.setattr(adk_runner, "run_agent_sync", _fake_run_agent_sync)

    committed = {}

    def _fake_commit_batch(scope_, batch, card_ref, clock_):
        committed["called"] = True

    monkeypatch.setattr(
        "app.sub_agents.friday_review.agent.commit_batch", _fake_commit_batch
    )

    delivery = run_friday_review_flow(
        scope, FrozenClock(at=NOW), llm_agent=_FakeLlmAgent(), deliverer=deliverer
    )

    assert delivery is not None
    assert delivery.sent_at is not None
    assert len(deliverer.sent) == 1
    assert committed.get("called") is True


def test_second_call_same_week_is_idempotent(pg_session, monkeypatch):
    user = _make_owner(pg_session)
    scope = OwnerScope(owner_user_id=user.id, session=pg_session)
    deliverer = _FakeDeliverer()

    def _fake_run_agent_sync(llm_agent, state, kickoff_text=None):
        return {"friday_review_synthesize_result": SynthesizeOutput().model_dump()}

    import app.core.adk_runner as adk_runner

    monkeypatch.setattr(adk_runner, "run_agent_sync", _fake_run_agent_sync)
    monkeypatch.setattr(
        "app.sub_agents.friday_review.agent.commit_batch", lambda *a, **k: None
    )

    first = run_friday_review_flow(scope, FrozenClock(at=NOW), llm_agent=_FakeLlmAgent(), deliverer=deliverer)
    second = run_friday_review_flow(scope, FrozenClock(at=NOW), llm_agent=_FakeLlmAgent(), deliverer=deliverer)

    assert first.id == second.id
    assert len(deliverer.sent) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/sub_agents/friday_review/test_agent.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement**

```python
# app/sub_agents/friday_review/agent.py
"""Orchestrates the SCHEDULED push path (design spec §3, steps 1-8, minus
the day/time gate itself — that's friday_review_scheduler.py's job, Task
8): suppression check -> gather -> synthesize -> deliver -> identity batch
commit. Mirrors app.sub_agents.dossier.agent's run_dossier_flow shape:
llm_agent built internally via build_synthesize_agent() when the caller
doesn't supply one (a None default would crash the real flow the moment
synthesize_friday_review calls run_agent_sync on it)."""

import datetime
import logging

from google.adk.agents import BaseAgent
from google.adk.events import Event
from google.adk.events.event_actions import EventActions
from sqlalchemy import select

from app.core.clock import Clock
from app.core.models import FridayReviewDelivery, Suppression, User
from app.core.scope import OwnerScope
from app.identity.friday_batch import build_batch, commit_batch
from app.sub_agents.friday_review.sub_agents.deliver.agent import deliver_friday_review
from app.sub_agents.friday_review.sub_agents.gather.agent import (
    gather_friday_review_context,
    week_start_monday,
)
from app.sub_agents.friday_review.sub_agents.synthesize.agent import (
    PROMPT_VERSION,
    build_synthesize_agent,
    synthesize_friday_review,
)

logger = logging.getLogger(__name__)

SUPPRESSION_TARGET_REF = "friday_review"


def _is_on_leave(owner_scope: OwnerScope, now: datetime.datetime) -> bool:
    rows = (
        owner_scope.session.execute(
            select(Suppression).where(
                Suppression.owner_user_id == owner_scope.owner_user_id,
                Suppression.target_ref == SUPPRESSION_TARGET_REF,
            )
        )
        .scalars()
        .all()
    )
    return any(row.expires_at > now for row in rows)


def _existing_scheduled_delivery(owner_scope: OwnerScope, week_start):
    return owner_scope.session.execute(
        select(FridayReviewDelivery).where(
            FridayReviewDelivery.owner_user_id == owner_scope.owner_user_id,
            FridayReviewDelivery.week_start_date == week_start,
            FridayReviewDelivery.trigger == "scheduled",
        )
    ).scalar_one_or_none()


def run_friday_review_flow(
    owner_scope: OwnerScope, clock: Clock, llm_agent=None, deliverer=None
) -> FridayReviewDelivery | None:
    now = clock.now()
    week_start = week_start_monday(now)

    existing = _existing_scheduled_delivery(owner_scope, week_start)
    if existing is not None:
        return existing

    if _is_on_leave(owner_scope, now):
        skip_row = FridayReviewDelivery(
            id=__import__("uuid").uuid4().hex,
            owner_user_id=owner_scope.owner_user_id,
            week_start_date=week_start,
            trigger="scheduled",
            sent_at=None,
            skipped_reason="on_leave",
            prompt_version=PROMPT_VERSION,
            card_ref=None,
            proposed_ledger_items=[],
            ledger_confirmed_at=None,
            created_at=now,
        )
        owner_scope.add(skip_row)
        owner_scope.commit()
        return skip_row

    context = gather_friday_review_context(owner_scope, clock)
    agent = llm_agent if llm_agent is not None else build_synthesize_agent()
    card = synthesize_friday_review(context, agent)

    user = owner_scope.session.get(User, owner_scope.owner_user_id)
    delivery = deliver_friday_review(
        card, week_start=week_start, trigger="scheduled", owner_scope=owner_scope,
        slack_user_id=user.slack_user_id, prompt_version=PROMPT_VERSION,
        deliverer=deliverer, clock=clock,
    )

    if delivery.sent_at is not None:
        try:
            batch = build_batch(owner_scope)
            if batch:
                commit_batch(owner_scope, batch, delivery.card_ref or "", clock)
        except Exception:
            logger.exception(
                "run_friday_review_flow: identity batch commit failed, card already sent"
            )

    return delivery


class FridayReviewOrchestrator(BaseAgent):
    """ADK-invoked entry point, mirrors DossierOrchestrator."""

    async def _run_async_impl(self, ctx):
        owner_scope = ctx.session.state["owner_scope"]
        clock = ctx.session.state["clock"]
        delivery = run_friday_review_flow(owner_scope, clock)
        state_delta = {"friday_review_delivery_id": delivery.id if delivery is not None else None}
        yield Event(author=self.name, actions=EventActions(state_delta=state_delta))
```

The test's `_fake_run_agent_sync` patches `app.core.adk_runner.run_agent_sync` directly (the module `synthesize_friday_review` imports it from at call time) rather than the re-export inside `synthesize/agent.py` — matches how `synthesize_dossier`'s own tests patch it in `tests/unit/sub_agents/dossier/sub_agents/synthesize/test_agent.py`; check that file for the exact monkeypatch target if this doesn't take.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/sub_agents/friday_review/test_agent.py -v`
Expected: PASS (all tests)

- [ ] **Step 5: Commit**

```bash
git add app/sub_agents/friday_review/agent.py tests/unit/sub_agents/friday_review/test_agent.py
git commit -m "feat(friday-review): add scheduled-push flow orchestrator"
```

---

### Task 7: `sub_agents/friday_review/pull.py` + `/mentor review` wiring

**Files:**
- Create: `app/sub_agents/friday_review/pull.py`
- Modify: `app/triggers/slack_command.py` (add `run_review_command`)
- Test: `tests/unit/sub_agents/friday_review/test_pull.py`, additions to `tests/unit/triggers/test_slack_command.py`

**Interfaces:**
- Consumes: `gather_friday_review_context`/`week_start_monday` (Task 2), `synthesize_friday_review`/`build_synthesize_agent` (Task 3), `deliver_friday_review` (Task 5).
- Produces: `pull_friday_review(owner_scope, clock, llm_agent=None, deliverer=None) -> FridayReviewDelivery`, `run_review_command(session, owner_user_id, clock=None) -> None`. Task 10 (Slack wiring) calls `run_review_command`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/sub_agents/friday_review/test_pull.py
import datetime
import uuid

from app.core.clock import FrozenClock
from app.core.models import User
from app.core.scope import OwnerScope
from app.sub_agents.friday_review.pull import pull_friday_review
from app.sub_agents.friday_review.sub_agents.synthesize.agent import SynthesizeOutput

TUESDAY = datetime.datetime(2026, 8, 25, 10, 0, 0, tzinfo=datetime.UTC)


class _FakeDeliverer:
    enabled = True

    def __init__(self):
        self.sent = []

    def deliver(self, user_slack_id, blocks, channel_id=None, thread_ts=None, dm_thread_ts=None):
        self.sent.append((user_slack_id, blocks))
        return {"sent": True, "dm_ts": "abc", "dm_channel": "D1"}


class _FakeLlmAgent:
    pass


def test_pull_runs_on_a_non_friday_and_uses_pull_trigger(pg_session, monkeypatch):
    user = User(id=str(uuid.uuid4()), created_at=TUESDAY, slack_user_id="U9")
    pg_session.add(user)
    pg_session.commit()
    scope = OwnerScope(owner_user_id=user.id, session=pg_session)
    deliverer = _FakeDeliverer()

    def _fake_run_agent_sync(llm_agent, state, kickoff_text=None):
        return {"friday_review_synthesize_result": SynthesizeOutput().model_dump()}

    import app.core.adk_runner as adk_runner

    monkeypatch.setattr(adk_runner, "run_agent_sync", _fake_run_agent_sync)

    delivery = pull_friday_review(
        scope, FrozenClock(at=TUESDAY), llm_agent=_FakeLlmAgent(), deliverer=deliverer
    )

    assert delivery.trigger == "pull"
    assert delivery.sent_at is not None
    assert len(deliverer.sent) == 1
```

Add to `tests/unit/triggers/test_slack_command.py` (existing file — follow its own fixture conventions, e.g. `make_user`/`pg_session`):

```python
def test_run_review_command_delivers(pg_session, monkeypatch):
    from app.triggers.slack_command import run_review_command
    # ... seed a linked user, monkeypatch app.core.adk_runner.run_agent_sync
    # the same way test_run_prep_command does for pull_dossier, then assert
    # a FridayReviewDelivery row exists with trigger="pull".
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/sub_agents/friday_review/test_pull.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement**

```python
# app/sub_agents/friday_review/pull.py
"""The /mentor review pull path: enters at gather directly, bypassing the
day/time gate AND the suppression check entirely (design spec §3: "the
Friday/time gate decides when to push automatically, never whether the
review can be pulled on demand" — same framing F1's pull_dossier gives
its own push-gate bypass). Always answers fresh: deliver_friday_review's
own "pull" branch updates the existing (owner, week, "pull") row in place
rather than deduping it away."""

from app.core.clock import Clock
from app.core.models import User
from app.core.scope import OwnerScope
from app.sub_agents.friday_review.sub_agents.deliver.agent import deliver_friday_review
from app.sub_agents.friday_review.sub_agents.gather.agent import (
    gather_friday_review_context,
    week_start_monday,
)
from app.sub_agents.friday_review.sub_agents.synthesize.agent import (
    PROMPT_VERSION,
    build_synthesize_agent,
    synthesize_friday_review,
)


def pull_friday_review(
    owner_scope: OwnerScope, clock: Clock, llm_agent=None, deliverer=None
):
    context = gather_friday_review_context(owner_scope, clock)
    agent = llm_agent if llm_agent is not None else build_synthesize_agent()
    card = synthesize_friday_review(context, agent)

    user = owner_scope.session.get(User, owner_scope.owner_user_id)
    return deliver_friday_review(
        card, week_start=week_start_monday(clock.now()), trigger="pull",
        owner_scope=owner_scope, slack_user_id=user.slack_user_id,
        prompt_version=PROMPT_VERSION, deliverer=deliverer, clock=clock,
    )
```

```python
# app/triggers/slack_command.py — add near run_prep_command

def run_review_command(
    session: Session,
    owner_user_id: str,
    clock: Clock | None = None,
) -> None:
    """The `/mentor review` work, run after Slack has already been acked
    (same 3s-window constraint as run_prep_command). Private by
    construction — pull_friday_review never threads a channel_id through,
    same reasoning run_prep_command's own docstring gives for dossiers."""
    clock = clock or SystemClock()
    try:
        user = session.get(User, owner_user_id)
        if user is None:
            logger.warning(
                "slack /mentor review: no User row for owner=%s", owner_user_id
            )
            return

        scope = OwnerScope(owner_user_id=owner_user_id, session=session)
        deliverer = SlackDeliverer()
        if not deliverer.enabled:
            logger.warning(
                "slack /mentor review: SLACK_BOT_TOKEN not configured, "
                "owner=%s got no card",
                owner_user_id,
            )
            return

        pull_friday_review(scope, clock, deliverer=deliverer)
    except Exception:
        logger.exception(
            "slack /mentor review: failed to gather/synthesize/deliver for owner=%s",
            owner_user_id,
        )
```

Add `from app.sub_agents.friday_review.pull import pull_friday_review` to `slack_command.py`'s import block, alongside the existing `from app.sub_agents.dossier.pull import pull_dossier`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/sub_agents/friday_review/test_pull.py tests/unit/triggers/test_slack_command.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/sub_agents/friday_review/pull.py app/triggers/slack_command.py tests/unit/sub_agents/friday_review/test_pull.py tests/unit/triggers/test_slack_command.py
git commit -m "feat(friday-review): add pull path and /mentor review command"
```

---

### Task 8: `triggers/friday_review_scheduler.py` — Friday-afternoon poll loop

**Files:**
- Create: `app/triggers/friday_review_scheduler.py`
- Test: `tests/unit/triggers/test_friday_review_scheduler.py`

**Interfaces:**
- Consumes: `run_friday_review_flow` (Task 6), `app.core.models.User`.
- Produces: `_should_fire(now_local: datetime.datetime, fire_time: datetime.time) -> bool`, `_get_friday_review_scheduled_owner_ids() -> list[str]`, `_poll_once(session_factory, owner_user_ids) -> None`, `main()`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/triggers/test_friday_review_scheduler.py
import datetime
import uuid

import pytest

from app.core.models import User
from app.triggers.friday_review_scheduler import (
    _get_friday_review_scheduled_owner_ids,
    _poll_once,
    _should_fire,
)


def test_whitelist_env_var_required(monkeypatch):
    monkeypatch.delenv("FRIDAY_REVIEW_SCHEDULED_OWNER_IDS", raising=False)
    with pytest.raises(RuntimeError):
        _get_friday_review_scheduled_owner_ids()


def test_whitelist_env_var_parses_csv(monkeypatch):
    monkeypatch.setenv("FRIDAY_REVIEW_SCHEDULED_OWNER_IDS", "a,b, c")
    assert _get_friday_review_scheduled_owner_ids() == ["a", "b", "c"]


@pytest.mark.parametrize(
    "weekday,time_,expected",
    [
        (3, datetime.time(16, 0), False),   # Thursday, exactly fire time
        (4, datetime.time(15, 59), False),  # Friday, before fire time
        (4, datetime.time(16, 0), True),    # Friday, exactly fire time
        (4, datetime.time(23, 59), True),   # Friday, late
        (5, datetime.time(16, 0), False),   # Saturday
    ],
)
def test_should_fire_boundaries(weekday, time_, expected):
    # 2026-08-24 is a Monday (weekday 0); offset to the target weekday.
    day = datetime.date(2026, 8, 24) + datetime.timedelta(days=weekday)
    now_local = datetime.datetime.combine(day, time_)
    assert _should_fire(now_local, datetime.time(16, 0)) is expected


def test_poll_once_calls_flow_only_for_whitelisted_linked_users(pg_session, monkeypatch):
    linked = User(
        id=str(uuid.uuid4()), created_at=datetime.datetime(2026, 8, 28, tzinfo=datetime.UTC),
        slack_user_id="U1", friday_review_fire_time_local=datetime.time(0, 0),
    )
    unlinked = User(
        id=str(uuid.uuid4()), created_at=datetime.datetime(2026, 8, 28, tzinfo=datetime.UTC),
        slack_user_id=None, friday_review_fire_time_local=datetime.time(0, 0),
    )
    pg_session.add_all([linked, unlinked])
    pg_session.commit()

    called_with = []
    monkeypatch.setattr(
        "app.triggers.friday_review_scheduler.run_friday_review_flow",
        lambda scope, clock: called_with.append(scope.owner_user_id),
    )

    _poll_once(lambda: pg_session, [linked.id, unlinked.id])

    assert called_with == [linked.id]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/triggers/test_friday_review_scheduler.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement**

```python
# app/triggers/friday_review_scheduler.py
"""L1 trigger: the Friday-afternoon (per-user friday_review_fire_time_local)
poll loop for F4. Templated on cron_scheduler.py's own shape exactly
(design spec §4: "own scheduler, own table, own gate-equivalent — the
established per-ritual pattern") — a fourth near-identical poll loop
alongside cron_scheduler.py/agenda_scheduler.py/dossier_scheduler.py,
deliberately not merged into any of them this pass."""

import datetime
import logging
import time
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.core.clock import SystemClock
from app.core.config import get_settings
from app.core.db import get_engine, get_session_factory
from app.core.models import User
from app.core.scope import OwnerScope
from app.sub_agents.friday_review.agent import run_friday_review_flow

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 60
FRIDAY_WEEKDAY = 4  # Monday=0 .. Sunday=6


def _should_fire(now_local: datetime.datetime, fire_time: datetime.time) -> bool:
    return now_local.weekday() == FRIDAY_WEEKDAY and now_local.time() >= fire_time


def _get_friday_review_scheduled_owner_ids() -> list[str]:
    import os

    raw = os.environ.get("FRIDAY_REVIEW_SCHEDULED_OWNER_IDS", "")
    owner_user_ids = [o.strip() for o in raw.split(",") if o.strip()]
    if not owner_user_ids:
        raise RuntimeError(
            "FRIDAY_REVIEW_SCHEDULED_OWNER_IDS is not set — refusing to start rather "
            "than silently scheduling every linked user in the database. Set it to a "
            "comma-separated list of owner_user_id(s), e.g. "
            "FRIDAY_REVIEW_SCHEDULED_OWNER_IDS=usr_amal"
        )
    return owner_user_ids


def _poll_once(session_factory, owner_user_ids: list[str]) -> None:
    session = session_factory()
    try:
        linked_users = (
            session.execute(
                select(User).where(
                    User.id.in_(owner_user_ids), User.slack_user_id.is_not(None)
                )
            )
            .scalars()
            .all()
        )
    finally:
        session.close()

    for user in linked_users:
        now_local = datetime.datetime.now(ZoneInfo(user.tz))
        if not _should_fire(now_local, user.friday_review_fire_time_local):
            continue

        session = session_factory()
        try:
            scope = OwnerScope(owner_user_id=user.id, session=session)
            run_friday_review_flow(scope, SystemClock())
        except Exception:
            logger.exception(
                "friday review scheduler: run failed for owner=%s, will retry next poll",
                user.id,
            )
        finally:
            session.close()


def main() -> None:
    from dotenv import load_dotenv

    load_dotenv()
    owner_user_ids = _get_friday_review_scheduled_owner_ids()
    engine = get_engine(get_settings().database_url)
    session_factory = get_session_factory(engine)

    logger.info(
        "friday review scheduler: polling every %ss for owner_user_ids=%s",
        POLL_INTERVAL_SECONDS,
        owner_user_ids,
    )
    while True:
        try:
            _poll_once(session_factory, owner_user_ids)
        except Exception:
            logger.exception("friday review scheduler: poll failed, will retry next interval")
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/triggers/test_friday_review_scheduler.py -v`
Expected: PASS (all tests)

- [ ] **Step 5: Commit**

```bash
git add app/triggers/friday_review_scheduler.py tests/unit/triggers/test_friday_review_scheduler.py
git commit -m "feat(friday-review): add Friday-afternoon poll loop scheduler"
```

---

### Task 9: `delivery/tts.py` addition — `deliver_friday_review_audio`

**Files:**
- Modify: `app/delivery/tts.py` (replace Task 5's temporary stub with the real implementation)
- Create: `app/prompts/friday_review_audio_script.v1.md`
- Test: `tests/unit/delivery/test_friday_review_tts.py`

**Interfaces:**
- Consumes: `FridayReviewCard` (Task 3).
- Produces: `build_friday_review_speech_script(card) -> str`, `draft_friday_review_audio_script(card) -> str | None`, `deliver_friday_review_audio(deliverer, card, channel_id, thread_ts) -> None`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/delivery/test_friday_review_tts.py
import datetime

from app.delivery.tts import build_friday_review_speech_script, deliver_friday_review_audio
from app.sub_agents.friday_review.sub_agents.synthesize.agent import FridayReviewCard, ScoredWin


def _base_card(**overrides) -> FridayReviewCard:
    defaults = dict(
        week_start=datetime.date(2026, 8, 24), wins=[], slipped_lines=[],
        one_adjustment=None, agenda_resolved_lines=[], agenda_stuck_lines=[],
        okr_progress_lines=[], career_narrative=None, daily_pulse_patterns=[],
        skill_distribution_summary=None, quiet_week=False, identity_asks_count=0,
    )
    defaults.update(overrides)
    return FridayReviewCard(**defaults)


def test_quiet_week_script_says_so():
    script = build_friday_review_speech_script(_base_card(quiet_week=True))
    assert "quiet" in script.lower()


def test_script_mentions_each_win():
    card = _base_card(
        wins=[
            ScoredWin(text="Shipped the migration", source_link=None, source_reference_key="w1",
                      moved_goal_title=None, already_logged=False, skill_category=None)
        ]
    )
    script = build_friday_review_speech_script(card)
    assert "Shipped the migration" in script


class _FakeDeliverer:
    enabled = True

    def __init__(self):
        self.uploaded = []

    def upload_audio(self, channel_id, audio_bytes, filename, title, thread_ts):
        self.uploaded.append((channel_id, filename, thread_ts))


def test_deliver_never_raises_when_tts_unavailable(monkeypatch):
    monkeypatch.setattr("app.delivery.tts.synthesize_speech", lambda text: None)
    deliverer = _FakeDeliverer()
    deliver_friday_review_audio(deliverer, _base_card(), "D1", "123.456")
    assert deliverer.uploaded == []  # no audio -> no upload, no crash
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/delivery/test_friday_review_tts.py -v`
Expected: FAIL (`build_friday_review_speech_script` doesn't exist — Task 5's stub only added `deliver_friday_review_audio`)

- [ ] **Step 3: Write the prompt**

```markdown
<!-- app/prompts/friday_review_audio_script.v1.md -->
# friday_review_audio_script.v1

You write the spoken script for the audio companion to a Friday
reflection. Someone is winding down their week and listening to this — it
should land as a warm, honest, evidence-grounded mirror, never a
performance review read aloud.

## What you receive

A JSON object mirroring the card's own fields: wins (already phrased and
sourced), slipped items, one_adjustment, agenda resolved/stuck lines, OKR
progress lines, career_narrative, skill_distribution_summary, quiet_week.

Everything here is already resolved — you do not add facts, invent a
trend, or restate a number differently than given.

## What you write

One tight, warm paragraph (roughly 30-90 seconds spoken, ~80-200 words).

- If `quiet_week` is true, say plainly that it was a quiet week and name
  whatever real evidence exists — no manufactured energy.
- Otherwise, open by naming the week's real wins in plain language, not a
  list read aloud.
- If `one_adjustment` is given, land it clearly and constructively, as
  something worth trying next week — never as criticism.
- If `career_narrative` is given, fold it in naturally near the end.
- Close warmly — this is meant to feel like a colleague reflecting the
  week back, not a status report.

## Rules

- Every claim must trace to a field you were given. Never invent a win, a
  number, or a trend not present in the input.
- No markdown, no URLs, no bullet points, no field labels.
- Neutral, evidence-first tone — no praise inflation, no scolding (same
  discipline the text card itself follows).
```

- [ ] **Step 4: Implement**

```python
# app/delivery/tts.py — replace the Task 5 stub with the real implementation

FRIDAY_REVIEW_NARRATOR_MODEL = "gemini-2.5-flash"
FRIDAY_REVIEW_NARRATOR_PROMPT_ID = "friday_review_audio_script.v1"
_FRIDAY_REVIEW_NARRATOR_PROMPT_PATH = (
    Path(__file__).resolve().parents[1] / "prompts" / "friday_review_audio_script.v1.md"
)


class FridayReviewNarratorOutput(BaseModel):
    script: str


friday_review_narrator_agent = Agent(
    name="friday_review_narrator",
    model=FRIDAY_REVIEW_NARRATOR_MODEL,
    instruction=_FRIDAY_REVIEW_NARRATOR_PROMPT_PATH.read_text(encoding="utf-8")
    + "\n\nFriday review data (JSON):\n{friday_review_narrator_input_json}",
    output_schema=FridayReviewNarratorOutput,
    output_key="friday_review_narrator_result",
    generate_content_config=genai_types.GenerateContentConfig(temperature=0.4),
)


def build_friday_review_speech_script(card) -> str:
    """Deterministic fallback — a template concatenation of the card's own
    fields, same role as build_dossier_speech_script."""
    if card.quiet_week:
        return "It was a quiet week — not much to report, but here's what's real."

    lines = ["Here's your Friday reflection."]
    if card.wins:
        lines.append("This week: " + "; ".join(w.text for w in card.wins) + ".")
    if card.slipped_lines:
        lines.append("A few things slipped: " + "; ".join(card.slipped_lines) + ".")
    if card.one_adjustment:
        lines.append(f"One thing worth trying next week: {card.one_adjustment}")
    if card.career_narrative:
        lines.append(card.career_narrative)

    return " ".join(lines)


def _friday_review_narrator_input(card) -> dict:
    return {
        "wins": [w.text for w in card.wins],
        "slipped_lines": card.slipped_lines,
        "one_adjustment": card.one_adjustment,
        "agenda_resolved_lines": card.agenda_resolved_lines,
        "agenda_stuck_lines": card.agenda_stuck_lines,
        "okr_progress_lines": card.okr_progress_lines,
        "career_narrative": card.career_narrative,
        "skill_distribution_summary": card.skill_distribution_summary,
        "quiet_week": card.quiet_week,
    }


def draft_friday_review_audio_script(card) -> str | None:
    try:
        state = run_agent_sync(
            friday_review_narrator_agent,
            {"friday_review_narrator_input_json": json.dumps(_friday_review_narrator_input(card))},
        )
        return FridayReviewNarratorOutput.model_validate(
            state["friday_review_narrator_result"]
        ).script
    except Exception:
        logger.exception(
            "friday review tts: LLM script draft failed, falling back to template"
        )
        return None


def deliver_friday_review_audio(deliverer, card, channel_id: str, thread_ts: str) -> None:
    if not deliverer.enabled:
        return
    script = draft_friday_review_audio_script(card) or build_friday_review_speech_script(card)
    audio = synthesize_speech(script)
    if audio is None:
        return
    try:
        deliverer.upload_audio(
            channel_id, audio_bytes=audio, filename="friday_review.mp3",
            title="Your Friday reflection (audio)", thread_ts=thread_ts,
        )
    except Exception:
        logger.exception("friday review tts: upload failed")
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/unit/delivery/test_friday_review_tts.py tests/unit/sub_agents/friday_review/sub_agents/deliver/test_agent.py -v`
Expected: PASS (both files — Task 5's deliver tests now exercise the real audio function via its `try/except`, never raising)

- [ ] **Step 6: Commit**

```bash
git add app/delivery/tts.py app/prompts/friday_review_audio_script.v1.md tests/unit/delivery/test_friday_review_tts.py
git commit -m "feat(friday-review): add audio companion"
```

---

### Task 10: Slack wiring — `/mentor review` slash command + `Confirm & log` interactive action

**Files:**
- Modify: `app/triggers/slack_socket_listener.py`

**Interfaces:**
- Consumes: `run_review_command` (Task 7), `confirm_and_log_ledger_items` (Task 5), `FRIDAY_REVIEW_CONFIRM_LOG_ACTION_ID` (Task 4).
- Produces: nothing new importable — wires existing functions into the live Socket Mode path, mirroring the existing `"prep"` slash-command branch and the feedback-button interactive branch exactly.

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/triggers/test_slack_socket_listener.py` (existing file — follow its own fixture/monkeypatch conventions for `_handle_slash_command`/`_handle_interactive`):

```python
def test_review_slash_command_is_accepted_and_scheduled(monkeypatch):
    # Mirror the existing test_prep_slash_command_is_accepted_and_scheduled
    # (or equivalent) in this file: build a payload with text="review",
    # monkeypatch resolve_owner_user_id to return a real id, monkeypatch
    # _schedule to capture its args, call _handle_slash_command, and assert
    # run_review_command (via _run_review_command_with_own_session) was
    # scheduled with the resolved owner_user_id.


def test_confirm_log_interactive_action_is_scheduled(monkeypatch):
    # Build an "interactive" payload with actions=[{"action_id":
    # FRIDAY_REVIEW_CONFIRM_LOG_ACTION_ID, "value": "<delivery_id>"}],
    # monkeypatch resolve_owner_user_id, monkeypatch _schedule, call
    # _handle_interactive, assert confirm_and_log_ledger_items gets
    # scheduled with the payload's delivery_id.
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/triggers/test_slack_socket_listener.py -v -k "review or confirm_log"`
Expected: FAIL (`"review"` currently rejected by `_handle_slash_command`'s allow-list; `FRIDAY_REVIEW_CONFIRM_LOG_ACTION_ID` not recognized by `_handle_interactive`)

- [ ] **Step 3: Implement**

```python
# app/triggers/slack_socket_listener.py — imports
from app.delivery.cards import (
    FEEDBACK_DOWN_ACTION_ID,
    FEEDBACK_UP_ACTION_ID,
    FRIDAY_REVIEW_CONFIRM_LOG_ACTION_ID,
    SHOW_FULL_SHORTLIST_ACTION_ID,
    parse_feedback_value,
)
from app.core.clock import SystemClock
from app.sub_agents.friday_review.sub_agents.deliver.agent import confirm_and_log_ledger_items
from app.triggers.slack_command import (
    parse_slash_command,
    resolve_owner_user_id,
    run_prep_command,
    run_pulse_command,
    run_review_command,
    run_shortlist_command,
)
```

```python
# app/triggers/slack_socket_listener.py — new helper, alongside
# _run_prep_command_with_own_session

def _run_review_command_with_own_session(
    session_factory: sessionmaker,
    owner_user_id: str,
) -> None:
    session = session_factory()
    try:
        run_review_command(session, owner_user_id)
    finally:
        session.close()


def _run_confirm_log_with_own_session(
    session_factory: sessionmaker,
    owner_user_id: str,
    delivery_id: str,
    dm_channel_id: str,
    message_ts: str | None,
) -> None:
    session = session_factory()
    try:
        scope = OwnerScope(owner_user_id=owner_user_id, session=session)
        created = confirm_and_log_ledger_items(scope, SystemClock(), delivery_id)
        deliverer = SlackDeliverer()
        if deliverer.enabled and dm_channel_id:
            text = (
                f"Logged {len(created)} item(s) to your ledger."
                if created
                else "Nothing new to log."
            )
            deliverer.post_thread_reply(
                owner_user_id, [{"type": "section", "text": {"type": "mrkdwn", "text": text}}],
                text, thread_ts=message_ts,
            )
    except Exception:
        logger.exception(
            "slack friday review confirm-and-log: failed for owner=%s delivery=%s",
            owner_user_id,
            delivery_id,
        )
    finally:
        session.close()
```

```python
# app/triggers/slack_socket_listener.py — _handle_slash_command, extend the allow-list
    if command_payload.text not in ("", "pulse", "prep", "review"):
        _reply(
            response_url,
            f"`{command_payload.text}` isn't supported yet — only "
            "`/mentor pulse`, `/mentor prep`, and `/mentor review`.",
        )
        return
```

```python
# app/triggers/slack_socket_listener.py — _handle_slash_command, new branch
    # (placed alongside the existing "prep" branch)
    if command_payload.text == "review":
        _reply(response_url, "On it — your Friday reflection is on its way.")
        _schedule(
            _run_review_command_with_own_session,
            session_factory,
            owner_user_id,
        )
        return
```

```python
# app/triggers/slack_socket_listener.py — _handle_interactive, extend the
# recognized action_id check and branch
def _handle_interactive(payload: dict, session_factory: sessionmaker) -> None:
    actions = payload.get("actions") or []
    action_id = actions[0].get("action_id") if actions else None
    if action_id not in (
        SHOW_FULL_SHORTLIST_ACTION_ID,
        FRIDAY_REVIEW_CONFIRM_LOG_ACTION_ID,
        *_FEEDBACK_SIGNAL_BY_ACTION_ID,
    ):
        return

    slack_user_id = payload.get("user", {}).get("id", "")
    dm_channel_id = payload.get("channel", {}).get("id", "")

    session: Session = session_factory()
    try:
        owner_user_id = resolve_owner_user_id(session, slack_user_id)
    finally:
        session.close()

    if owner_user_id is None:
        return

    if action_id in _FEEDBACK_SIGNAL_BY_ACTION_ID:
        delivery_id, item_type, item_id = parse_feedback_value(actions[0]["value"])
        _schedule(
            _run_feedback_command_with_own_session,
            session_factory, owner_user_id, delivery_id, item_id, item_type,
            _FEEDBACK_SIGNAL_BY_ACTION_ID[action_id],
        )
        return

    if action_id == FRIDAY_REVIEW_CONFIRM_LOG_ACTION_ID:
        delivery_id = actions[0]["value"]
        message_ts = payload.get("message", {}).get("ts")
        _schedule(
            _run_confirm_log_with_own_session,
            session_factory, owner_user_id, delivery_id, dm_channel_id, message_ts,
        )
        return

    if not dm_channel_id:
        return
    message_ts = payload.get("message", {}).get("ts")
    _schedule(
        _run_shortlist_command_with_own_session,
        session_factory, owner_user_id, message_ts,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/triggers/test_slack_socket_listener.py -v`
Expected: PASS (all tests, including the two new ones)

- [ ] **Step 5: Commit**

```bash
git add app/triggers/slack_socket_listener.py tests/unit/triggers/test_slack_socket_listener.py
git commit -m "feat(friday-review): wire /mentor review and Confirm & log into Socket Mode"
```

---

### Task 11: Cross-owner isolation + end-to-end flow test

**Files:**
- Create: `tests/unit/sub_agents/friday_review/test_cross_owner_isolation.py`
- Create: `tests/unit/sub_agents/friday_review/test_end_to_end.py`

**Interfaces:**
- Consumes: everything from Tasks 1-7. No new production code.

- [ ] **Step 1: Write the tests**

```python
# tests/unit/sub_agents/friday_review/test_cross_owner_isolation.py
"""Same shape as F1's and the identity design's own cross-owner isolation
tests: seed two owners with overlapping evidence, assert zero cross-owner
leakage in every gather query."""
import datetime
import uuid

from app.agenda.models import Accomplishment
from app.core.clock import FrozenClock
from app.core.models import Commitment, Goal, User
from app.core.scope import OwnerScope
from app.sub_agents.friday_review.sub_agents.gather.agent import gather_friday_review_context

NOW = datetime.datetime(2026, 8, 28, 16, 0, 0, tzinfo=datetime.UTC)


def test_zero_cross_owner_leakage(pg_session):
    owner_a = User(id=str(uuid.uuid4()), created_at=NOW)
    owner_b = User(id=str(uuid.uuid4()), created_at=NOW)
    pg_session.add_all([owner_a, owner_b])
    pg_session.commit()

    for owner in (owner_a, owner_b):
        pg_session.add_all(
            [
                Accomplishment(
                    id=str(uuid.uuid4()), owner_user_id=owner.id, description="win",
                    source_reference_key=f"acc-{owner.id}",
                    occurred_at=NOW - datetime.timedelta(days=1),
                ),
                Commitment(
                    id=str(uuid.uuid4()), owner_user_id=owner.id, promised_to_person_id=None,
                    description="slipped", source_reference_key=f"src-{owner.id}",
                    promised_at=NOW - datetime.timedelta(days=10),
                    due_at=NOW - datetime.timedelta(days=1), delivered_at=None, status="open",
                ),
                Goal(
                    id=str(uuid.uuid4()), owner_user_id=owner.id, title="goal",
                    status="active", external_ref=None, created_at=NOW,
                    goal_type="key_result", progress=0.5, current_value=5, target_value=10,
                ),
            ]
        )
    pg_session.commit()

    scope_a = OwnerScope(owner_user_id=owner_a.id, session=pg_session)
    context_a = gather_friday_review_context(scope_a, FrozenClock(at=NOW))

    assert all(w.source_reference_key == f"acc-{owner_a.id}" for w in context_a.wins)
    assert all(s.source_reference_key == f"src-{owner_a.id}" for s in context_a.slipped)
    assert all(g.title == "goal" for g in context_a.okr_progress)
    assert len(context_a.wins) == 1
    assert len(context_a.slipped) == 1
    assert len(context_a.okr_progress) == 1
```

```python
# tests/unit/sub_agents/friday_review/test_end_to_end.py
"""Gather -> synthesize (LLM stubbed via run_agent_sync monkeypatch,
matching every other sub-agent's own test convention) -> deliver -> a
real Confirm & log write-back, exercised as one flow."""
import datetime
import uuid

from app.agenda.models import Accomplishment
from app.core.clock import FrozenClock
from app.core.models import Commitment, FridayReviewDelivery, User, WorkItem
from app.core.scope import OwnerScope
from app.sub_agents.friday_review.agent import run_friday_review_flow
from app.sub_agents.friday_review.sub_agents.deliver.agent import confirm_and_log_ledger_items
from app.sub_agents.friday_review.sub_agents.synthesize.agent import (
    SkillClassificationOutput,
    SynthesizeOutput,
    WinOutput,
)

NOW = datetime.datetime(2026, 8, 28, 16, 0, 0, tzinfo=datetime.UTC)


class _FakeDeliverer:
    enabled = True

    def __init__(self):
        self.sent = []

    def deliver(self, user_slack_id, blocks, channel_id=None, thread_ts=None, dm_thread_ts=None):
        self.sent.append(blocks)
        return {"sent": True, "dm_ts": "789.012", "dm_channel": "D9"}


def test_full_flow_from_evidence_to_confirmed_ledger_entry(pg_session, monkeypatch):
    user = User(id=str(uuid.uuid4()), created_at=NOW, slack_user_id="U1")
    pg_session.add(user)
    work_item_id = str(uuid.uuid4())
    pg_session.add(
        WorkItem(
            id=work_item_id, owner_user_id=user.id, actor_reference_key="me",
            resolved_person_id=None, source="linear", external_id="LIN-1",
            title="Ship the migration", status="Done", url="https://linear.app/x/1",
            due_at=None, updated_at=NOW - datetime.timedelta(days=1),
        )
    )
    pg_session.commit()
    scope = OwnerScope(owner_user_id=user.id, session=pg_session)

    def _fake_run_agent_sync(llm_agent, state, kickoff_text=None):
        return {
            "friday_review_synthesize_result": SynthesizeOutput(
                wins=[
                    WinOutput(
                        source_reference_key=work_item_id,
                        phrased_text="Shipped the migration.",
                    )
                ],
                skill_classifications=[
                    SkillClassificationOutput(
                        source_reference_key=work_item_id,
                        skill_category="technical_execution",
                    )
                ],
            ).model_dump()
        }

    import app.core.adk_runner as adk_runner

    monkeypatch.setattr(adk_runner, "run_agent_sync", _fake_run_agent_sync)
    monkeypatch.setattr(
        "app.sub_agents.friday_review.agent.commit_batch", lambda *a, **k: None
    )

    deliverer = _FakeDeliverer()
    delivery = run_friday_review_flow(
        scope, FrozenClock(at=NOW), llm_agent=object(), deliverer=deliverer
    )

    assert delivery.sent_at is not None
    assert delivery.proposed_ledger_items == [
        {
            "description": "Shipped the migration.",
            "source_reference_key": work_item_id,
            "skill_category": "technical_execution",
        }
    ]

    created = confirm_and_log_ledger_items(scope, FrozenClock(at=NOW), delivery.id)
    assert len(created) == 1
    assert created[0].source_reference_key == work_item_id
    assert created[0].skill_category == "technical_execution"

    refreshed = pg_session.get(FridayReviewDelivery, delivery.id)
    assert refreshed.ledger_confirmed_at is not None
```

- [ ] **Step 2: Run tests to verify they fail, then pass**

Run: `uv run pytest tests/unit/sub_agents/friday_review/test_cross_owner_isolation.py tests/unit/sub_agents/friday_review/test_end_to_end.py -v`
Expected: both fail only if an earlier task has a real bug (these tests exercise already-implemented code, so a failure here means going back to fix the task that's actually broken, not new production code). Once green: PASS.

- [ ] **Step 3: Run the full test suite for this feature area**

Run: `uv run pytest tests/unit/sub_agents/friday_review tests/unit/delivery/test_friday_review_cards.py tests/unit/delivery/test_friday_review_tts.py tests/unit/triggers/test_friday_review_scheduler.py tests/unit/triggers/test_slack_socket_listener.py tests/unit/test_friday_review_delivery_model.py -v`
Expected: PASS, full green run across every F4 test file.

- [ ] **Step 4: Commit**

```bash
git add tests/unit/sub_agents/friday_review/test_cross_owner_isolation.py tests/unit/sub_agents/friday_review/test_end_to_end.py
git commit -m "test(friday-review): add cross-owner isolation and end-to-end flow tests"
```

---

## Deferred, per design spec §10 (not part of this plan)

- The `/ui` dashboard panel (F1's `build_*_a2ui_payload` pattern) for Friday reviews.
- Week-over-week OKR deltas (needs a new weekly-snapshot table).
- Disputing/correcting a listed win (`Accomplishment.status` + a "remove" affordance).
- A real per-user Mon-Fri calendar week (evidence window stays "trailing 7 days from now").
- Merging `friday_review_scheduler.py` with the other three poll loops.
