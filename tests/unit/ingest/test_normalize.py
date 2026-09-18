import datetime
import uuid

from app.core.clock import FrozenClock
from app.core.models import Event, Goal, OneOnOneNote, RawIngestRef, WorkItem
from app.identity.models import Person
from app.ingest.fixture_source import load_day_fixture
from app.ingest.normalize import (
    normalize_event,
    normalize_goal,
    normalize_message,
    normalize_one_on_one_note,
    normalize_work_item,
)

CLOCK = FrozenClock(at=datetime.datetime(2026, 8, 7, 7, 0, tzinfo=datetime.UTC))


def _seed_sarah(scope):
    scope.add(
        Person(
            id=str(uuid.uuid4()),
            canonical_name="Sarah Ben Youssef",
            primary_email="sarah@acme.com",
            is_self=False,
            is_active=True,
            created_at=datetime.datetime.now(datetime.UTC),
        )
    )
    scope.commit()


def test_normalize_event_maps_fields_and_resolves_actor(pg_session, make_scope):
    scope = make_scope()
    _seed_sarah(scope)
    raw = load_day_fixture("normal_day")["events"][0]  # evt-1, 1:1 with Sarah

    row = normalize_event(scope, raw, CLOCK)

    assert row.external_id == "evt-1"
    assert row.title == "1:1 with Sarah"
    assert row.status == "confirmed"
    assert row.resolved_person_id is not None


def test_normalize_event_leaves_unresolved_actor_unresolved(pg_session, make_scope):
    scope = make_scope()
    raw = load_day_fixture("normal_day")["events"][1]  # evt-2, standup, no roster match

    row = normalize_event(scope, raw, CLOCK)

    assert row.resolved_person_id is None


def test_normalize_event_is_idempotent_on_owner_source_external_id(
    pg_session, make_scope
):
    scope = make_scope()
    raw = load_day_fixture("normal_day")["events"][0]

    first = normalize_event(scope, raw, CLOCK)
    second = normalize_event(scope, raw, CLOCK)

    assert first.id == second.id
    rows = pg_session.execute(scope.query(Event)).scalars().all()
    assert len(rows) == 1


def test_normalize_event_stamps_provenance_idempotently(pg_session, make_scope):
    scope = make_scope()
    raw = load_day_fixture("normal_day")["events"][0]

    normalize_event(scope, raw, CLOCK)
    normalize_event(scope, raw, CLOCK)

    refs = pg_session.execute(scope.query(RawIngestRef)).scalars().all()
    assert len(refs) == 1
    assert refs[0].canonical_table == "events"
    assert refs[0].source == "calendar"
    assert refs[0].external_id == "evt-1"


def test_normalize_work_item_maps_fields(pg_session, make_scope):
    scope = make_scope()
    raw = load_day_fixture("normal_day")["work_items"][0]

    row = normalize_work_item(scope, raw, CLOCK)

    assert row.external_id == "MENT-214"
    assert row.status == "blocked"


def test_normalize_message_maps_fields_and_never_stores_body(pg_session, make_scope):
    scope = make_scope()
    raw = load_day_fixture("normal_day")["messages"][0]

    row = normalize_message(scope, raw, CLOCK)

    assert row.external_id == "1699999999.000100"
    assert row.channel == "C0123"
    assert row.body_ref.startswith("fixture://")
    assert not hasattr(row, "body")


def test_normalize_updates_existing_row_on_reingest(pg_session, make_scope):
    scope = make_scope()
    raw = dict(load_day_fixture("normal_day")["work_items"][0])

    first = normalize_work_item(scope, raw, CLOCK)
    assert first.status == "blocked"

    raw["status"] = "done"
    second = normalize_work_item(scope, raw, CLOCK)

    assert second.id == first.id
    assert second.status == "done"
    rows = pg_session.execute(scope.query(WorkItem)).scalars().all()
    assert len(rows) == 1


_OBJECTIVE_RAW = {
    "source": "notion",
    "external_id": "obj-1",
    "goal_type": "objective",
    "title": "Ship the mentor agent",
    "status": "On Track",
    "quarter": "Q3",
    "progress": 0.5,
    "parent_external_id": None,
}

_KEY_RESULT_RAW = {
    "source": "notion",
    "external_id": "kr-1",
    "goal_type": "key_result",
    "title": "Ship 2 features",
    "current_value": 1.0,
    "target_value": 2.0,
    "parent_external_id": "obj-1",
}


def test_normalize_goal_maps_fields(pg_session, make_scope):
    scope = make_scope()

    row = normalize_goal(scope, _OBJECTIVE_RAW, CLOCK)

    assert row.external_id == "obj-1"
    assert row.goal_type == "objective"
    assert row.title == "Ship the mentor agent"
    assert row.quarter == "Q3"
    assert row.progress == 0.5
    assert row.owner_user_id == scope.owner_user_id


def test_normalize_goal_is_idempotent_on_owner_source_external_id(
    pg_session, make_scope
):
    scope = make_scope()

    first = normalize_goal(scope, _KEY_RESULT_RAW, CLOCK)
    second = normalize_goal(scope, dict(_KEY_RESULT_RAW, current_value=2.0), CLOCK)

    assert first.id == second.id
    assert second.current_value == 2.0
    rows = pg_session.execute(scope.query(Goal)).scalars().all()
    assert len(rows) == 1


def test_normalize_goal_preserves_key_result_parent_link(pg_session, make_scope):
    scope = make_scope()

    row = normalize_goal(scope, _KEY_RESULT_RAW, CLOCK)

    assert row.parent_external_id == "obj-1"
    assert row.goal_type == "key_result"


def test_normalize_goal_stamps_provenance(pg_session, make_scope):
    scope = make_scope()

    normalize_goal(scope, _OBJECTIVE_RAW, CLOCK)

    refs = pg_session.execute(scope.query(RawIngestRef)).scalars().all()
    assert len(refs) == 1
    assert refs[0].canonical_table == "goals"
    assert refs[0].source == "notion"
    assert refs[0].external_id == "obj-1"


def test_normalize_one_on_one_note_maps_fields_and_is_idempotent(
    pg_session, make_scope
):
    scope = make_scope()
    raw = {
        "source": "notion",
        "external_id": "note-1",
        "title": "1:1: check-in",
        "linked_key_result_external_ids": '["kr-1"]',
    }

    first = normalize_one_on_one_note(scope, raw, CLOCK)
    second = normalize_one_on_one_note(scope, raw, CLOCK)

    assert first.id == second.id
    assert first.title == "1:1: check-in"
    assert first.linked_key_result_external_ids == '["kr-1"]'
    assert first.owner_user_id == scope.owner_user_id
    rows = pg_session.execute(scope.query(OneOnOneNote)).scalars().all()
    assert len(rows) == 1
