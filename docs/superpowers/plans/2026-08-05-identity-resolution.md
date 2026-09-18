# Identity Resolution (Layer 4) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the layer-4 identity resolution module (`app/identity/`) that turns raw
actor references from Calendar/Slack/Linear-Jira into a `Resolution` result
(`Resolved`/`Unconfirmed`/`Unattributed`), per
`docs/superpowers/specs/2026-08-05-identity-resolution-design.md` — **with per-user
isolation as a structural guarantee** (spec §11, added 2026-08-06). Mentor is a consumer
product with no organization layer; the user is the tenant, and every table is owned by
exactly one user.

**Architecture:** Pure, DB-free matcher functions (`normalize.py`, `matchers.py`) feed a
single write-owning module (`resolve.py`) that runs the tier ladder, applies the three
score gates, and manages the confirm/reject round-trip. `roster.py` is the only read of
the DB the matchers see (one atomic snapshot). `friday_batch.py` owns the deferred-ask
batching. An `OwnerScope` object (`app/core/scope.py`), constructed once per
request/invocation, is threaded through every function that touches the DB — no function
in `app/identity/` accepts a bare `Session`. Postgres (Cloud SQL) is the deployed
database; SQLite remains valid only for a single-user local install.

**Tech Stack:** Python 3.11, SQLAlchemy 2.x (ORM only, no raw SQL outside migrations),
Postgres via Docker Compose for dev/CI, Alembic for migrations, `psycopg` as the Postgres
driver, pytest + pytest-asyncio, PyYAML for the golden fixture file.

## Global Constraints

- Never call `datetime.now()` outside `app/core/clock.py` (`AGENT.md` §3). Tests freeze
  time via the clock, never via `unittest.mock.patch("datetime.now")`.
- All config through `app/core/config.py` (pydantic settings). No bare `os.environ` reads
  anywhere in `app/identity/`.
- Business logic never imports a vendor SDK (`AGENT.md` §3) — `app/identity/` has zero
  Slack/Google/Jira SDK imports.
- No LLM calls anywhere in this module — layers 1-4 are LLM-free (`AGENT.md` §2).
- `ruff check . && black --check . && pytest` must pass before any commit (`AGENT.md` §7
  Definition of done).
- Never log message bodies, ledger content, raw handles, or raw emails — log a
  `safe_log_key()`-hashed `reference_key` and `person_id` only (spec §7).
- The three gate constants (`AUTO_LINK_SCORE=0.95`, `ASK_SCORE=0.70`, `MARGIN_MIN=0.15`)
  and all other thresholds live only in `app/identity/config.py` — no inline literals.
- **Every domain table has a non-nullable `owner_user_id`, indexed first** (spec §5,
  §11.2). `UNIQUE(owner_user_id, source, external_id)`, never `UNIQUE(source,
  external_id)` — the latter lets two users' identically-numbered Slack IDs collapse onto
  one `Person` and leak one user's identity graph into the other's.
- **No function in `app/identity/` accepts a bare `Session`.** Every DB-touching function
  takes `scope: OwnerScope` as its first argument (spec §11.3). Enforced by a CI
  scope-leak test — no `session.query(...)`/`select(...)` on an owned model outside
  `OwnerScope`'s helpers.
- Postgres is the target for dev/CI (via Docker Compose), not SQLite — `jsonb` and
  `TIMESTAMP WITH TIME ZONE` columns must be exercised by real Postgres, not simulated by
  SQLite's looser typing (spec §11.1).
- Rosters are per-user by design. Person↔Person dedup — within one user's roster, and
  especially across users — is out of scope (spec §9, §11.2).

---

## File Structure

```
compose.yaml                NEW — local Postgres service for dev/CI

app/core/
  __init__.py          NEW — empty
  config.py             NEW — pydantic Settings: DATABASE_URL
  clock.py               NEW — Clock protocol + SystemClock + FrozenClock (test double)
  db.py                   NEW — SQLAlchemy engine/session factory, declarative Base,
                                 pool_pre_ping + small pool size for serverless
  scope.py                 NEW — OwnerScope: the per-request owner_user_id + session
                                 carrier and the only sanctioned way to query owned models
  models.py                  NEW — User, plus minimal L3 stand-in: Event, WorkItem,
                                    Message (id, owner_user_id, actor_reference_key,
                                    resolved_person_id — full normalization schema is a
                                    separate feature; these columns are what confirm()'s
                                    backfill needs and no more)

app/identity/
  __init__.py            NEW — empty
  config.py               NEW — gate + prior constants (spec §4.3, §6)
  types.py                 NEW — Resolved/Unconfirmed/Unattributed, MatchCandidate,
                                  RawReference
  models.py                 NEW — Person, Identity, UnresolvedReference, NotSameAs,
                                   PendingConfirmation, MergeLog, PersonRelationship,
                                   RosterVersion — every table owner-scoped (spec §5)
  normalize.py                NEW — normalize_handle, normalize_email,
                                     build_reference_key, safe_log_key
  roster.py                     NEW — load_snapshot(scope) -> RosterSnapshot
  matchers.py                    NEW — match_idp .. match_fuzzy_name,
                                        apply_context_priors, gate
  resolve.py                      NEW — resolve, resolve_for_surface, confirm, reject,
                                         unlink, attach_ask — all scope-first
  friday_batch.py                  NEW — build_batch, commit_batch — scope-first

app/storage/
  __init__.py           NEW — empty
  blobs.py                NEW — BlobStore protocol, LocalFilesystemBlobStore,
                                 GCSBlobStore — put/get/signed_url, owner-prefixed keys

migrations/
  env.py                NEW — Alembic env, imports app.core.db.Base + all model modules
  script.py.mako         NEW — Alembic template (framework default)
  versions/0001_initial.py NEW — one clean baseline migration (no prior prod data) for
                                  every table in app/core/models.py + app/identity/models.py

tests/unit/core/
  test_clock.py          NEW
  test_db.py               NEW
  test_models.py             NEW
  test_scope.py                NEW
tests/identity/
  __init__.py             NEW
  conftest.py               NEW — Postgres session fixture (via compose), scope-builder
                                   fixture, roster-builder fixture
  golden.yaml                 NEW — adversarial matcher fixture set (spec §8)
  test_normalize.py            NEW
  test_roster.py                 NEW
  test_matchers_golden.py         NEW
  test_gates_property.py           NEW
  test_resolve.py                   NEW
  test_confirmation.py               NEW
  test_friday_batch.py                NEW
  test_invariants.py                   NEW — no-dual-membership + cross-tenant isolation
                                              + constraint + scope-leak gates (spec §8,
                                              §11)
```

Dependency order for building (one-way, per spec §6/§11): `core/` (clock, config, db,
scope, models) → `identity/config.py` → `identity/types.py` → `identity/models.py` (+
migration) → `identity/normalize.py` → `identity/roster.py` → `identity/matchers.py` →
`identity/resolve.py` → `identity/friday_batch.py` → `storage/blobs.py` → cross-cutting
tests.

---

## Task 1: Core scaffolding — clock, config, db [COMPLETE — no changes]

Already implemented and reviewed (commits `a759a5a..e28c669`): `app/core/clock.py`,
`app/core/config.py`, `app/core/db.py`. Do not touch. Task 2 below amends `db.py` for
Postgres.

---

## Task 2: Postgres via Docker Compose + serverless pool tuning

**Files:**
- Create: `compose.yaml`
- Modify: `app/core/db.py:9-15` (the `get_engine` function from Task 1)
- Modify: `app/core/config.py` (the `Settings.database_url` default from Task 1)
- Test: `tests/unit/core/test_db.py`

**Interfaces:**
- Consumes: `Base`, `get_engine`, `get_session_factory` (Task 1, this task modifies
  `get_engine`'s body only — its signature `get_engine(url: str) -> Engine` is unchanged)
- Produces: `get_engine` now sets `pool_pre_ping=True` and a small pool
  (`pool_size=5, max_overflow=2`) for non-SQLite URLs; `Settings.database_url` default
  points at the Compose Postgres service for local dev.

- [ ] **Step 1: Write `compose.yaml`**

```yaml
# compose.yaml
services:
  db:
    image: postgres:16
    environment:
      POSTGRES_USER: mentor
      POSTGRES_PASSWORD: mentor_dev_only
      POSTGRES_DB: mentor
    ports:
      - "5432:5432"
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U mentor"]
      interval: 2s
      timeout: 2s
      retries: 15
```

- [ ] **Step 2: Start it and confirm it's healthy**

Run: `docker compose up -d db && docker compose ps`
Expected: `db` service status `healthy` within ~30s.

- [ ] **Step 3: Add the Postgres driver dependency**

Run: `uv add psycopg[binary]`

- [ ] **Step 4: Write the failing test**

```python
# tests/unit/core/test_db.py
from app.core.db import get_engine


def test_get_engine_enables_pool_pre_ping_for_postgres():
    engine = get_engine("postgresql+psycopg://mentor:mentor_dev_only@localhost:5432/mentor")
    assert engine.pool._pre_ping is True


def test_get_engine_sqlite_still_works_without_pool_tuning():
    engine = get_engine("sqlite:///:memory:")
    # SQLite's SingletonThreadPool/StaticPool has no _pre_ping concept the same way;
    # this just confirms get_engine doesn't raise for the single-user-local-install path
    assert engine is not None
```

- [ ] **Step 5: Run test to verify it fails**

Run: `uv run pytest tests/unit/core/test_db.py -v`
Expected: FAIL — `assert engine.pool._pre_ping is True` fails (currently False, the
default)

- [ ] **Step 6: Update `get_engine` and the config default**

```python
# app/core/db.py — replace get_engine's body
def get_engine(url: str) -> Engine:
    if url.startswith("sqlite"):
        return create_engine(url, connect_args={"check_same_thread": False})
    return create_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=2)
```

```python
# app/core/config.py — update the default
class Settings(BaseSettings):
    database_url: str = (
        "postgresql+psycopg://mentor:mentor_dev_only@localhost:5432/mentor"
    )

    class Config:
        env_file = ".env"
```

- [ ] **Step 7: Run test to verify it passes**

Run: `uv run pytest tests/unit/core/test_db.py -v`
Expected: PASS (2 tests)

- [ ] **Step 8: Commit**

```bash
git add compose.yaml app/core/db.py app/core/config.py pyproject.toml uv.lock tests/unit/core/test_db.py
git commit -m "feat(core): Postgres via Docker Compose, serverless pool tuning"
```

---

## Task 3: `app/core/scope.py` — OwnerScope, the isolation enforcement mechanism

**Files:**
- Create: `app/core/scope.py`
- Test: `tests/unit/core/test_scope.py`

**Interfaces:**
- Consumes: `Session` (SQLAlchemy)
- Produces: `OwnerScope(owner_user_id: str, session: Session)` (frozen dataclass) with
  methods `query(model) -> Select` (returns `select(model).where(model.owner_user_id ==
  self.owner_user_id)` — the *only* sanctioned way to build a `SELECT` against an owned
  model), `add(instance)` (stamps `instance.owner_user_id = self.owner_user_id` before
  `self.session.add(instance)`, raising `ValueError` if the instance already has a
  different `owner_user_id` set), and `commit()` (delegates to `self.session.commit()`).
  Importable from `app.core.scope`. This is the object every later task's `resolve.py`,
  `roster.py`, and `friday_batch.py` function takes as its first argument — read those
  tasks' code against this interface, not against a bare `Session`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/core/test_scope.py
import pytest
from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.orm import Session as SASession

from app.core.db import Base, get_engine, get_session_factory
from app.core.scope import OwnerScope


class _Widget(Base):
    """Test-only owned model — not part of the app schema."""

    __tablename__ = "test_widgets_for_scope"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String, index=True)
    name: Mapped[str] = mapped_column(String)


@pytest.fixture
def session() -> SASession:
    engine = get_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return get_session_factory(engine)()


def test_query_filters_to_the_scoped_owner(session):
    session.add_all(
        [
            _Widget(id="1", owner_user_id="user-a", name="a-widget"),
            _Widget(id="2", owner_user_id="user-b", name="b-widget"),
        ]
    )
    session.commit()

    scope = OwnerScope(owner_user_id="user-a", session=session)
    rows = session.execute(scope.query(_Widget)).scalars().all()

    assert [r.name for r in rows] == ["a-widget"]


def test_add_stamps_owner_user_id(session):
    scope = OwnerScope(owner_user_id="user-a", session=session)
    widget = _Widget(id="3", name="stamped")

    scope.add(widget)
    scope.commit()

    fetched = session.get(_Widget, "3")
    assert fetched.owner_user_id == "user-a"


def test_add_rejects_mismatched_owner_already_set(session):
    scope = OwnerScope(owner_user_id="user-a", session=session)
    widget = _Widget(id="4", owner_user_id="user-b", name="mismatch")

    with pytest.raises(ValueError):
        scope.add(widget)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/core/test_scope.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.core.scope'`

- [ ] **Step 3: Write the scope module**

```python
# app/core/scope.py
"""OwnerScope: the per-request owner_user_id + session carrier and the only
sanctioned way to query or write an owned model (design spec §11.3). No
function in app/identity/ may accept a bare Session — this object is what
they take instead. Enforced at CI time by a scope-leak test that greps for
bare session.query(...)/select(...) on owned models outside this module's
helpers."""
from dataclasses import dataclass

from sqlalchemy import Select, select
from sqlalchemy.orm import Session


@dataclass(frozen=True)
class OwnerScope:
    owner_user_id: str
    session: Session

    def query(self, model) -> Select:
        return select(model).where(model.owner_user_id == self.owner_user_id)

    def add(self, instance) -> None:
        existing = getattr(instance, "owner_user_id", None)
        if existing is not None and existing != self.owner_user_id:
            raise ValueError(
                f"instance owner_user_id={existing!r} does not match "
                f"scope owner_user_id={self.owner_user_id!r}"
            )
        instance.owner_user_id = self.owner_user_id
        self.session.add(instance)

    def commit(self) -> None:
        self.session.commit()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/core/test_scope.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add app/core/scope.py tests/unit/core/test_scope.py
git commit -m "feat(core): add OwnerScope, the per-user isolation enforcement mechanism"
```

---

## Task 4: `app/core/models.py` — User, plus minimal L3 stand-in tables

**Files:**
- Create: `app/core/models.py`
- Test: `tests/unit/core/test_models.py`

**Interfaces:**
- Consumes: `app.core.db.Base` (Task 1)
- Produces: `User` (`id: str` PK, `created_at: datetime` — everything else, auth/profile,
  belongs to a different feature) and `Event`, `WorkItem`, `Message` ORM classes, each
  with `id: str` (PK), `owner_user_id: str` (indexed first, FK to `User.id`),
  `actor_reference_key: str`, `resolved_person_id: str | None` — importable from
  `app.core.models`. Deliberately minimal: full L3 normalization (timestamps, provenance,
  dedup) is a separate feature; these columns are exactly what
  `identity.resolve.confirm()`/`unlink()` need to backfill (spec §4.7, §9), plus the
  `owner_user_id` every table in this system now requires (spec §11.2).

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/core/test_models.py
import datetime
import uuid

from sqlalchemy.orm import Session

from app.core.db import Base, get_engine, get_session_factory
from app.core.models import Event, Message, User, WorkItem


def test_event_round_trips_owner_and_actor_reference_key():
    engine = get_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session: Session = get_session_factory(engine)()

    user = User(id=str(uuid.uuid4()), created_at=datetime.datetime.now(datetime.UTC))
    session.add(user)
    session.commit()

    event = Event(
        id=str(uuid.uuid4()),
        owner_user_id=user.id,
        actor_reference_key="slack:U123",
        resolved_person_id=None,
    )
    session.add(event)
    session.commit()

    fetched = session.get(Event, event.id)
    assert fetched.owner_user_id == user.id
    assert fetched.actor_reference_key == "slack:U123"
    assert fetched.resolved_person_id is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/core/test_models.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.core.models'`

- [ ] **Step 3: Write the models**

```python
# app/core/models.py
import datetime

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    created_at: Mapped[datetime.datetime] = mapped_column()


class _OwnedAttributedRecordMixin:
    id: Mapped[str] = mapped_column(String, primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String, index=True)
    actor_reference_key: Mapped[str] = mapped_column(String, index=True)
    resolved_person_id: Mapped[str | None] = mapped_column(String, nullable=True)


class Event(_OwnedAttributedRecordMixin, Base):
    __tablename__ = "events"


class WorkItem(_OwnedAttributedRecordMixin, Base):
    __tablename__ = "work_items"


class Message(_OwnedAttributedRecordMixin, Base):
    __tablename__ = "messages"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/core/test_models.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/core/models.py tests/unit/core/test_models.py
git commit -m "feat(core): add User and owner-scoped L3 stand-in tables"
```

---

## Task 5: `app/identity/config.py` — gate and prior constants

**Files:**
- Create: `app/identity/__init__.py`
- Create: `app/identity/config.py`
- Test: `tests/identity/__init__.py`
- Test: `tests/identity/test_config.py`

**Interfaces:**
- Produces (all `float` unless noted): `AUTO_LINK_SCORE`, `ASK_SCORE`, `MARGIN_MIN`,
  `SINGLE_CANDIDATE_MARGIN`, `MIN_ROSTER_SIZE` (`int`), `CO_MEETING_PRIOR`,
  `SHARED_PROJECT_PRIOR`, `ACTIVE_CANDIDATES_PENALTY`, `FRIDAY_BATCH_CAP` (`int`),
  `MAX_ASKS_PER_CANDIDATE` (`int`), `KEY_VERSION` (`int`) — importable from
  `app.identity.config`. Nothing in this module concerns per-user isolation — gate
  thresholds are the same for every owner.

- [ ] **Step 1: Write the failing test**

```python
# tests/identity/test_config.py
from app.identity import config


def test_gate_ordering_is_sane():
    assert 0 < config.MARGIN_MIN < 1
    assert 0 < config.ASK_SCORE < config.AUTO_LINK_SCORE <= 1
    assert config.SINGLE_CANDIDATE_MARGIN == config.MARGIN_MIN * 2
    assert config.MIN_ROSTER_SIZE >= 2
    assert config.FRIDAY_BATCH_CAP == 7
    assert config.MAX_ASKS_PER_CANDIDATE == 1
    assert config.KEY_VERSION >= 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/identity/test_config.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.identity'`

- [ ] **Step 3: Write the config module**

```python
# app/identity/config.py
"""Tunable constants for identity resolution (spec §4.3, §4.5, §4.8, §6). No
literal threshold values may appear anywhere else in app/identity/."""

AUTO_LINK_SCORE = 0.95
ASK_SCORE = 0.70
MARGIN_MIN = 0.15
SINGLE_CANDIDATE_MARGIN = MARGIN_MIN * 2  # margin required vs. the runner-up
                                           # for the near-certain auto-link gate
MIN_ROSTER_SIZE = 2  # below this, auto-link never fires (cold-start guard)

CO_MEETING_PRIOR = 0.10
SHARED_PROJECT_PRIOR = 0.10
ACTIVE_CANDIDATES_PENALTY = -0.15

FRIDAY_BATCH_CAP = 7
MAX_ASKS_PER_CANDIDATE = 1  # an expired or rejected ask is never re-surfaced
PENDING_CONFIRMATION_TTL_DAYS = 7
KEY_VERSION = 1
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/identity/test_config.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/identity/__init__.py app/identity/config.py tests/identity
git commit -m "feat(identity): add gate and prior constants"
```

---

## Task 6: `app/identity/types.py` — Resolution result type, MatchCandidate, RawReference

**Files:**
- Create: `app/identity/types.py`
- Test: `tests/identity/test_types.py`

**Interfaces:**
- Produces: `RawReference(source: str, external_id: str | None, handle: str | None,
  email: str | None, display_name: str | None)` (frozen dataclass — what a connector
  hands to `resolve()`; note it deliberately carries no `owner_user_id` — that comes from
  the `OwnerScope` the caller already holds, not from the reference itself);
  `MatchCandidate(person_id: str, tier: int, score: float)` (frozen dataclass);
  `Resolved(person_id: str, tier: int, confidence: Literal["verified", "inferred"])`,
  `Unconfirmed(reference_key: str, top: MatchCandidate, margin: float)`,
  `Unattributed(reference_key: str, raw_handle: str | None)` (frozen dataclasses);
  `Resolution = Resolved | Unconfirmed | Unattributed` (type alias) — all importable from
  `app.identity.types`.

- [ ] **Step 1: Write the failing test**

```python
# tests/identity/test_types.py
from app.identity.types import (
    MatchCandidate,
    RawReference,
    Resolved,
    Unattributed,
    Unconfirmed,
)


def test_raw_reference_is_frozen_and_typed():
    ref = RawReference(
        source="slack",
        external_id="U123",
        handle="sbenali",
        email=None,
        display_name="Sarah Ben Youssef",
    )
    assert ref.source == "slack"
    assert ref.email is None


def test_resolved_is_frozen_and_typed():
    r = Resolved(person_id="p1", tier=1, confidence="verified")
    assert r.person_id == "p1"
    assert r.confidence == "verified"


def test_unconfirmed_carries_top_candidate_and_margin():
    top = MatchCandidate(person_id="p2", tier=4, score=0.8)
    u = Unconfirmed(reference_key="slack:handle:sbenali", top=top, margin=0.2)
    assert u.top.score == 0.8
    assert u.margin == 0.2


def test_unattributed_carries_raw_handle():
    u = Unattributed(reference_key="slack:handle:unknown", raw_handle="@unknown")
    assert u.raw_handle == "@unknown"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/identity/test_types.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.identity.types'`

- [ ] **Step 3: Write the types module**

```python
# app/identity/types.py
"""Resolution result type (spec §4.1) and the matcher candidate type (spec §6).
This is a leaf module: matchers.py, resolve.py, and any L5/L6 consumer import
from here without importing resolve.py (the writer module)."""

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class RawReference:
    source: str
    external_id: str | None
    handle: str | None
    email: str | None
    display_name: str | None


@dataclass(frozen=True)
class MatchCandidate:
    person_id: str
    tier: int
    score: float


@dataclass(frozen=True)
class Resolved:
    person_id: str
    tier: int
    confidence: Literal["verified", "inferred"]


@dataclass(frozen=True)
class Unconfirmed:
    reference_key: str
    top: MatchCandidate
    margin: float


@dataclass(frozen=True)
class Unattributed:
    reference_key: str
    raw_handle: str | None


Resolution = Resolved | Unconfirmed | Unattributed
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/identity/test_types.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add app/identity/types.py tests/identity/test_types.py
git commit -m "feat(identity): add Resolution result type, MatchCandidate, RawReference"
```

---

## Task 7: `app/identity/models.py` — owner-scoped schema, plus the initial migration

**Files:**
- Create: `app/identity/models.py`
- Create: `alembic.ini`
- Create: `migrations/env.py`
- Create: `migrations/script.py.mako`
- Test: `tests/identity/conftest.py`
- Test: `tests/identity/test_models.py`

**Interfaces:**
- Consumes: `app.core.db.Base` (Task 1), `app.core.models.User` (Task 4)
- Produces: `Person`, `Identity`, `UnresolvedReference`, `NotSameAs`,
  `PendingConfirmation`, `MergeLog`, `PersonRelationship`, `RosterVersion` ORM classes,
  importable from `app.identity.models`, matching spec §5 exactly — **every table carries
  `owner_user_id: str` (indexed first, FK to `users.id`)**, and uniqueness constraints are
  owner-scoped: `UNIQUE(owner_user_id, source, external_id)` on `Identity` (not
  `UNIQUE(source, external_id)` — spec §11.2, the single most important constraint in this
  schema), `UNIQUE(owner_user_id, reference_key)` on `Identity` and
  `UnresolvedReference`, `UNIQUE(owner_user_id, reference_key, candidate_person_id) WHERE
  status='pending'` on `PendingConfirmation`. `RosterVersion` is now **one row per owner**
  (`owner_user_id` is its primary key), not a `CHECK(id=1)` singleton — a single global
  counter would let one user's roster change invalidate another user's cached scores.
- Produces (test fixture): `tests/identity/conftest.py::pg_session` — a session against
  the Compose Postgres instance (Task 2) with all tables created, used by every later
  identity test. `tests/identity/conftest.py::make_scope` — a fixture factory that creates
  a `User` row and returns an `OwnerScope` bound to it, so later tasks' tests can call
  `make_scope(session)` for a ready-to-use scope instead of hand-rolling one.

- [ ] **Step 1: Write the failing test**

```python
# tests/identity/conftest.py
import datetime
import uuid

import pytest
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import Base, get_engine, get_session_factory
from app.core.models import User
from app.core.scope import OwnerScope


@pytest.fixture
def pg_session() -> Session:
    engine = get_engine(get_settings().database_url)
    Base.metadata.create_all(engine)
    session: Session = get_session_factory(engine)()
    yield session
    # tests are responsible for their own row cleanup via unique IDs per test;
    # a shared Postgres instance is not torn down between tests
    session.close()


@pytest.fixture
def db_session(pg_session) -> Session:
    """Alias kept for readability in tests migrated from the SQLite-era plan —
    identical fixture, Postgres-backed now (spec §11.1)."""
    return pg_session


@pytest.fixture
def make_scope(pg_session):
    def _make(owner_user_id: str | None = None) -> OwnerScope:
        uid = owner_user_id or str(uuid.uuid4())
        pg_session.add(User(id=uid, created_at=datetime.datetime.now(datetime.UTC)))
        pg_session.commit()
        return OwnerScope(owner_user_id=uid, session=pg_session)

    return _make
```

```python
# tests/identity/test_models.py
import datetime
import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from app.identity.models import Identity, Person


def _make_person(session, scope, **overrides):
    person = Person(
        id=str(uuid.uuid4()),
        canonical_name=overrides.get("canonical_name", "Sarah Ben Youssef"),
        primary_email=overrides.get("primary_email"),
        is_self=overrides.get("is_self", False),
        is_active=overrides.get("is_active", True),
        roster_source=overrides.get("roster_source"),
        created_at=datetime.datetime.now(datetime.UTC),
    )
    scope.add(person)
    scope.commit()
    return person


def test_person_primary_email_unique_per_owner_when_set(db_session, make_scope):
    scope = make_scope()
    _make_person(db_session, scope, primary_email="sarah@acme.com")
    dup = Person(
        id=str(uuid.uuid4()),
        owner_user_id=scope.owner_user_id,
        canonical_name="Someone Else",
        primary_email="sarah@acme.com",
        is_self=False,
        is_active=True,
        created_at=datetime.datetime.now(datetime.UTC),
    )
    db_session.add(dup)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_person_primary_email_can_repeat_across_different_owners(db_session, make_scope):
    scope_a = make_scope()
    scope_b = make_scope()
    _make_person(db_session, scope_a, primary_email="sarah@acme.com")
    # same email, different owner — must succeed, these are unrelated people
    _make_person(db_session, scope_b, primary_email="sarah@acme.com")


def test_identity_source_external_id_unique_per_owner(db_session, make_scope):
    scope = make_scope()
    person = _make_person(db_session, scope)
    now = datetime.datetime.now(datetime.UTC)
    scope.add(
        Identity(
            id=str(uuid.uuid4()),
            person_id=person.id,
            source="slack",
            external_id="U123",
            reference_key="slack:U123",
            key_version=1,
            tier=1,
            confidence="verified",
            verified_by="auto",
            first_seen=now,
            last_seen=now,
        )
    )
    scope.commit()
    dup = Identity(
        id=str(uuid.uuid4()),
        owner_user_id=scope.owner_user_id,
        person_id=person.id,
        source="slack",
        external_id="U123",
        reference_key="slack:U123-other",
        key_version=1,
        tier=1,
        confidence="verified",
        verified_by="auto",
        first_seen=now,
        last_seen=now,
    )
    db_session.add(dup)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_identity_source_external_id_can_repeat_across_different_owners(
    db_session, make_scope
):
    # the constraint test the multi-tenancy revision exists for: the same
    # (source, external_id) — e.g. two users whose Slack workspaces both
    # assigned user ID "U123" — must NOT collapse onto one Identity/Person
    scope_a = make_scope()
    scope_b = make_scope()
    person_a = _make_person(db_session, scope_a)
    person_b = _make_person(db_session, scope_b)
    now = datetime.datetime.now(datetime.UTC)

    scope_a.add(
        Identity(
            id=str(uuid.uuid4()), person_id=person_a.id, source="slack",
            external_id="U123", reference_key="slack:U123", key_version=1, tier=1,
            confidence="verified", verified_by="auto", first_seen=now, last_seen=now,
        )
    )
    scope_a.commit()
    scope_b.add(
        Identity(
            id=str(uuid.uuid4()), person_id=person_b.id, source="slack",
            external_id="U123", reference_key="slack:U123", key_version=1, tier=1,
            confidence="verified", verified_by="auto", first_seen=now, last_seen=now,
        )
    )
    scope_b.commit()  # must not raise
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose up -d db && uv run pytest tests/identity/test_models.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.identity.models'`

- [ ] **Step 3: Write the models**

```python
# app/identity/models.py
"""Owner-scoped schema for identity resolution (design spec §5, §11.2). Every
table carries owner_user_id, indexed first. Postgres-specific types (JSONB,
TIMESTAMP WITH TIME ZONE) per spec §11.1 — this schema targets Postgres; SQLite
remains valid only for the single-user local install path."""
import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class Person(Base):
    __tablename__ = "persons"
    __table_args__ = (
        Index("ix_person_owner", "owner_user_id"),
        Index(
            "uq_person_owner_primary_email",
            "owner_user_id",
            "primary_email",
            unique=True,
            sqlite_where=text("primary_email IS NOT NULL"),
            postgresql_where=text("primary_email IS NOT NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"))
    canonical_name: Mapped[str] = mapped_column(String)
    primary_email: Mapped[str | None] = mapped_column(String, nullable=True)
    is_self: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    roster_source: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))


class Identity(Base):
    __tablename__ = "identities"
    __table_args__ = (
        Index("ix_identity_owner", "owner_user_id"),
        UniqueConstraint(
            "owner_user_id", "reference_key", name="uq_identity_owner_reference_key"
        ),
        Index(
            "uq_identity_owner_source_external_id",
            "owner_user_id",
            "source",
            "external_id",
            unique=True,
            sqlite_where=text("external_id IS NOT NULL"),
            postgresql_where=text("external_id IS NOT NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"))
    person_id: Mapped[str] = mapped_column(String, ForeignKey("persons.id"))
    source: Mapped[str] = mapped_column(String)  # "calendar" | "slack" | "linear" | "jira"
    external_id: Mapped[str | None] = mapped_column(String, nullable=True)
    reference_key: Mapped[str] = mapped_column(String)
    key_version: Mapped[int] = mapped_column(Integer)
    tier: Mapped[int] = mapped_column(Integer)
    confidence: Mapped[str] = mapped_column(String)  # "verified" | "inferred"
    verified_by: Mapped[str] = mapped_column(String)  # "auto" | "user_confirmed"
    handle: Mapped[str | None] = mapped_column(String, nullable=True)
    email: Mapped[str | None] = mapped_column(String, nullable=True)
    display_name: Mapped[str | None] = mapped_column(String, nullable=True)
    provenance: Mapped[dict] = mapped_column(JSONB, default=dict)
    first_seen: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))
    last_seen: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))


class UnresolvedReference(Base):
    __tablename__ = "unresolved_references"
    __table_args__ = (
        Index("ix_unresolved_reference_owner", "owner_user_id"),
        UniqueConstraint(
            "owner_user_id", "reference_key", name="uq_unresolved_owner_reference_key"
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"))
    reference_key: Mapped[str] = mapped_column(String)
    key_version: Mapped[int] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(String)
    external_id: Mapped[str | None] = mapped_column(String, nullable=True)
    handle: Mapped[str | None] = mapped_column(String, nullable=True)
    email: Mapped[str | None] = mapped_column(String, nullable=True)
    display_name: Mapped[str | None] = mapped_column(String, nullable=True)
    candidates: Mapped[list] = mapped_column(JSONB, default=list)
    best_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    margin: Mapped[float | None] = mapped_column(Float, nullable=True)
    occurrence_count: Mapped[int] = mapped_column(Integer, default=0)
    distinct_day_count: Mapped[int] = mapped_column(Integer, default=0)
    last_seen_date: Mapped[datetime.date | None] = mapped_column(nullable=True)
    ask_count: Mapped[int] = mapped_column(Integer, default=0)
    scored_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))
    roster_version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String, default="pending")
    first_seen: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))
    last_seen: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))


class NotSameAs(Base):
    __tablename__ = "not_same_as"
    __table_args__ = (
        Index("ix_not_same_as_owner_key_person", "owner_user_id", "reference_key", "person_id"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"))
    reference_key: Mapped[str] = mapped_column(String)
    person_id: Mapped[str] = mapped_column(String, ForeignKey("persons.id"))
    rejected_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))
    actor: Mapped[str] = mapped_column(String, default="user")


class PendingConfirmation(Base):
    __tablename__ = "pending_confirmations"
    __table_args__ = (
        Index("ix_pending_confirmation_owner", "owner_user_id"),
        Index(
            "uq_pending_confirmation_owner_key_candidate",
            "owner_user_id",
            "reference_key",
            "candidate_person_id",
            unique=True,
            sqlite_where=text("status = 'pending'"),
            postgresql_where=text("status = 'pending'"),
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"))
    reference_key: Mapped[str] = mapped_column(String, index=True)  # not an FK, see spec §5
    candidate_person_id: Mapped[str] = mapped_column(String, ForeignKey("persons.id"))
    candidate_score: Mapped[float] = mapped_column(Float)
    surface: Mapped[str] = mapped_column(String)  # "card" | "friday_batch"
    card_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="pending")
    asked_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))
    answered_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    expires_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))


class MergeLog(Base):
    __tablename__ = "merge_log"
    __table_args__ = (Index("ix_merge_log_owner", "owner_user_id"),)

    id: Mapped[str] = mapped_column(String, primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"))
    person_id: Mapped[str] = mapped_column(String, ForeignKey("persons.id"))
    reference_key: Mapped[str] = mapped_column(String, index=True)
    action: Mapped[str] = mapped_column(String)
    prev_person_id: Mapped[str | None] = mapped_column(String, nullable=True)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    tier: Mapped[int | None] = mapped_column(Integer, nullable=True)
    confidence: Mapped[str | None] = mapped_column(String, nullable=True)
    actor: Mapped[str] = mapped_column(String)  # "system" | "user"
    reason: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))


class PersonRelationship(Base):
    __tablename__ = "person_relationships"
    __table_args__ = (
        UniqueConstraint(
            "owner_user_id",
            "person_id_a",
            "person_id_b",
            name="uq_person_relationship_owner_pair",
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"))
    person_id_a: Mapped[str] = mapped_column(String, ForeignKey("persons.id"))
    person_id_b: Mapped[str] = mapped_column(String, ForeignKey("persons.id"))
    co_meeting_count: Mapped[int] = mapped_column(Integer, default=0)
    shared_project_count: Mapped[int] = mapped_column(Integer, default=0)
    last_contact_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    computed_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))


class RosterVersion(Base):
    """One row PER OWNER (owner_user_id is the primary key) — not a CHECK(id=1)
    singleton. A single global counter would let one user's roster change
    invalidate every other user's cached UnresolvedReference scores."""

    __tablename__ = "roster_version"

    owner_user_id: Mapped[str] = mapped_column(
        String, ForeignKey("users.id"), primary_key=True
    )
    version: Mapped[int] = mapped_column(Integer, default=0)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/identity/test_models.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Set up Alembic and generate the initial migration**

```bash
uv run alembic init migrations
```

Replace the generated `migrations/env.py` imports and `target_metadata` so it picks up
every model module, and point it at Postgres via `Settings`:

```python
# migrations/env.py — edit the top of the file
from app.core.config import get_settings
from app.core.db import Base
import app.core.models  # noqa: F401 — registers User/Event/WorkItem/Message on Base
import app.identity.models  # noqa: F401 — registers identity tables on Base

target_metadata = Base.metadata
```

Point `sqlalchemy.url` at `get_settings().database_url` by replacing the
Alembic-generated `run_migrations_online`'s static engine construction with
`get_engine(get_settings().database_url)` — follow the Alembic-generated env.py's existing
structure and substitute this one line.

Since there is no production data yet, this is one clean baseline migration, not an
additive patch (spec §12):

```bash
docker compose up -d db
uv run alembic revision --autogenerate -m "initial owner-scoped schema"
```

Expected: a new file under `migrations/versions/` creating `users`, `persons`,
`identities`, `unresolved_references`, `not_same_as`, `pending_confirmations`,
`merge_log`, `person_relationships`, `roster_version`, `events`, `work_items`, `messages`
— inspect it and confirm every table except `users` has an `owner_user_id` column with an
index, and that `identities` has the owner-scoped unique constraints from Step 3, not
plain `UNIQUE(source, external_id)`.

```bash
uv run alembic upgrade head
```

Expected: exits 0 against the Compose Postgres instance.

- [ ] **Step 6: Commit**

```bash
git add app/identity/models.py alembic.ini migrations tests/identity/conftest.py tests/identity/test_models.py
git commit -m "feat(identity): add owner-scoped schema and initial Postgres migration"
```

---

## Task 8: `app/identity/normalize.py` — conservative-only key construction

**Files:**
- Create: `app/identity/normalize.py`
- Test: `tests/identity/test_normalize.py`

**Interfaces:**
- Produces: `normalize_handle(raw: str) -> str`, `normalize_email(raw: str) -> str`,
  `build_reference_key(source: str, external_id: str | None, handle: str | None) -> str`,
  `safe_log_key(reference_key: str) -> str` — importable from `app.identity.normalize`.
  `build_reference_key` raises `ValueError` if neither `external_id` nor `handle` is
  given, and always produces a key starting with a known source prefix (`"calendar:"`,
  `"slack:"`, `"linear:"`, `"jira:"`). `safe_log_key` implements the logging rule from
  design spec §7 and is the only spelling any log line in `app.identity` may use for a
  `reference_key`. **No owner concept here** — `reference_key` is unique per-owner at the
  DB layer (Task 7's constraints), not by encoding the owner into the string itself (spec
  §4.2).

- [ ] **Step 1: Write the failing test**

```python
# tests/identity/test_normalize.py
import pytest

from app.identity.normalize import build_reference_key, normalize_email, normalize_handle


def test_normalize_handle_casefolds_and_trims():
    assert normalize_handle("  Sbenali  ") == "sbenali"


def test_normalize_handle_does_not_strip_digit_suffix():
    # sbenali and sbenali2 are frequently different people — the key must never
    # silently conflate them. Digit-suffix stripping belongs in match_handle_heuristic.
    assert normalize_handle("sbenali2") == "sbenali2"
    assert normalize_handle("sbenali2") != normalize_handle("sbenali")


def test_normalize_handle_unicode_nfkc_folds():
    assert normalize_handle("Şenol") == normalize_handle("şenol")


def test_normalize_email_lowercases_and_strips_plus_tag():
    assert normalize_email("Sarah.BenYoussef+linear@ACME.com") == (
        "sarah.benyoussef@acme.com"
    )


def test_normalize_email_does_not_fold_alias_domains():
    # acme.io -> acme.com folding is matcher evidence (tier 2), not a key transform
    assert normalize_email("s.benyoussef@acme.io") == "s.benyoussef@acme.io"


def test_build_reference_key_prefers_external_id():
    key = build_reference_key(source="slack", external_id="U123", handle="sbenali")
    assert key == "slack:U123"


def test_build_reference_key_falls_back_to_handle():
    key = build_reference_key(source="slack", external_id=None, handle="Sbenali")
    assert key == "slack:handle:sbenali"


def test_build_reference_key_requires_something():
    with pytest.raises(ValueError):
        build_reference_key(source="slack", external_id=None, handle=None)


def test_build_reference_key_rejects_unknown_source():
    with pytest.raises(ValueError):
        build_reference_key(source="carrier_pigeon", external_id="1", handle=None)


def test_safe_log_key_passes_through_external_id_keys():
    from app.identity.normalize import safe_log_key

    assert safe_log_key("slack:U123") == "slack:U123"


def test_safe_log_key_hashes_handle_portion():
    from app.identity.normalize import safe_log_key

    logged = safe_log_key("slack:handle:sbenali")
    assert logged.startswith("slack:handle:")
    assert "sbenali" not in logged
    assert len(logged.split(":")[-1]) == 8
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/identity/test_normalize.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.identity.normalize'`

- [ ] **Step 3: Write the normalize module**

```python
# app/identity/normalize.py
"""Conservative-only key construction (design spec §4.4). The key normalizes,
the matchers guess: nothing here may conflate two different people. Digit-suffix
stripping, alias-domain folding, and fuzzy comparison belong in matchers.py."""
import hashlib
import unicodedata

KNOWN_SOURCES = frozenset({"calendar", "slack", "linear", "jira"})


def normalize_handle(raw: str) -> str:
    return unicodedata.normalize("NFKC", raw).strip().casefold()


def normalize_email(raw: str) -> str:
    email = unicodedata.normalize("NFKC", raw).strip().casefold()
    local, _, domain = email.partition("@")
    local = local.split("+", 1)[0]
    return f"{local}@{domain}"


def build_reference_key(
    source: str, external_id: str | None, handle: str | None
) -> str:
    if source not in KNOWN_SOURCES:
        raise ValueError(f"unknown source: {source!r}")
    if external_id:
        return f"{source}:{external_id}"
    if handle:
        return f"{source}:handle:{normalize_handle(handle)}"
    raise ValueError("build_reference_key requires external_id or handle")


def safe_log_key(reference_key: str) -> str:
    """The logging rule from design spec §7: an external_id-keyed reference_key
    is an opaque provider ID and safe to log as-is. A handle-keyed reference_key
    has its handle portion hashed before it may reach any log line."""
    prefix, _, rest = reference_key.partition(":")
    if rest.startswith("handle:"):
        handle_part = rest[len("handle:") :]
        digest = hashlib.sha256(handle_part.encode("utf-8")).hexdigest()[:8]
        return f"{prefix}:handle:{digest}"
    return reference_key
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/identity/test_normalize.py -v`
Expected: PASS (10 tests)

- [ ] **Step 5: Commit**

```bash
git add app/identity/normalize.py tests/identity/test_normalize.py
git commit -m "feat(identity): add conservative-only normalize and reference_key"
```

---

## Task 9: `app/identity/roster.py` — atomic, owner-scoped snapshot read

**Files:**
- Create: `app/identity/roster.py`
- Test: `tests/identity/test_roster.py`

**Interfaces:**
- Consumes: `Person`, `NotSameAs`, `PersonRelationship`, `RosterVersion` (Task 7);
  `OwnerScope` (Task 3)
- Produces: `RosterSnapshot(people: list[Person], not_same_as: set[tuple[str, str]],
  relationships: dict[tuple[str, str], PersonRelationship], roster_version: int)` (frozen
  dataclass), and `load_snapshot(scope: OwnerScope) -> RosterSnapshot` — importable from
  `app.identity.roster`. **Takes `scope`, never a bare `Session`** (Global Constraints).
  Every query inside filters to `scope.owner_user_id` via `scope.query(...)`. `not_same_as`
  is keyed by `(reference_key, person_id)` for O(1) filtering in `matchers.py` — no
  `owner_user_id` in that tuple since the whole snapshot is already one owner's data.

- [ ] **Step 1: Write the failing test**

```python
# tests/identity/test_roster.py
import datetime
import uuid

from app.identity.models import NotSameAs, Person, RosterVersion
from app.identity.roster import load_snapshot


def test_load_snapshot_reads_version_and_data_atomically(db_session, make_scope):
    scope = make_scope()
    db_session.add(RosterVersion(owner_user_id=scope.owner_user_id, version=3))
    person = Person(
        id=str(uuid.uuid4()),
        owner_user_id=scope.owner_user_id,
        canonical_name="Sarah Ben Youssef",
        primary_email="sarah@acme.com",
        is_self=False,
        is_active=True,
        created_at=datetime.datetime.now(datetime.UTC),
    )
    db_session.add(person)
    db_session.add(
        NotSameAs(
            id=str(uuid.uuid4()),
            owner_user_id=scope.owner_user_id,
            reference_key="slack:handle:sbenali",
            person_id=person.id,
            rejected_at=datetime.datetime.now(datetime.UTC),
            actor="user",
        )
    )
    db_session.commit()

    snapshot = load_snapshot(scope)

    assert snapshot.roster_version == 3
    assert len(snapshot.people) == 1
    assert snapshot.people[0].id == person.id
    assert ("slack:handle:sbenali", person.id) in snapshot.not_same_as


def test_load_snapshot_defaults_version_to_zero_when_unset(db_session, make_scope):
    scope = make_scope()
    snapshot = load_snapshot(scope)
    assert snapshot.roster_version == 0
    assert snapshot.people == []


def test_load_snapshot_never_returns_another_owners_people(db_session, make_scope):
    scope_a = make_scope()
    scope_b = make_scope()
    db_session.add(
        Person(
            id=str(uuid.uuid4()), owner_user_id=scope_a.owner_user_id,
            canonical_name="Owner A's Person", primary_email=None,
            is_self=False, is_active=True,
            created_at=datetime.datetime.now(datetime.UTC),
        )
    )
    db_session.commit()

    snapshot_b = load_snapshot(scope_b)

    assert snapshot_b.people == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/identity/test_roster.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.identity.roster'`

- [ ] **Step 3: Write the roster module**

```python
# app/identity/roster.py
"""The only DB read matchers see (design spec §6). One atomic snapshot so the
stamped roster_version always matches the data scored against it. Every read
goes through OwnerScope (spec §11.3) — no function here accepts a bare
Session."""
from dataclasses import dataclass

from app.core.scope import OwnerScope
from app.identity.models import NotSameAs, Person, PersonRelationship, RosterVersion


@dataclass(frozen=True)
class RosterSnapshot:
    people: list[Person]
    not_same_as: set[tuple[str, str]]
    relationships: dict[tuple[str, str], PersonRelationship]
    roster_version: int


def load_snapshot(scope: OwnerScope) -> RosterSnapshot:
    version_row = scope.session.get(RosterVersion, scope.owner_user_id)
    roster_version = version_row.version if version_row else 0

    people = list(scope.session.execute(scope.query(Person)).scalars().all())

    not_same_as = {
        (row.reference_key, row.person_id)
        for row in scope.session.execute(scope.query(NotSameAs)).scalars().all()
    }

    relationships = {
        (row.person_id_a, row.person_id_b): row
        for row in scope.session.execute(scope.query(PersonRelationship)).scalars().all()
    }

    return RosterSnapshot(
        people=people,
        not_same_as=not_same_as,
        relationships=relationships,
        roster_version=roster_version,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/identity/test_roster.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add app/identity/roster.py tests/identity/test_roster.py
git commit -m "feat(identity): add atomic, owner-scoped roster snapshot reader"
```

---

## Task 10: `app/identity/matchers.py` — the tier ladder, pure and DB-free

**Files:**
- Create: `app/identity/matchers.py`
- Create: `tests/identity/golden.yaml`
- Test: `tests/identity/test_matchers_golden.py`
- Test: `tests/identity/test_gates_property.py`

**Interfaces:**
- Consumes: `RawReference`, `MatchCandidate` (Task 6); `RosterSnapshot` (Task 9); `Person`
  (Task 7); gate/prior constants (Task 5); `normalize_handle`, `normalize_email` (Task 8)
- Produces: `match_idp(ref, roster) -> MatchCandidate | None`, `match_primary_email(ref,
  roster) -> MatchCandidate | None`, `match_alias_email(ref, roster) -> MatchCandidate |
  None`, `match_exact_name(ref, roster) -> MatchCandidate | None`,
  `match_handle_heuristic(ref, roster) -> list[MatchCandidate]`, `match_fuzzy_name(ref,
  roster) -> list[MatchCandidate]`, `apply_context_priors(candidates, ref, roster) ->
  list[MatchCandidate]` — all importable from `app.identity.matchers`. Also
  `gate(score: float, margin: float, roster_size: int) -> Literal["auto_link", "ask",
  "none"]`, the pure decision function Task 11 calls after ranking. **This module has no
  owner concept and takes no `OwnerScope`** — `roster: RosterSnapshot` is already filtered
  to one owner by `roster.py` (Task 9), so matching logic is identical regardless of
  tenancy.

Tier 0 (`match_idp`) returns `None` unconditionally in this pass — no IdP connector exists
(spec §9); the function exists so `resolve.py` can call it uniformly and it starts
returning real matches the day an IdP connector lands, with zero change to `resolve.py`.

- [ ] **Step 1: Write the adversarial golden fixture set**

```yaml
# tests/identity/golden.yaml
# Adversarial by construction (design spec §8). Two gates when this file is
# scored by test_matchers_golden.py: precision >= 0.99, false_merge_count == 0.
# One owner's roster only — golden.yaml tests matching logic, not tenancy;
# tenancy is tested separately in test_invariants.py.
roster:
  - id: p_sarah
    canonical_name: "Sarah Ben Youssef"
    primary_email: "sarah.benyoussef@acme.com"
    alias_emails: ["s.benyoussef@acme.io"]
    is_active: true
  - id: p_sarah2
    canonical_name: "Sarah Ben Ali"
    primary_email: "sarah.benali@acme.com"
    is_active: true
  - id: p_mohamed1
    canonical_name: "Mohamed Amine"
    primary_email: "mohamed.amine@acme.com"
    is_active: true
  - id: p_mohamed2
    canonical_name: "Mohamed Amine"  # duplicate display name, on purpose
    primary_email: "mohamed.amine2@acme.com"
    is_active: true
  - id: p_contractor
    canonical_name: "Jamie Ext"
    primary_email: "jamie@vendor.com"
    is_active: true

cases:
  - name: primary_email_exact_match
    reference: {source: slack, external_id: "U1", handle: "sarah.dev", email: "sarah.benyoussef@acme.com"}
    expect: {kind: resolved, person_id: p_sarah, confidence: verified}

  - name: alias_domain_email_match
    reference: {source: linear, external_id: "L1", handle: null, email: "s.benyoussef+linear@acme.io"}
    expect: {kind: resolved, person_id: p_sarah, confidence: verified}

  - name: duplicate_display_name_never_auto_links
    reference: {source: calendar, external_id: null, handle: null, display_name: "Mohamed Amine"}
    expect: {kind: unattributed}   # margin=0 between p_mohamed1 and p_mohamed2

  - name: unique_exact_name_asks_not_auto_links
    reference: {source: calendar, external_id: null, handle: null, display_name: "Sarah Ben Youssef"}
    expect: {kind: unconfirmed, top_person_id: p_sarah}   # tier 3 never auto-links

  - name: handle_digit_suffix_is_a_different_person_by_default
    reference: {source: slack, external_id: "U9", handle: "sbenali2"}
    # sbenali (no such handle in this roster) vs sbenali2 — must not silently
    # collide via the reference_key; scored as its own candidate set
    expect: {kind: unattributed}

  - name: contractor_ext_handle_does_not_false_merge_to_similar_name
    reference: {source: slack, external_id: "U10", handle: "jamie_ext"}
    expect: {kind: unconfirmed, top_person_id: p_contractor}

  - name: unicode_near_duplicate_folds_via_nfkc
    reference: {source: calendar, external_id: null, handle: null, display_name: "Şarah Ben Youssef"}
    expect: {kind: unconfirmed, top_person_id: p_sarah}

  - name: handle_matches_wrong_persons_name_stays_unattributed
    # "sbenali" as a handle superficially resembles "Sarah Ben Ali" by prefix,
    # but there is no real evidence — must not false-merge
    reference: {source: slack, external_id: "U11", handle: "totallyunrelated"}
    expect: {kind: unattributed}
```

```python
# tests/identity/test_matchers_golden.py
import datetime

import yaml

from app.identity import matchers
from app.identity.models import Person
from app.identity.roster import RosterSnapshot
from app.identity.types import RawReference


def _load_golden():
    with open("tests/identity/golden.yaml") as f:
        return yaml.safe_load(f)


def _build_roster(golden) -> RosterSnapshot:
    people = [
        Person(
            id=p["id"],
            owner_user_id="golden-set-owner",
            canonical_name=p["canonical_name"],
            primary_email=p.get("primary_email"),
            is_self=False,
            is_active=p.get("is_active", True),
            created_at=datetime.datetime.now(datetime.UTC),
        )
        for p in golden["roster"]
    ]
    return RosterSnapshot(
        people=people, not_same_as=set(), relationships={}, roster_version=1
    )


def _resolve_via_matchers(ref: RawReference, roster: RosterSnapshot):
    """Runs the full tier ladder + gates, mirroring what resolve.py will do,
    without touching the DB — this is what makes the golden set DB-free."""
    for matcher in (matchers.match_idp, matchers.match_primary_email, matchers.match_alias_email):
        hit = matcher(ref, roster)
        if hit is not None:
            return ("resolved", hit.person_id, "verified")

    candidates: list = []
    exact_name = matchers.match_exact_name(ref, roster)
    if exact_name is not None:
        candidates.append(exact_name)
    candidates.extend(matchers.match_handle_heuristic(ref, roster))
    candidates.extend(matchers.match_fuzzy_name(ref, roster))
    candidates = matchers.apply_context_priors(candidates, ref, roster)
    candidates.sort(key=lambda c: c.score, reverse=True)

    if not candidates:
        return ("unattributed", None, None)

    top = candidates[0]
    second_score = candidates[1].score if len(candidates) > 1 else 0.0
    margin = top.score - second_score
    outcome = matchers.gate(top.score, margin, roster_size=len(roster.people))

    if outcome == "auto_link":
        return ("resolved", top.person_id, "inferred")
    if outcome == "ask":
        return ("unconfirmed", top.person_id, None)
    return ("unattributed", None, None)


def test_golden_set_precision_and_false_merge_gate():
    golden = _load_golden()
    roster = _build_roster(golden)

    total_resolved = 0
    correct_resolved = 0
    false_merges = 0

    for case in golden["cases"]:
        ref = RawReference(
            source=case["reference"]["source"],
            external_id=case["reference"].get("external_id"),
            handle=case["reference"].get("handle"),
            email=case["reference"].get("email"),
            display_name=case["reference"].get("display_name"),
        )
        kind, person_id, _confidence = _resolve_via_matchers(ref, roster)
        expect = case["expect"]

        assert kind == expect["kind"], f"{case['name']}: got {kind}, want {expect['kind']}"

        if kind == "resolved":
            total_resolved += 1
            if person_id == expect.get("person_id"):
                correct_resolved += 1
            else:
                false_merges += 1
        elif kind == "unconfirmed" and "top_person_id" in expect:
            assert person_id == expect["top_person_id"], case["name"]

    assert false_merges == 0
    if total_resolved:
        assert correct_resolved / total_resolved >= 0.99
```

```python
# tests/identity/test_gates_property.py
from app.identity import config, matchers


def test_gate_boundaries_match_config_exactly():
    just_below_auto = config.AUTO_LINK_SCORE - 0.01
    at_auto = config.AUTO_LINK_SCORE

    assert matchers.gate(just_below_auto, margin=1.0, roster_size=10) != "auto_link"
    assert matchers.gate(at_auto, margin=1.0, roster_size=10) == "auto_link"


def test_gate_requires_min_roster_size_for_auto_link():
    result = matchers.gate(
        config.AUTO_LINK_SCORE, margin=1.0, roster_size=config.MIN_ROSTER_SIZE - 1
    )
    assert result != "auto_link"


def test_gate_ask_boundary():
    just_below_ask = config.ASK_SCORE - 0.01
    at_ask_with_margin = config.ASK_SCORE

    assert matchers.gate(just_below_ask, margin=1.0, roster_size=10) == "none"
    assert (
        matchers.gate(at_ask_with_margin, margin=config.MARGIN_MIN, roster_size=10)
        == "ask"
    )


def test_gate_ask_requires_margin():
    result = matchers.gate(
        config.ASK_SCORE, margin=config.MARGIN_MIN - 0.01, roster_size=10
    )
    assert result == "none"


def test_gate_is_exhaustive_and_exclusive():
    for score in (0.0, 0.3, 0.69, 0.70, 0.85, 0.94, 0.95, 1.0):
        for margin in (0.0, 0.1, 0.15, 0.29, 0.30, 0.5):
            outcome = matchers.gate(score, margin, roster_size=10)
            assert outcome in ("auto_link", "ask", "none")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/identity/test_matchers_golden.py tests/identity/test_gates_property.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.identity.matchers'`

- [ ] **Step 3: Write the matchers module**

```python
# app/identity/matchers.py
"""The tier ladder (design spec §4.3, §6). Pure functions only — no DB access,
no owner concept (roster is already owner-filtered by roster.py). Everything
here is fixture-testable against tests/identity/golden.yaml without a
database. Only match_idp through match_alias_email (tiers 0-2) are
categorical; match_exact_name through match_fuzzy_name (tiers 3-5) always
produce a scored MatchCandidate that passes through gate()."""
import difflib
import re
from typing import Literal

from app.identity import config
from app.identity.models import Person
from app.identity.normalize import normalize_email, normalize_handle
from app.identity.roster import RosterSnapshot
from app.identity.types import MatchCandidate, RawReference

_HANDLE_SUFFIX_RE = re.compile(r"(-dev|-ext|_ext|\d+)+$")


def match_idp(ref: RawReference, roster: RosterSnapshot) -> MatchCandidate | None:
    # No IdP/SSO connector exists yet (spec §9) — always returns None until one does.
    return None


def match_primary_email(
    ref: RawReference, roster: RosterSnapshot
) -> MatchCandidate | None:
    if not ref.email:
        return None
    target = normalize_email(ref.email)
    for person in roster.people:
        if person.primary_email and normalize_email(person.primary_email) == target:
            return MatchCandidate(person_id=person.id, tier=1, score=0.98)
    return None


_ALIAS_DOMAINS = {"acme.io": "acme.com"}


def _fold_alias_domain(email: str) -> str:
    local, _, domain = email.partition("@")
    folded_domain = _ALIAS_DOMAINS.get(domain, domain)
    return f"{local}@{folded_domain}"


def match_alias_email(
    ref: RawReference, roster: RosterSnapshot
) -> MatchCandidate | None:
    if not ref.email:
        return None
    target = _fold_alias_domain(normalize_email(ref.email))
    for person in roster.people:
        if not person.primary_email:
            continue
        if _fold_alias_domain(normalize_email(person.primary_email)) == target:
            if normalize_email(person.primary_email) == normalize_email(ref.email):
                continue  # exact match already handled by tier 1
            return MatchCandidate(person_id=person.id, tier=2, score=0.92)
    return None


def match_exact_name(
    ref: RawReference, roster: RosterSnapshot
) -> MatchCandidate | None:
    if not ref.display_name:
        return None
    target = normalize_handle(ref.display_name)
    matches = [
        person
        for person in roster.people
        if person.is_active and normalize_handle(person.canonical_name) == target
    ]
    if len(matches) != 1:
        return None
    return MatchCandidate(person_id=matches[0].id, tier=3, score=0.85)


def match_handle_heuristic(
    ref: RawReference, roster: RosterSnapshot
) -> list[MatchCandidate]:
    if not ref.handle:
        return []
    stem = _HANDLE_SUFFIX_RE.sub("", normalize_handle(ref.handle))
    if not stem:
        return []
    candidates = []
    for person in roster.people:
        name_stem = "".join(normalize_handle(person.canonical_name).split())
        ratio = difflib.SequenceMatcher(None, stem, name_stem).ratio()
        if ratio >= 0.6:
            candidates.append(MatchCandidate(person_id=person.id, tier=4, score=round(ratio, 4)))
    return candidates


def match_fuzzy_name(
    ref: RawReference, roster: RosterSnapshot
) -> list[MatchCandidate]:
    if not ref.display_name:
        return []
    target = normalize_handle(ref.display_name)
    candidates = []
    for person in roster.people:
        name = normalize_handle(person.canonical_name)
        ratio = difflib.SequenceMatcher(None, target, name).ratio()
        if 0.4 <= ratio < 1.0:  # exact ties are tier 3's job, not tier 5's
            candidates.append(MatchCandidate(person_id=person.id, tier=5, score=round(ratio, 4)))
    return candidates


def _relationship_with_self(person_id: str, roster: RosterSnapshot):
    self_person = next((p for p in roster.people if p.is_self), None)
    if self_person is None or self_person.id == person_id:
        return None
    pair = tuple(sorted((self_person.id, person_id)))
    return roster.relationships.get(pair)


def apply_context_priors(
    candidates: list[MatchCandidate], ref: RawReference, roster: RosterSnapshot
) -> list[MatchCandidate]:
    """Applies §4 context priors to tier 4/5 candidates only (tiers 0-3 are
    unaffected — they're either categorical or already gate-bound on their own
    fixed score). Priors are computed relative to the self person (the user
    this Mentor instance serves) via PersonRelationship, keyed (a, b) with
    a < b per spec §5."""
    active_count = sum(1 for c in candidates if _person_is_active(c.person_id, roster))
    adjusted = []
    for c in candidates:
        if c.tier not in (4, 5):
            adjusted.append(c)
            continue
        delta = 0.0
        rel = _relationship_with_self(c.person_id, roster)
        if rel is not None and rel.co_meeting_count > 0:
            delta += config.CO_MEETING_PRIOR
        if rel is not None and rel.shared_project_count > 0:
            delta += config.SHARED_PROJECT_PRIOR
        if active_count > 1:
            delta += config.ACTIVE_CANDIDATES_PENALTY
        adjusted.append(
            MatchCandidate(person_id=c.person_id, tier=c.tier, score=max(0.0, min(1.0, c.score + delta)))
        )
    return adjusted


def _person_is_active(person_id: str, roster: RosterSnapshot) -> bool:
    return any(p.id == person_id and p.is_active for p in roster.people)


def gate(
    score: float, margin: float, roster_size: int
) -> Literal["auto_link", "ask", "none"]:
    if (
        score >= config.AUTO_LINK_SCORE
        and margin >= config.SINGLE_CANDIDATE_MARGIN
        and roster_size >= config.MIN_ROSTER_SIZE
    ):
        return "auto_link"
    if score >= config.ASK_SCORE and margin >= config.MARGIN_MIN:
        return "ask"
    return "none"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/identity/test_matchers_golden.py tests/identity/test_gates_property.py -v`
Expected: PASS (all cases in golden.yaml pass, false_merges == 0, gate boundary tests pass)

If a golden case fails, do not loosen the assertion — fix the matcher (this file is the
one place where a false-merge regression must be caught before it reaches `resolve.py`).

- [ ] **Step 5: Commit**

```bash
git add app/identity/matchers.py tests/identity/golden.yaml tests/identity/test_matchers_golden.py tests/identity/test_gates_property.py
git commit -m "feat(identity): add tier ladder matchers with adversarial golden set"
```

---

## Task 11: `app/identity/resolve.py` — `resolve()`, the write-owning entrypoint

**Files:**
- Create: `app/identity/resolve.py`
- Test: `tests/identity/test_resolve.py`

**Interfaces:**
- Consumes: everything from Tasks 5-10 (`config`, `types`, `models`, `normalize`,
  `roster`, `matchers`); `OwnerScope` (Task 3); `Clock` (Task 1)
- Produces: `resolve(scope: OwnerScope, ref: RawReference, clock: Clock) -> Resolution` —
  importable from `app.identity.resolve`. **`scope` is always the first argument** — this
  is the only function later tasks (L3→L4 boundary) call for ingest-time resolution. Also
  produces the private helpers `_write_identity(scope, person_id, ref, key, tier,
  confidence, verified_by, clock) -> None` and `_upsert_unresolved(scope, ref, key,
  candidates, best_score, margin, roster_version, clock) -> None`, used by `resolve()` and
  by Task 12. Every write goes through `scope.add(...)`, which stamps `owner_user_id`
  automatically (Task 3) — no model instance in this file ever sets `owner_user_id`
  itself.

- [ ] **Step 1: Write the failing tests**

```python
# tests/identity/test_resolve.py
import datetime
import uuid

from app.core.clock import FrozenClock
from app.identity.models import Identity, MergeLog, RosterVersion, UnresolvedReference
from app.identity.resolve import resolve
from app.identity.types import RawReference, Resolved, Unattributed, Unconfirmed

NOW = datetime.datetime(2026, 8, 5, 8, 30, tzinfo=datetime.UTC)


def _seed_person(db_session, scope, **overrides):
    from app.identity.models import Person

    person = Person(
        id=overrides.get("id", str(uuid.uuid4())),
        canonical_name=overrides.get("canonical_name", "Sarah Ben Youssef"),
        primary_email=overrides.get("primary_email"),
        is_self=overrides.get("is_self", False),
        is_active=overrides.get("is_active", True),
        created_at=NOW,
    )
    scope.add(person)
    db_session.add(RosterVersion(owner_user_id=scope.owner_user_id, version=1))
    scope.commit()
    return person


def test_tier1_email_match_writes_identity_and_merge_log(db_session, make_scope):
    scope = make_scope()
    person = _seed_person(db_session, scope, primary_email="sarah@acme.com")
    ref = RawReference(
        source="slack", external_id="U1", handle="sarah.dev",
        email="sarah@acme.com", display_name=None,
    )

    result = resolve(scope, ref, FrozenClock(at=NOW))

    assert isinstance(result, Resolved)
    assert result.person_id == person.id
    assert result.confidence == "verified"
    identity = db_session.query(Identity).filter_by(
        owner_user_id=scope.owner_user_id, reference_key="slack:U1"
    ).one()
    assert identity.person_id == person.id
    log = db_session.query(MergeLog).filter_by(
        owner_user_id=scope.owner_user_id, reference_key="slack:U1"
    ).one()
    assert log.action == "auto_link"


def test_second_ingest_of_same_reference_is_a_cache_hit_no_rescan(db_session, make_scope):
    scope = make_scope()
    person = _seed_person(db_session, scope, primary_email="sarah@acme.com")
    ref = RawReference(
        source="slack", external_id="U1", handle="sarah.dev",
        email="sarah@acme.com", display_name=None,
    )
    resolve(scope, ref, FrozenClock(at=NOW))
    before = db_session.query(Identity).filter_by(owner_user_id=scope.owner_user_id).count()

    result = resolve(scope, ref, FrozenClock(at=NOW))

    assert isinstance(result, Resolved)
    assert result.person_id == person.id
    assert (
        db_session.query(Identity).filter_by(owner_user_id=scope.owner_user_id).count()
        == before
    )  # no new row


def test_no_match_produces_unattributed_and_upserts_unresolved(db_session, make_scope):
    scope = make_scope()
    _seed_person(db_session, scope, canonical_name="Someone Else", primary_email="x@acme.com")
    ref = RawReference(
        source="slack", external_id="U2", handle="totallyunrelated",
        email=None, display_name=None,
    )

    result = resolve(scope, ref, FrozenClock(at=NOW))

    assert isinstance(result, Unattributed)
    row = db_session.query(UnresolvedReference).filter_by(
        owner_user_id=scope.owner_user_id, reference_key="slack:U2"
    ).one()
    assert row.status == "pending"


def test_ambiguous_exact_name_is_unconfirmed_not_resolved(db_session, make_scope):
    scope = make_scope()
    _seed_person(db_session, scope, canonical_name="Sarah Ben Youssef")
    ref = RawReference(
        source="calendar", external_id=None, handle=None,
        email=None, display_name="Sarah Ben Youssef",
    )

    result = resolve(scope, ref, FrozenClock(at=NOW))

    # tier 3 (exact name) never auto-links — see spec §4.3
    assert isinstance(result, Unconfirmed)


def test_tier_0_2_write_does_not_bump_roster_version(db_session, make_scope):
    scope = make_scope()
    _seed_person(db_session, scope, primary_email="sarah@acme.com")
    version_before = db_session.get(RosterVersion, scope.owner_user_id).version
    ref = RawReference(
        source="slack", external_id="U1", handle="sarah.dev",
        email="sarah@acme.com", display_name=None,
    )

    resolve(scope, ref, FrozenClock(at=NOW))

    assert db_session.get(RosterVersion, scope.owner_user_id).version == version_before


def test_a_raising_matcher_is_treated_as_did_not_fire(db_session, make_scope, monkeypatch):
    scope = make_scope()
    _seed_person(db_session, scope, primary_email="sarah@acme.com")

    def _boom(ref, roster):
        raise ValueError("malformed input")

    monkeypatch.setattr("app.identity.matchers.match_exact_name", _boom)
    ref = RawReference(
        source="calendar", external_id=None, handle=None,
        email=None, display_name="Sarah Ben Youssef",
    )

    result = resolve(scope, ref, FrozenClock(at=NOW))

    # tier 3 raised and was skipped; no other tier can match this reference,
    # so it degrades to Unattributed rather than propagating the exception
    assert isinstance(result, Unattributed)


def test_resolve_never_matches_across_owners(db_session, make_scope):
    scope_a = make_scope()
    scope_b = make_scope()
    _seed_person(db_session, scope_a, primary_email="sarah@acme.com")
    # scope_b has an empty roster — the same email must NOT resolve against
    # scope_a's Person, because roster.load_snapshot(scope_b) never sees it
    ref = RawReference(
        source="slack", external_id="U1", handle="sarah.dev",
        email="sarah@acme.com", display_name=None,
    )

    result = resolve(scope_b, ref, FrozenClock(at=NOW))

    assert not isinstance(result, Resolved)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/identity/test_resolve.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.identity.resolve'`

- [ ] **Step 3: Write `resolve.py` (part 1 — `resolve()`)**

```python
# app/identity/resolve.py
"""The only module in app/identity with side effects (design spec §6). Owns
resolve(), resolve_for_surface(), confirm(), reject(), unlink(), attach_ask().
Every function takes scope: OwnerScope as its first argument (spec §11.3) —
no bare Session, ever."""
import logging
import uuid

from sqlalchemy import select

from app.core.clock import Clock
from app.core.scope import OwnerScope
from app.identity import config, matchers
from app.identity.models import Identity, MergeLog, UnresolvedReference
from app.identity.normalize import build_reference_key, safe_log_key
from app.identity.roster import load_snapshot
from app.identity.types import (
    MatchCandidate,
    RawReference,
    Resolved,
    Resolution,
    Unattributed,
    Unconfirmed,
)

logger = logging.getLogger(__name__)


def _run_matcher(matcher, ref, roster, key):
    """Runs one matcher, catching and logging any exception rather than
    letting a malformed input abort the whole tier ladder (design spec §7).
    A matcher that raises is treated as 'did not fire'. Never logs raw
    email/handle content — only the hashed reference_key."""
    try:
        return matcher(ref, roster)
    except Exception:
        logger.exception(
            "matcher %s raised for reference_key=%s",
            matcher.__name__,
            safe_log_key(key),
        )
        return None


def resolve(scope: OwnerScope, ref: RawReference, clock: Clock) -> Resolution:
    key = build_reference_key(ref.source, ref.external_id, ref.handle)

    cached = scope.session.execute(
        scope.query(Identity).where(Identity.reference_key == key)
    ).scalar_one_or_none()
    if cached is not None:
        return Resolved(
            person_id=cached.person_id, tier=cached.tier, confidence=cached.confidence
        )

    roster = load_snapshot(scope)

    for tier_matcher in (matchers.match_idp, matchers.match_primary_email, matchers.match_alias_email):
        hit = _run_matcher(tier_matcher, ref, roster, key)
        if hit is not None:
            _write_identity(scope, hit.person_id, ref, key, hit.tier, "verified", "auto", clock)
            return Resolved(person_id=hit.person_id, tier=hit.tier, confidence="verified")

    candidates: list[MatchCandidate] = []
    exact = _run_matcher(matchers.match_exact_name, ref, roster, key)
    if exact is not None:
        candidates.append(exact)
    candidates.extend(_run_matcher(matchers.match_handle_heuristic, ref, roster, key) or [])
    candidates.extend(_run_matcher(matchers.match_fuzzy_name, ref, roster, key) or [])
    candidates = [c for c in candidates if (key, c.person_id) not in roster.not_same_as]
    candidates = matchers.apply_context_priors(candidates, ref, roster)
    candidates.sort(key=lambda c: c.score, reverse=True)

    if not candidates:
        _upsert_unresolved(scope, ref, key, [], None, None, roster.roster_version, clock)
        return Unattributed(reference_key=key, raw_handle=ref.handle)

    top = candidates[0]
    second_score = candidates[1].score if len(candidates) > 1 else 0.0
    margin = round(top.score - second_score, 4)
    outcome = matchers.gate(top.score, margin, roster_size=len(roster.people))

    if outcome == "auto_link":
        _write_identity(scope, top.person_id, ref, key, top.tier, "inferred", "auto", clock)
        return Resolved(person_id=top.person_id, tier=top.tier, confidence="inferred")

    candidate_payload = [
        {"person_id": c.person_id, "tier": c.tier, "score": c.score} for c in candidates
    ]
    _upsert_unresolved(scope, ref, key, candidate_payload, top.score, margin, roster.roster_version, clock)

    if outcome == "ask":
        return Unconfirmed(reference_key=key, top=top, margin=margin)
    return Unattributed(reference_key=key, raw_handle=ref.handle)


def _write_identity(
    scope: OwnerScope,
    person_id: str,
    ref: RawReference,
    key: str,
    tier: int,
    confidence: str,
    verified_by: str,
    clock: Clock,
) -> None:
    now = clock.now()
    scope.add(
        Identity(
            id=str(uuid.uuid4()),
            person_id=person_id,
            source=ref.source,
            external_id=ref.external_id,
            reference_key=key,
            key_version=config.KEY_VERSION,
            tier=tier,
            confidence=confidence,
            verified_by=verified_by,
            handle=ref.handle,
            email=ref.email,
            display_name=ref.display_name,
            provenance={"tier": tier},
            first_seen=now,
            last_seen=now,
        )
    )
    scope.add(
        MergeLog(
            id=str(uuid.uuid4()),
            person_id=person_id,
            reference_key=key,
            action="auto_link",
            prev_person_id=None,
            payload={"tier": tier, "confidence": confidence},
            tier=tier,
            confidence=confidence,
            actor="system",
            reason=f"tier {tier} match",
            created_at=now,
        )
    )
    # No RosterVersion bump: an Identity write for an already-known Person
    # is not matching-relevant (spec §4.6), and never touches another
    # owner's RosterVersion row regardless (spec §11.2).
    scope.commit()


def _upsert_unresolved(
    scope: OwnerScope,
    ref: RawReference,
    key: str,
    candidates: list[dict],
    best_score: float | None,
    margin: float | None,
    roster_version: int,
    clock: Clock,
) -> None:
    now = clock.now()
    today = now.date()
    existing = scope.session.execute(
        scope.query(UnresolvedReference).where(UnresolvedReference.reference_key == key)
    ).scalar_one_or_none()

    if existing is None:
        scope.add(
            UnresolvedReference(
                id=str(uuid.uuid4()),
                reference_key=key,
                key_version=config.KEY_VERSION,
                source=ref.source,
                external_id=ref.external_id,
                handle=ref.handle,
                email=ref.email,
                display_name=ref.display_name,
                candidates=candidates,
                best_score=best_score,
                margin=margin,
                occurrence_count=1,
                distinct_day_count=1,
                last_seen_date=today,
                ask_count=0,
                scored_at=now,
                roster_version=roster_version,
                status="pending",
                first_seen=now,
                last_seen=now,
            )
        )
    else:
        existing.candidates = candidates
        existing.best_score = best_score
        existing.margin = margin
        existing.occurrence_count += 1
        if existing.last_seen_date != today:
            existing.distinct_day_count += 1
            existing.last_seen_date = today
        existing.scored_at = now
        existing.roster_version = roster_version
        existing.last_seen = now

    scope.commit()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/identity/test_resolve.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add app/identity/resolve.py tests/identity/test_resolve.py
git commit -m "feat(identity): add resolve() — cache, tier ladder, owner-scoped writes"
```

---

## Task 12: `resolve.py` — confirm, reject, unlink, resolve_for_surface, attach_ask

**Files:**
- Modify: `app/identity/resolve.py` (append)
- Test: `tests/identity/test_confirmation.py`

**Interfaces:**
- Consumes: `resolve()`, `_write_identity` internals (Task 11); `Event`/`WorkItem`/
  `Message` (Task 4); `PendingConfirmation`, `NotSameAs`, `RosterVersion` (Task 7)
- Produces: `confirm(scope: OwnerScope, reference_key: str, person_id: str, clock: Clock)
  -> None`, `reject(scope, reference_key: str, person_id: str, clock: Clock) -> None`,
  `unlink(scope, reference_key: str, reason: str, clock: Clock) -> None`,
  `resolve_for_surface(scope, reference_key: str, clock: Clock) -> Resolution`,
  `attach_ask(scope, reference_key: str, candidate_person_id: str, candidate_score:
  float, surface: Literal["card", "friday_batch"], card_ref: str | None, clock: Clock) ->
  PendingConfirmation | None` (`None` when `MAX_ASKS_PER_CANDIDATE` is already hit) — all
  importable from `app.identity.resolve`, alongside `resolve()` from Task 11. **Every
  historical-row backfill (`Event`/`WorkItem`/`Message` UPDATE) filters on both
  `owner_user_id == scope.owner_user_id` AND `actor_reference_key == reference_key`** —
  `actor_reference_key` alone is only unique within one owner (spec §11.2), so omitting
  the owner filter here would be exactly the cross-tenant leak the whole revision exists
  to prevent.

- [ ] **Step 1: Add the TTL constant test (constant itself already added in Task 5)**

```python
# tests/identity/test_config.py — add this test to the existing file
def test_pending_confirmation_ttl_is_positive():
    from app.identity import config

    assert config.PENDING_CONFIRMATION_TTL_DAYS > 0
```

Run: `uv run pytest tests/identity/test_config.py -v`
Expected: PASS (all config tests)

- [ ] **Step 2: Write the failing tests**

```python
# tests/identity/test_confirmation.py
import datetime
import uuid

from app.core.clock import FrozenClock
from app.core.models import Event
from app.identity.models import (
    Identity,
    MergeLog,
    NotSameAs,
    Person,
    RosterVersion,
    UnresolvedReference,
)
from app.identity.resolve import attach_ask, confirm, reject, resolve, resolve_for_surface, unlink
from app.identity.types import RawReference, Resolved, Unattributed, Unconfirmed

NOW = datetime.datetime(2026, 8, 5, 8, 30, tzinfo=datetime.UTC)
LATER = NOW + datetime.timedelta(hours=1)


def _seed_unresolved_reference(db_session, scope, person_id, reference_key="slack:handle:sbenali"):
    db_session.add(RosterVersion(owner_user_id=scope.owner_user_id, version=1))
    scope.add(
        UnresolvedReference(
            id=str(uuid.uuid4()),
            reference_key=reference_key,
            key_version=1,
            source="slack",
            external_id=None,
            handle="sbenali",
            email=None,
            display_name=None,
            candidates=[{"person_id": person_id, "tier": 4, "score": 0.8}],
            best_score=0.8,
            margin=0.2,
            occurrence_count=1,
            distinct_day_count=1,
            last_seen_date=NOW.date(),
            ask_count=0,
            scored_at=NOW,
            roster_version=1,
            status="pending",
            first_seen=NOW,
            last_seen=NOW,
        )
    )
    scope.commit()


def test_confirm_writes_identity_and_backfills_historical_rows(db_session, make_scope):
    scope = make_scope()
    person = Person(
        id=str(uuid.uuid4()), canonical_name="Sarah Ben Ali", primary_email=None,
        is_self=False, is_active=True, created_at=NOW,
    )
    scope.add(person)
    _seed_unresolved_reference(db_session, scope, person.id)
    scope.add(
        Event(id=str(uuid.uuid4()), actor_reference_key="slack:handle:sbenali", resolved_person_id=None)
    )
    scope.commit()

    confirm(scope, "slack:handle:sbenali", person.id, FrozenClock(at=LATER))

    identity = db_session.query(Identity).filter_by(
        owner_user_id=scope.owner_user_id, reference_key="slack:handle:sbenali"
    ).one()
    assert identity.verified_by == "user_confirmed"
    assert identity.confidence == "verified"
    event = db_session.query(Event).filter_by(
        owner_user_id=scope.owner_user_id, actor_reference_key="slack:handle:sbenali"
    ).one()
    assert event.resolved_person_id == person.id
    assert db_session.query(UnresolvedReference).filter_by(
        owner_user_id=scope.owner_user_id, reference_key="slack:handle:sbenali"
    ).count() == 0


def test_confirm_never_backfills_another_owners_event(db_session, make_scope):
    scope_a = make_scope()
    scope_b = make_scope()
    person_a = Person(
        id=str(uuid.uuid4()), canonical_name="Sarah Ben Ali", primary_email=None,
        is_self=False, is_active=True, created_at=NOW,
    )
    scope_a.add(person_a)
    _seed_unresolved_reference(db_session, scope_a, person_a.id)
    # owner B happens to have an event with the SAME reference_key text — this
    # must never be touched by owner A's confirm()
    scope_b.add(
        Event(id=str(uuid.uuid4()), actor_reference_key="slack:handle:sbenali", resolved_person_id=None)
    )
    scope_a.commit()
    scope_b.commit()

    confirm(scope_a, "slack:handle:sbenali", person_a.id, FrozenClock(at=LATER))

    owner_b_event = db_session.query(Event).filter_by(owner_user_id=scope_b.owner_user_id).one()
    assert owner_b_event.resolved_person_id is None


def test_confirm_is_idempotent(db_session, make_scope):
    scope = make_scope()
    person = Person(
        id=str(uuid.uuid4()), canonical_name="Sarah Ben Ali", primary_email=None,
        is_self=False, is_active=True, created_at=NOW,
    )
    scope.add(person)
    _seed_unresolved_reference(db_session, scope, person.id)
    scope.commit()

    confirm(scope, "slack:handle:sbenali", person.id, FrozenClock(at=LATER))
    confirm(scope, "slack:handle:sbenali", person.id, FrozenClock(at=LATER))  # no-op, no error

    assert db_session.query(Identity).filter_by(
        owner_user_id=scope.owner_user_id, reference_key="slack:handle:sbenali"
    ).count() == 1


def test_reject_persists_not_same_as_and_filters_future_candidates(db_session, make_scope):
    scope = make_scope()
    rejected_person = Person(
        id=str(uuid.uuid4()), canonical_name="Wrong Person", primary_email=None,
        is_self=False, is_active=True, created_at=NOW,
    )
    correct_person = Person(
        id=str(uuid.uuid4()), canonical_name="Right Person", primary_email="right@acme.com",
        is_self=False, is_active=True, created_at=NOW,
    )
    scope.add(rejected_person)
    scope.add(correct_person)
    _seed_unresolved_reference(db_session, scope, rejected_person.id)
    scope.commit()

    reject(scope, "slack:handle:sbenali", rejected_person.id, FrozenClock(at=LATER))

    assert db_session.query(NotSameAs).filter_by(
        owner_user_id=scope.owner_user_id,
        reference_key="slack:handle:sbenali", person_id=rejected_person.id
    ).count() == 1
    row = db_session.query(UnresolvedReference).filter_by(
        owner_user_id=scope.owner_user_id, reference_key="slack:handle:sbenali"
    ).one()
    assert row.candidates == []
    assert row.best_score is None


def test_confirm_then_unlink_then_reingest_never_relinks(db_session, make_scope):
    scope = make_scope()
    person = Person(
        id=str(uuid.uuid4()), canonical_name="Sarah Ben Ali", primary_email=None,
        is_self=False, is_active=True, created_at=NOW,
    )
    scope.add(person)
    _seed_unresolved_reference(db_session, scope, person.id)
    scope.add(
        Event(id=str(uuid.uuid4()), actor_reference_key="slack:handle:sbenali", resolved_person_id=None)
    )
    scope.commit()

    confirm(scope, "slack:handle:sbenali", person.id, FrozenClock(at=LATER))
    unlink(scope, "slack:handle:sbenali", reason="wrong person", clock=FrozenClock(at=LATER))

    assert db_session.query(Identity).filter_by(
        owner_user_id=scope.owner_user_id, reference_key="slack:handle:sbenali"
    ).count() == 0
    event = db_session.query(Event).filter_by(
        owner_user_id=scope.owner_user_id, actor_reference_key="slack:handle:sbenali"
    ).one()
    assert event.resolved_person_id is None
    split_log = db_session.query(MergeLog).filter_by(
        owner_user_id=scope.owner_user_id, reference_key="slack:handle:sbenali", action="split"
    ).one()
    assert split_log.prev_person_id == person.id

    # re-ingest the same reference — must not silently re-link to the person
    # it was just split from
    ref = RawReference(
        source="slack", external_id=None, handle="sbenali", email=None, display_name=None,
    )
    result = resolve(scope, ref, FrozenClock(at=LATER))
    assert not (isinstance(result, Resolved) and result.person_id == person.id)


def test_attach_ask_respects_max_asks_per_candidate(db_session, make_scope):
    scope = make_scope()
    person = Person(
        id=str(uuid.uuid4()), canonical_name="Sarah Ben Ali", primary_email=None,
        is_self=False, is_active=True, created_at=NOW,
    )
    scope.add(person)
    _seed_unresolved_reference(db_session, scope, person.id)
    scope.commit()

    first = attach_ask(
        scope, "slack:handle:sbenali", person.id, 0.8, "card", "card-1", FrozenClock(at=NOW)
    )
    second = attach_ask(
        scope, "slack:handle:sbenali", person.id, 0.8, "friday_batch", None, FrozenClock(at=LATER)
    )

    assert first is not None
    assert second is None  # MAX_ASKS_PER_CANDIDATE == 1, already asked once


def test_resolve_for_surface_returns_unconfirmed_for_fresh_ambiguous_reference(db_session, make_scope):
    scope = make_scope()
    person = Person(
        id=str(uuid.uuid4()), canonical_name="Sarah Ben Ali", primary_email=None,
        is_self=False, is_active=True, created_at=NOW,
    )
    scope.add(person)
    _seed_unresolved_reference(db_session, scope, person.id)
    scope.commit()

    result = resolve_for_surface(scope, "slack:handle:sbenali", FrozenClock(at=NOW))

    assert isinstance(result, Unconfirmed)
    assert result.top.person_id == person.id


def test_resolve_for_surface_unknown_key_is_unattributed(db_session, make_scope):
    scope = make_scope()
    db_session.add(RosterVersion(owner_user_id=scope.owner_user_id, version=1))
    db_session.commit()

    result = resolve_for_surface(scope, "slack:handle:nobody", FrozenClock(at=NOW))

    assert isinstance(result, Unattributed)
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/identity/test_confirmation.py -v`
Expected: FAIL — `ImportError: cannot import name 'confirm' from 'app.identity.resolve'`

- [ ] **Step 4: Append the confirm/reject/unlink/resolve_for_surface/attach_ask functions**

```python
# app/identity/resolve.py — append below the Task 11 content; also update the
# import block at the top of the file to add these names:
import datetime

from sqlalchemy import select, update

from app.core.models import Event, Message, WorkItem
from app.identity.models import NotSameAs, PendingConfirmation, RosterVersion
```

```python
# app/identity/resolve.py — appended functions

_ATTRIBUTED_TABLES = (Event, WorkItem, Message)


def _bump_roster_version(scope: OwnerScope) -> None:
    row = scope.session.get(RosterVersion, scope.owner_user_id)
    if row is None:
        scope.add(RosterVersion(version=1))
    else:
        row.version += 1


def confirm(scope: OwnerScope, reference_key: str, person_id: str, clock: Clock) -> None:
    already_linked = scope.session.execute(
        scope.query(Identity).where(Identity.reference_key == reference_key)
    ).scalar_one_or_none()
    if already_linked is not None:
        return  # idempotent

    unresolved = scope.session.execute(
        scope.query(UnresolvedReference).where(UnresolvedReference.reference_key == reference_key)
    ).scalar_one_or_none()
    if unresolved is None:
        return  # idempotent: nothing left to confirm

    now = clock.now()
    tier = next(
        (c["tier"] for c in unresolved.candidates if c["person_id"] == person_id), 5
    )

    scope.add(
        Identity(
            id=str(uuid.uuid4()),
            person_id=person_id,
            source=unresolved.source,
            external_id=unresolved.external_id,
            reference_key=reference_key,
            key_version=config.KEY_VERSION,
            tier=tier,
            confidence="verified",
            verified_by="user_confirmed",
            handle=unresolved.handle,
            email=unresolved.email,
            display_name=unresolved.display_name,
            provenance={"source": "user_confirmed"},
            first_seen=now,
            last_seen=now,
        )
    )
    scope.add(
        MergeLog(
            id=str(uuid.uuid4()),
            person_id=person_id,
            reference_key=reference_key,
            action="user_confirm",
            prev_person_id=None,
            payload={"candidates": unresolved.candidates},
            tier=tier,
            confidence="verified",
            actor="user",
            reason="user confirmed identity",
            created_at=now,
        )
    )

    pending = scope.session.execute(
        scope.query(PendingConfirmation).where(
            PendingConfirmation.reference_key == reference_key,
            PendingConfirmation.candidate_person_id == person_id,
            PendingConfirmation.status == "pending",
        )
    ).scalar_one_or_none()
    if pending is not None:
        pending.status = "confirmed"
        pending.answered_at = now

    # owner_user_id filter here is load-bearing: actor_reference_key is only
    # unique within one owner (spec §11.2) — omitting it would backfill
    # another owner's row that happens to share the same reference_key text.
    for table in _ATTRIBUTED_TABLES:
        scope.session.execute(
            update(table)
            .where(
                table.owner_user_id == scope.owner_user_id,
                table.actor_reference_key == reference_key,
            )
            .values(resolved_person_id=person_id)
        )

    scope.session.delete(unresolved)
    # No RosterVersion bump: linking to an already-existing Person doesn't
    # change what other references are scored against (spec §4.6).
    scope.commit()


def reject(scope: OwnerScope, reference_key: str, person_id: str, clock: Clock) -> None:
    now = clock.now()
    scope.add(
        NotSameAs(
            id=str(uuid.uuid4()),
            reference_key=reference_key,
            person_id=person_id,
            rejected_at=now,
            actor="user",
        )
    )
    _bump_roster_version(scope)

    unresolved = scope.session.execute(
        scope.query(UnresolvedReference).where(UnresolvedReference.reference_key == reference_key)
    ).scalar_one_or_none()
    if unresolved is not None:
        remaining = [c for c in unresolved.candidates if c["person_id"] != person_id]
        unresolved.candidates = remaining
        if remaining:
            top, second = remaining[0], (remaining[1] if len(remaining) > 1 else None)
            unresolved.best_score = top["score"]
            unresolved.margin = round(top["score"] - (second["score"] if second else 0.0), 4)
        else:
            unresolved.best_score = None
            unresolved.margin = None

    pending = scope.session.execute(
        scope.query(PendingConfirmation).where(
            PendingConfirmation.reference_key == reference_key,
            PendingConfirmation.candidate_person_id == person_id,
            PendingConfirmation.status == "pending",
        )
    ).scalar_one_or_none()
    if pending is not None:
        pending.status = "rejected"
        pending.answered_at = now

    scope.add(
        MergeLog(
            id=str(uuid.uuid4()),
            person_id=person_id,
            reference_key=reference_key,
            action="user_reject",
            prev_person_id=None,
            payload={"rejected_person_id": person_id},
            tier=None,
            confidence=None,
            actor="user",
            reason="user rejected candidate",
            created_at=now,
        )
    )
    scope.commit()


def unlink(scope: OwnerScope, reference_key: str, reason: str, clock: Clock) -> None:
    identity = scope.session.execute(
        scope.query(Identity).where(Identity.reference_key == reference_key)
    ).scalar_one_or_none()
    if identity is None:
        return  # idempotent: nothing linked

    now = clock.now()
    person_id = identity.person_id
    tier, confidence = identity.tier, identity.confidence
    scope.session.delete(identity)

    for table in _ATTRIBUTED_TABLES:
        scope.session.execute(
            update(table)
            .where(
                table.owner_user_id == scope.owner_user_id,
                table.actor_reference_key == reference_key,
            )
            .values(resolved_person_id=None)
        )

    scope.add(
        MergeLog(
            id=str(uuid.uuid4()),
            person_id=person_id,
            reference_key=reference_key,
            action="split",
            prev_person_id=person_id,
            payload={"unlinked_from": person_id, "tier": tier, "confidence": confidence},
            tier=tier,
            confidence=confidence,
            actor="user",
            reason=reason,
            created_at=now,
        )
    )
    scope.add(
        NotSameAs(
            id=str(uuid.uuid4()),
            reference_key=reference_key,
            person_id=person_id,
            rejected_at=now,
            actor="user",
        )
    )
    _bump_roster_version(scope)
    scope.commit()


def resolve_for_surface(scope: OwnerScope, reference_key: str, clock: Clock) -> Resolution:
    identity = scope.session.execute(
        scope.query(Identity).where(Identity.reference_key == reference_key)
    ).scalar_one_or_none()
    if identity is not None:
        return Resolved(person_id=identity.person_id, tier=identity.tier, confidence=identity.confidence)

    unresolved = scope.session.execute(
        scope.query(UnresolvedReference).where(UnresolvedReference.reference_key == reference_key)
    ).scalar_one_or_none()
    if unresolved is None:
        return Unattributed(reference_key=reference_key, raw_handle=None)

    roster = load_snapshot(scope)
    if unresolved.roster_version < roster.roster_version:
        ref = RawReference(
            source=unresolved.source,
            external_id=unresolved.external_id,
            handle=unresolved.handle,
            email=unresolved.email,
            display_name=unresolved.display_name,
        )
        return resolve(scope, ref, clock)

    if not unresolved.candidates or unresolved.best_score is None or unresolved.margin is None:
        return Unattributed(reference_key=reference_key, raw_handle=unresolved.handle)

    outcome = matchers.gate(unresolved.best_score, unresolved.margin, roster_size=len(roster.people))
    top_dict = unresolved.candidates[0]
    top = MatchCandidate(
        person_id=top_dict["person_id"], tier=top_dict["tier"], score=top_dict["score"]
    )
    if outcome == "ask":
        return Unconfirmed(reference_key=reference_key, top=top, margin=unresolved.margin)
    return Unattributed(reference_key=reference_key, raw_handle=unresolved.handle)


def attach_ask(
    scope: OwnerScope,
    reference_key: str,
    candidate_person_id: str,
    candidate_score: float,
    surface: str,
    card_ref: str | None,
    clock: Clock,
) -> PendingConfirmation | None:
    unresolved = scope.session.execute(
        scope.query(UnresolvedReference).where(UnresolvedReference.reference_key == reference_key)
    ).scalar_one_or_none()
    if unresolved is None or unresolved.ask_count >= config.MAX_ASKS_PER_CANDIDATE:
        return None

    now = clock.now()
    pending = PendingConfirmation(
        id=str(uuid.uuid4()),
        reference_key=reference_key,
        candidate_person_id=candidate_person_id,
        candidate_score=candidate_score,
        surface=surface,
        card_ref=card_ref,
        status="pending",
        asked_at=now,
        answered_at=None,
        expires_at=now + datetime.timedelta(days=config.PENDING_CONFIRMATION_TTL_DAYS),
    )
    scope.add(pending)
    unresolved.ask_count += 1
    unresolved.status = "surfaced"
    scope.commit()
    return pending
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/identity/test_confirmation.py -v`
Expected: PASS (9 tests)

- [ ] **Step 6: Run the full identity suite so far**

Run: `uv run pytest tests/identity -v`
Expected: PASS (all tests across Tasks 5-12)

- [ ] **Step 7: Commit**

```bash
git add app/identity/resolve.py tests/identity/test_config.py tests/identity/test_confirmation.py
git commit -m "feat(identity): add confirm, reject, unlink, resolve_for_surface, attach_ask"
```

---

## Task 13: `app/identity/friday_batch.py` — deferred-ask batching

**Files:**
- Create: `app/identity/friday_batch.py`
- Test: `tests/identity/test_friday_batch.py`

**Interfaces:**
- Consumes: `UnresolvedReference`, `PendingConfirmation` (Task 7); `load_snapshot` (Task
  9); `matchers.gate` (Task 10); `config` (Tasks 5, 12); `OwnerScope` (Task 3)
- Produces: `FridayBatchItem(reference_key: str, handle: str | None, top: MatchCandidate)`
  (frozen dataclass); `build_batch(scope: OwnerScope, limit: int = FRIDAY_BATCH_CAP) ->
  list[FridayBatchItem]` (read-only); `commit_batch(scope: OwnerScope, batch:
  list[FridayBatchItem], card_ref: str, clock: Clock) -> None` (writes — called by L7 only
  after send succeeds) — importable from `app.identity.friday_batch`. `build_batch` is
  called once per owner, at Friday-review time for that specific user — never across
  owners in one call.

- [ ] **Step 1: Write the failing tests**

```python
# tests/identity/test_friday_batch.py
import datetime
import uuid

from app.core.clock import FrozenClock
from app.identity.friday_batch import build_batch, commit_batch
from app.identity.models import PendingConfirmation, Person, RosterVersion, UnresolvedReference

NOW = datetime.datetime(2026, 8, 5, 16, 0, tzinfo=datetime.UTC)


def _seed(db_session, scope, reference_key, person_id, distinct_day_count=1, status="pending"):
    scope.add(
        UnresolvedReference(
            id=str(uuid.uuid4()),
            reference_key=reference_key,
            key_version=1,
            source="slack",
            external_id=None,
            handle=reference_key.split(":")[-1],
            email=None,
            display_name=None,
            candidates=[{"person_id": person_id, "tier": 4, "score": 0.8}],
            best_score=0.8,
            margin=0.2,
            occurrence_count=distinct_day_count,
            distinct_day_count=distinct_day_count,
            last_seen_date=NOW.date(),
            ask_count=0,
            scored_at=NOW,
            roster_version=1,
            status=status,
            first_seen=NOW,
            last_seen=NOW,
        )
    )


def _seed_person(scope):
    person = Person(
        id=str(uuid.uuid4()), canonical_name="Sarah Ben Ali", primary_email=None,
        is_self=False, is_active=True, created_at=NOW,
    )
    scope.add(person)
    return person


def test_build_batch_excludes_live_pending_card_ask(db_session, make_scope):
    scope = make_scope()
    db_session.add(RosterVersion(owner_user_id=scope.owner_user_id, version=1))
    for _ in range(2):
        _seed_person(scope)
    person = _seed_person(scope)
    _seed(db_session, scope, "slack:handle:a", person.id)
    scope.add(
        PendingConfirmation(
            id=str(uuid.uuid4()), reference_key="slack:handle:a",
            candidate_person_id=person.id, candidate_score=0.8, surface="card",
            card_ref="card-1", status="pending", asked_at=NOW, answered_at=None,
            expires_at=NOW + datetime.timedelta(days=7),
        )
    )
    scope.commit()

    batch = build_batch(scope)

    assert batch == []


def test_build_batch_ranks_by_distinct_day_count(db_session, make_scope):
    scope = make_scope()
    db_session.add(RosterVersion(owner_user_id=scope.owner_user_id, version=1))
    for _ in range(2):
        _seed_person(scope)
    chatty_person = _seed_person(scope)
    quiet_person = _seed_person(scope)
    _seed(db_session, scope, "slack:handle:chatty", chatty_person.id, distinct_day_count=1)
    _seed(db_session, scope, "slack:handle:quiet", quiet_person.id, distinct_day_count=5)
    scope.commit()

    batch = build_batch(scope)

    assert batch[0].reference_key == "slack:handle:quiet"


def test_build_batch_never_includes_another_owners_references(db_session, make_scope):
    scope_a = make_scope()
    scope_b = make_scope()
    db_session.add(RosterVersion(owner_user_id=scope_a.owner_user_id, version=1))
    db_session.add(RosterVersion(owner_user_id=scope_b.owner_user_id, version=1))
    for _ in range(2):
        _seed_person(scope_a)
    person_b = _seed_person(scope_b)
    _seed(db_session, scope_b, "slack:handle:only-b", person_b.id)
    scope_a.commit()
    scope_b.commit()

    batch_a = build_batch(scope_a)

    assert batch_a == []


def test_commit_batch_creates_pending_confirmations_and_flips_status(db_session, make_scope):
    scope = make_scope()
    db_session.add(RosterVersion(owner_user_id=scope.owner_user_id, version=1))
    for _ in range(2):
        _seed_person(scope)
    person = _seed_person(scope)
    _seed(db_session, scope, "slack:handle:a", person.id)
    scope.commit()

    batch = build_batch(scope)
    commit_batch(scope, batch, card_ref="friday-2026-08-07", clock=FrozenClock(at=NOW))

    row = db_session.query(UnresolvedReference).filter_by(
        owner_user_id=scope.owner_user_id, reference_key="slack:handle:a"
    ).one()
    assert row.status == "surfaced"
    assert row.ask_count == 1
    confirmation = db_session.query(PendingConfirmation).filter_by(
        owner_user_id=scope.owner_user_id, reference_key="slack:handle:a", surface="friday_batch"
    ).one()
    assert confirmation.card_ref == "friday-2026-08-07"


def test_committed_batch_never_reselected(db_session, make_scope):
    scope = make_scope()
    db_session.add(RosterVersion(owner_user_id=scope.owner_user_id, version=1))
    for _ in range(2):
        _seed_person(scope)
    person = _seed_person(scope)
    _seed(db_session, scope, "slack:handle:a", person.id)
    scope.commit()

    first_batch = build_batch(scope)
    commit_batch(scope, first_batch, card_ref="friday-1", clock=FrozenClock(at=NOW))

    second_batch = build_batch(scope)

    assert second_batch == []


def test_partial_send_failure_leaves_undelivered_rows_pending(db_session, make_scope):
    scope = make_scope()
    db_session.add(RosterVersion(owner_user_id=scope.owner_user_id, version=1))
    for _ in range(2):
        _seed_person(scope)
    person_a = _seed_person(scope)
    person_b = _seed_person(scope)
    _seed(db_session, scope, "slack:handle:a", person_a.id)
    _seed(db_session, scope, "slack:handle:b", person_b.id)
    scope.commit()

    batch = build_batch(scope)
    delivered_only = [item for item in batch if item.reference_key == "slack:handle:a"]
    commit_batch(scope, delivered_only, card_ref="friday-1", clock=FrozenClock(at=NOW))

    undelivered = db_session.query(UnresolvedReference).filter_by(
        owner_user_id=scope.owner_user_id, reference_key="slack:handle:b"
    ).one()
    assert undelivered.status == "pending"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/identity/test_friday_batch.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.identity.friday_batch'`

- [ ] **Step 3: Write the friday_batch module**

```python
# app/identity/friday_batch.py
"""Deferred-ask batching for the Friday review (design spec §4.8). build_batch
is read-only; commit_batch is called by L7 only after the batch actually
sends. Both take scope: OwnerScope first — build_batch is invoked once per
owner, at that owner's Friday-review time, never across owners."""
import datetime
import uuid
from dataclasses import dataclass

from app.core.clock import Clock
from app.core.scope import OwnerScope
from app.identity import config, matchers
from app.identity.models import PendingConfirmation, UnresolvedReference
from app.identity.roster import load_snapshot
from app.identity.types import MatchCandidate


@dataclass(frozen=True)
class FridayBatchItem:
    reference_key: str
    handle: str | None
    top: MatchCandidate


def build_batch(
    scope: OwnerScope, limit: int = config.FRIDAY_BATCH_CAP
) -> list[FridayBatchItem]:
    roster = load_snapshot(scope)
    rows = scope.session.execute(
        scope.query(UnresolvedReference).where(UnresolvedReference.status == "pending")
    ).scalars().all()

    eligible = []
    for row in rows:
        if row.ask_count >= config.MAX_ASKS_PER_CANDIDATE:
            continue
        if not row.candidates or row.best_score is None or row.margin is None:
            continue
        if matchers.gate(row.best_score, row.margin, roster_size=len(roster.people)) != "ask":
            continue
        live_pending = scope.session.execute(
            scope.query(PendingConfirmation).where(
                PendingConfirmation.reference_key == row.reference_key,
                PendingConfirmation.status == "pending",
            )
        ).scalar_one_or_none()
        if live_pending is not None:
            continue
        eligible.append(row)

    eligible.sort(key=lambda r: r.distinct_day_count, reverse=True)
    return [
        FridayBatchItem(
            reference_key=r.reference_key,
            handle=r.handle,
            top=MatchCandidate(
                person_id=r.candidates[0]["person_id"],
                tier=r.candidates[0]["tier"],
                score=r.candidates[0]["score"],
            ),
        )
        for r in eligible[:limit]
    ]


def commit_batch(
    scope: OwnerScope, batch: list[FridayBatchItem], card_ref: str, clock: Clock
) -> None:
    now = clock.now()
    for item in batch:
        row = scope.session.execute(
            scope.query(UnresolvedReference).where(
                UnresolvedReference.reference_key == item.reference_key
            )
        ).scalar_one_or_none()
        if row is None:
            continue  # resolved/removed since build_batch ran — skip safely
        scope.add(
            PendingConfirmation(
                id=str(uuid.uuid4()),
                reference_key=item.reference_key,
                candidate_person_id=item.top.person_id,
                candidate_score=item.top.score,
                surface="friday_batch",
                card_ref=card_ref,
                status="pending",
                asked_at=now,
                answered_at=None,
                expires_at=now + datetime.timedelta(days=config.PENDING_CONFIRMATION_TTL_DAYS),
            )
        )
        row.ask_count += 1
        row.status = "surfaced"
    scope.commit()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/identity/test_friday_batch.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add app/identity/friday_batch.py tests/identity/test_friday_batch.py
git commit -m "feat(identity): add owner-scoped Friday batch build/commit"
```

---

## Task 14: `app/storage/blobs.py` — blob storage, never a database

**Files:**
- Create: `app/storage/__init__.py`
- Create: `app/storage/blobs.py`
- Test: `tests/unit/storage/test_blobs.py`

**Interfaces:**
- Produces: `build_blob_key(owner_user_id: str, kind: str, blob_id: str) -> str` (pure —
  always `"users/{owner_user_id}/{kind}/{blob_id}"`, raises `ValueError` if any component
  contains `/` or `..`, closing off path traversal); `BlobStore` (`Protocol` with `put(key:
  str, data: bytes) -> None`, `get(key: str) -> bytes`, `signed_url(key: str, expires_in:
  int = 3600) -> str`); `LocalFilesystemBlobStore(root_dir: Path)` (dev implementation);
  `GCSBlobStore(bucket_name: str)` (deployment implementation, `google-cloud-storage`) —
  all importable from `app.storage.blobs`. Per spec §11.4: this interface is for raw
  ingest payloads, generated PDFs/cards, and backup dumps — **never** for anything
  requiring a transaction, uniqueness, or a lock (that's §5's schema, via `OwnerScope`).

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/storage/test_blobs.py
import pytest

from app.storage.blobs import LocalFilesystemBlobStore, build_blob_key


def test_build_blob_key_is_owner_prefixed():
    key = build_blob_key(owner_user_id="user-a", kind="raw-slack", blob_id="msg-1")
    assert key == "users/user-a/raw-slack/msg-1"


def test_build_blob_key_rejects_path_traversal():
    with pytest.raises(ValueError):
        build_blob_key(owner_user_id="../etc", kind="raw-slack", blob_id="msg-1")
    with pytest.raises(ValueError):
        build_blob_key(owner_user_id="user-a", kind="raw-slack", blob_id="../../secret")


def test_local_filesystem_store_put_then_get_round_trips(tmp_path):
    store = LocalFilesystemBlobStore(root_dir=tmp_path)
    key = build_blob_key(owner_user_id="user-a", kind="raw-slack", blob_id="msg-1")

    store.put(key, b"raw slack payload")

    assert store.get(key) == b"raw slack payload"


def test_local_filesystem_store_isolates_owners_by_directory(tmp_path):
    store = LocalFilesystemBlobStore(root_dir=tmp_path)
    key_a = build_blob_key(owner_user_id="user-a", kind="raw-slack", blob_id="msg-1")
    key_b = build_blob_key(owner_user_id="user-b", kind="raw-slack", blob_id="msg-1")

    store.put(key_a, b"owner a's payload")
    store.put(key_b, b"owner b's payload")

    assert store.get(key_a) == b"owner a's payload"
    assert store.get(key_b) == b"owner b's payload"


def test_local_filesystem_store_signed_url_points_at_the_key(tmp_path):
    store = LocalFilesystemBlobStore(root_dir=tmp_path)
    key = build_blob_key(owner_user_id="user-a", kind="raw-slack", blob_id="msg-1")
    store.put(key, b"data")

    url = store.signed_url(key)

    assert key in url
```

```python
# tests/unit/storage/test_gcs_blobs.py — exercises GCSBlobStore's wrapper logic
# against a mocked google.cloud.storage client, since this environment has no
# real GCS bucket. This tests that OUR code calls the SDK correctly, not that
# the SDK itself works.
from unittest.mock import MagicMock, patch

from app.storage.blobs import GCSBlobStore, build_blob_key


def test_gcs_store_put_calls_upload_from_string_with_owner_prefixed_key():
    key = build_blob_key(owner_user_id="user-a", kind="raw-slack", blob_id="msg-1")
    with patch("app.storage.blobs.storage.Client") as mock_client_cls:
        mock_bucket = MagicMock()
        mock_client_cls.return_value.bucket.return_value = mock_bucket
        store = GCSBlobStore(bucket_name="mentor-blobs")

        store.put(key, b"payload")

        mock_bucket.blob.assert_called_once_with(key)
        mock_bucket.blob.return_value.upload_from_string.assert_called_once_with(b"payload")


def test_gcs_store_signed_url_delegates_to_blob_generate_signed_url():
    key = build_blob_key(owner_user_id="user-a", kind="raw-slack", blob_id="msg-1")
    with patch("app.storage.blobs.storage.Client") as mock_client_cls:
        mock_bucket = MagicMock()
        mock_client_cls.return_value.bucket.return_value = mock_bucket
        mock_bucket.blob.return_value.generate_signed_url.return_value = "https://signed"
        store = GCSBlobStore(bucket_name="mentor-blobs")

        url = store.signed_url(key, expires_in=120)

        assert url == "https://signed"
        mock_bucket.blob.return_value.generate_signed_url.assert_called_once()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/storage -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.storage.blobs'`

- [ ] **Step 3: Add the GCS dependency and write the blobs module**

Run: `uv add google-cloud-storage`

```python
# app/storage/blobs.py
"""Blob storage for raw ingest payloads, generated PDFs/cards, and backup
dumps — never for anything requiring a transaction, uniqueness, or a lock
(design spec §11.4). Object keys are always owner-prefixed. Swappable the
same way the L6 composition interface is: LocalFilesystemBlobStore for dev,
GCSBlobStore for deployment."""
import datetime
from pathlib import Path
from typing import Protocol

from google.cloud import storage


def build_blob_key(owner_user_id: str, kind: str, blob_id: str) -> str:
    for part, name in ((owner_user_id, "owner_user_id"), (kind, "kind"), (blob_id, "blob_id")):
        if "/" in part or ".." in part:
            raise ValueError(f"invalid {name}: {part!r} (no '/' or '..' allowed)")
    return f"users/{owner_user_id}/{kind}/{blob_id}"


class BlobStore(Protocol):
    def put(self, key: str, data: bytes) -> None: ...
    def get(self, key: str) -> bytes: ...
    def signed_url(self, key: str, expires_in: int = 3600) -> str: ...


class LocalFilesystemBlobStore:
    def __init__(self, root_dir: Path) -> None:
        self._root_dir = Path(root_dir)

    def _path_for(self, key: str) -> Path:
        path = self._root_dir / key
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def put(self, key: str, data: bytes) -> None:
        self._path_for(key).write_bytes(data)

    def get(self, key: str) -> bytes:
        return self._path_for(key).read_bytes()

    def signed_url(self, key: str, expires_in: int = 3600) -> str:
        return f"file://{self._path_for(key)}"


class GCSBlobStore:
    def __init__(self, bucket_name: str) -> None:
        self._bucket = storage.Client().bucket(bucket_name)

    def put(self, key: str, data: bytes) -> None:
        self._bucket.blob(key).upload_from_string(data)

    def get(self, key: str) -> bytes:
        return self._bucket.blob(key).download_as_bytes()

    def signed_url(self, key: str, expires_in: int = 3600) -> str:
        return self._bucket.blob(key).generate_signed_url(
            expiration=datetime.timedelta(seconds=expires_in)
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/storage -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add app/storage pyproject.toml uv.lock tests/unit/storage
git commit -m "feat(storage): add owner-prefixed blob interface (local + GCS)"
```

---

## Task 15: Cross-cutting invariant, cross-tenant isolation, and scope-leak gates

**Files:**
- Test: `tests/identity/test_invariants.py`

**Interfaces:**
- Consumes: everything from Tasks 3, 7-13. No production code changes in this task — it
  exercises the module end-to-end and checks the properties that matter most (spec §8,
  §11): no `reference_key` ever appears in both `Identity` and `UnresolvedReference` for
  the same owner simultaneously; full ingest+resolution for two users sharing the same
  Slack handle/email/display-name produces zero shared `Person`/`Identity` rows; and no
  `app/identity/*.py` module queries an owned model outside `OwnerScope`. These three plus
  the existing precision/false-merge gates (Task 10) are this feature's hard gates —
  Minor findings elsewhere may be deferred, these may not (spec §8, §11's "gates, not
  nice-to-haves" framing).

- [ ] **Step 1: Write the no-dual-membership + cross-tenant isolation tests**

```python
# tests/identity/test_invariants.py
import ast
import datetime
import uuid
from pathlib import Path

from app.core.clock import FrozenClock
from app.identity.models import Person, RosterVersion
from app.identity.resolve import confirm, reject, resolve, unlink
from app.identity.types import RawReference

NOW = datetime.datetime(2026, 8, 5, tzinfo=datetime.UTC)


def _assert_no_dual_membership(db_session, owner_user_id):
    from app.identity.models import Identity, UnresolvedReference

    identity_keys = {
        row.reference_key
        for row in db_session.query(Identity).filter_by(owner_user_id=owner_user_id).all()
    }
    unresolved_keys = {
        row.reference_key
        for row in db_session.query(UnresolvedReference).filter_by(owner_user_id=owner_user_id).all()
    }
    overlap = identity_keys & unresolved_keys
    assert not overlap, f"reference_key(s) in both Identity and UnresolvedReference: {overlap}"


def test_invariant_holds_after_auto_link(db_session, make_scope):
    scope = make_scope()
    person = Person(
        id=str(uuid.uuid4()), canonical_name="Sarah Ben Youssef",
        primary_email="sarah@acme.com", is_self=False, is_active=True, created_at=NOW,
    )
    scope.add(person)
    db_session.add(RosterVersion(owner_user_id=scope.owner_user_id, version=1))
    scope.commit()

    ref = RawReference(
        source="slack", external_id="U1", handle="sarah.dev",
        email="sarah@acme.com", display_name=None,
    )
    resolve(scope, ref, FrozenClock(at=NOW))

    _assert_no_dual_membership(db_session, scope.owner_user_id)


def test_invariant_holds_after_ask_then_confirm(db_session, make_scope):
    scope = make_scope()
    person = Person(
        id=str(uuid.uuid4()), canonical_name="Sarah Ben Youssef",
        primary_email=None, is_self=False, is_active=True, created_at=NOW,
    )
    other = Person(
        id=str(uuid.uuid4()), canonical_name="Other Person",
        primary_email=None, is_self=False, is_active=True, created_at=NOW,
    )
    scope.add(person)
    scope.add(other)
    db_session.add(RosterVersion(owner_user_id=scope.owner_user_id, version=1))
    scope.commit()

    ref = RawReference(
        source="calendar", external_id=None, handle=None,
        email=None, display_name="Sarah Ben Youssef",
    )
    result = resolve(scope, ref, FrozenClock(at=NOW))
    _assert_no_dual_membership(db_session, scope.owner_user_id)

    confirm(scope, result.reference_key, person.id, FrozenClock(at=NOW))
    _assert_no_dual_membership(db_session, scope.owner_user_id)


def test_invariant_holds_after_ask_then_reject(db_session, make_scope):
    scope = make_scope()
    person = Person(
        id=str(uuid.uuid4()), canonical_name="Sarah Ben Youssef",
        primary_email=None, is_self=False, is_active=True, created_at=NOW,
    )
    other = Person(
        id=str(uuid.uuid4()), canonical_name="Other Person",
        primary_email=None, is_self=False, is_active=True, created_at=NOW,
    )
    scope.add(person)
    scope.add(other)
    db_session.add(RosterVersion(owner_user_id=scope.owner_user_id, version=1))
    scope.commit()

    ref = RawReference(
        source="calendar", external_id=None, handle=None,
        email=None, display_name="Sarah Ben Youssef",
    )
    result = resolve(scope, ref, FrozenClock(at=NOW))
    reject(scope, result.reference_key, person.id, FrozenClock(at=NOW))

    _assert_no_dual_membership(db_session, scope.owner_user_id)


def test_invariant_holds_after_confirm_then_unlink_then_reresolve(db_session, make_scope):
    scope = make_scope()
    person = Person(
        id=str(uuid.uuid4()), canonical_name="Sarah Ben Youssef",
        primary_email=None, is_self=False, is_active=True, created_at=NOW,
    )
    other = Person(
        id=str(uuid.uuid4()), canonical_name="Other Person",
        primary_email=None, is_self=False, is_active=True, created_at=NOW,
    )
    scope.add(person)
    scope.add(other)
    db_session.add(RosterVersion(owner_user_id=scope.owner_user_id, version=1))
    scope.commit()

    ref = RawReference(
        source="calendar", external_id=None, handle=None,
        email=None, display_name="Sarah Ben Youssef",
    )
    result = resolve(scope, ref, FrozenClock(at=NOW))
    confirm(scope, result.reference_key, person.id, FrozenClock(at=NOW))
    unlink(scope, result.reference_key, reason="test", clock=FrozenClock(at=NOW))
    resolve(scope, ref, FrozenClock(at=NOW))

    _assert_no_dual_membership(db_session, scope.owner_user_id)


def test_cross_user_isolation_same_handle_email_and_name(db_session, make_scope):
    """The gate the multi-tenancy revision exists for (spec §11 test 1): two
    users whose sources contain the SAME Slack handle, email, and display
    name. Full ingest+resolution for both must produce zero shared Person
    rows, zero shared Identity rows, and each user's roster query returns
    only their own rows."""
    scope_a = make_scope()
    scope_b = make_scope()

    for scope in (scope_a, scope_b):
        person = Person(
            id=str(uuid.uuid4()), canonical_name="Sarah Ben Youssef",
            primary_email="sarah@acme.com", is_self=False, is_active=True, created_at=NOW,
        )
        scope.add(person)
        db_session.add(RosterVersion(owner_user_id=scope.owner_user_id, version=1))
        scope.commit()

    ref = RawReference(
        source="slack", external_id="U123", handle="sarah.dev",
        email="sarah@acme.com", display_name="Sarah Ben Youssef",
    )
    result_a = resolve(scope_a, ref, FrozenClock(at=NOW))
    result_b = resolve(scope_b, ref, FrozenClock(at=NOW))

    from app.identity.roster import load_snapshot

    people_a = {p.id for p in load_snapshot(scope_a).people}
    people_b = {p.id for p in load_snapshot(scope_b).people}

    assert people_a.isdisjoint(people_b)
    assert result_a.person_id != result_b.person_id
    assert result_a.person_id in people_a
    assert result_b.person_id in people_b


def test_scope_leak_static_check():
    """Static gate (spec §8, §11.3): no app/identity/*.py module may call
    session.query(...) or a bare select(...) on an owned model outside
    OwnerScope's own helpers. This walks the AST of every module in
    app/identity/ except roster.py/resolve.py/friday_batch.py's use of
    scope.query(...)/scope.session.execute(scope.query(...)) — those are the
    sanctioned call sites — and flags any `session.query` attribute access or
    bare `select(` call at module scope outside app/core/scope.py itself."""
    identity_dir = Path("app/identity")
    violations = []

    for py_file in identity_dir.glob("*.py"):
        source = py_file.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "query":
                # session.query(...) — the disallowed bare form. scope.query(...)
                # is the sanctioned OwnerScope method and also matches attr=="query",
                # so only flag when the value is literally named "session"/"db_session",
                # not "scope".
                if isinstance(node.value, ast.Name) and node.value.id in (
                    "session",
                    "db_session",
                ):
                    violations.append(f"{py_file}:{node.lineno} bare session.query(...)")

    assert not violations, "scope-leak violations found:\n" + "\n".join(violations)
```

- [ ] **Step 2: Run the tests**

Run: `uv run pytest tests/identity/test_invariants.py -v`
Expected: PASS (6 tests). If the no-dual-membership tests fail, the bug is in whichever of
`resolve`/`confirm`/`reject`/`unlink` left a `reference_key` in both tables for that
owner — fix that function, not this test. If the cross-user isolation test fails, treat it
as the highest-severity possible finding (spec §11.2: "the single worst possible bug this
system could ship") — do not proceed to other tasks until it passes.

- [ ] **Step 3: Run the entire identity + core + storage suite**

Run: `docker compose up -d db && uv run ruff check . && uv run black --check . && uv run pytest -q`
Expected: all pass — this is the Definition of Done from `AGENT.md` §7, applied to the
whole module (including the multi-tenancy revision) in one pass.

- [ ] **Step 4: Commit**

```bash
git add tests/identity/test_invariants.py
git commit -m "test(identity): add no-dual-membership, cross-tenant isolation, and scope-leak gates"
```

---
