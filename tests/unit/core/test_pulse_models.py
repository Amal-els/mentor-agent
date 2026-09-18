import datetime
import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from app.core.models import (
    Commitment,
    Event,
    FeedbackEvent,
    Goal,
    Message,
    PulseDelivery,
    RawIngestRef,
    Suppression,
    User,
    Weight,
    WorkItem,
)

NOW = datetime.datetime.now(datetime.UTC)


def test_event_full_fields_round_trip(pg_session, make_scope):
    scope = make_scope()
    event = Event(
        id=str(uuid.uuid4()),
        actor_reference_key="calendar:evt-1",
        resolved_person_id=None,
        source="calendar",
        external_id="evt-1",
        title="1:1 with Sarah",
        starts_at=NOW,
        ends_at=NOW + datetime.timedelta(minutes=30),
        status="confirmed",
        url="https://calendar.example/evt-1",
        series_id=None,
    )
    scope.add(event)
    scope.commit()

    fetched = pg_session.get(Event, event.id)
    assert fetched.title == "1:1 with Sarah"
    assert fetched.source == "calendar"
    assert fetched.external_id == "evt-1"
    assert fetched.starts_at is not None
    assert fetched.status == "confirmed"


def test_event_unique_owner_source_external_id(pg_session, make_scope):
    scope = make_scope()
    scope.add(
        Event(
            id=str(uuid.uuid4()),
            actor_reference_key="calendar:evt-2",
            source="calendar",
            external_id="evt-2",
            title="dup",
            starts_at=NOW,
            ends_at=NOW,
            status="confirmed",
        )
    )
    scope.commit()

    dup = Event(
        id=str(uuid.uuid4()),
        owner_user_id=scope.owner_user_id,
        actor_reference_key="calendar:evt-2-b",
        source="calendar",
        external_id="evt-2",
        title="dup-2",
        starts_at=NOW,
        ends_at=NOW,
        status="confirmed",
    )
    pg_session.add(dup)
    with pytest.raises(IntegrityError):
        pg_session.commit()
    pg_session.rollback()


def test_work_item_full_fields_round_trip(pg_session, make_scope):
    scope = make_scope()
    item = WorkItem(
        id=str(uuid.uuid4()),
        actor_reference_key="linear:U1",
        source="linear",
        external_id="MENT-214",
        title="Fix flaky test",
        status="blocked",
        url="https://linear.app/MENT-214",
        due_at=NOW,
        updated_at=NOW,
    )
    scope.add(item)
    scope.commit()

    fetched = pg_session.get(WorkItem, item.id)
    assert fetched.status == "blocked"
    assert fetched.external_id == "MENT-214"


def test_message_full_fields_round_trip(pg_session, make_scope):
    scope = make_scope()
    msg = Message(
        id=str(uuid.uuid4()),
        actor_reference_key="slack:U2",
        source="slack",
        external_id="1699999999.000100",
        channel="C0123",
        sent_at=NOW,
        url="https://slack.example/archives/C0123/p1699999999000100",
        is_dm=False,
        body_ref="s3://raw/slack/1699999999.000100",
    )
    scope.add(msg)
    scope.commit()

    fetched = pg_session.get(Message, msg.id)
    assert fetched.channel == "C0123"
    assert fetched.body_ref == "s3://raw/slack/1699999999.000100"
    # never a raw message body column on the model
    assert not hasattr(fetched, "body")


def test_goal_round_trip(pg_session, make_scope):
    scope = make_scope()
    goal = Goal(
        id=str(uuid.uuid4()),
        title="Ship morning pulse",
        status="active",
        external_ref=None,
        created_at=NOW,
    )
    scope.add(goal)
    scope.commit()

    fetched = pg_session.get(Goal, goal.id)
    assert fetched.title == "Ship morning pulse"
    assert fetched.status == "active"


def test_commitment_round_trip(pg_session, make_scope):
    scope = make_scope()
    commitment = Commitment(
        id=str(uuid.uuid4()),
        promised_to_person_id=None,
        description="Send Sarah the review",
        source_reference_key="slack:msg-1",
        promised_at=NOW,
        due_at=None,
        delivered_at=None,
        status="open",
    )
    scope.add(commitment)
    scope.commit()

    fetched = pg_session.get(Commitment, commitment.id)
    assert fetched.status == "open"
    assert fetched.delivered_at is None


def test_suppression_round_trip(pg_session, make_scope):
    scope = make_scope()
    suppression = Suppression(
        id=str(uuid.uuid4()),
        scope="series",
        target_ref="series:standup",
        reason="user muted",
        created_at=NOW,
        expires_at=NOW + datetime.timedelta(days=7),
        created_by="user",
    )
    scope.add(suppression)
    scope.commit()

    fetched = pg_session.get(Suppression, suppression.id)
    assert fetched.scope == "series"
    assert fetched.expires_at > NOW


def test_weight_round_trip(pg_session, make_scope):
    scope = make_scope()
    weight = Weight(
        id=str(uuid.uuid4()),
        key="goal:ship-pulse",
        value=1.5,
        updated_at=NOW,
    )
    scope.add(weight)
    scope.commit()

    fetched = pg_session.get(Weight, weight.id)
    assert fetched.value == 1.5


def test_pulse_delivery_round_trip_and_idempotent_unique(pg_session, make_scope):
    scope = make_scope()
    delivery = PulseDelivery(
        id=str(uuid.uuid4()),
        ritual="pulse",
        local_date=datetime.date(2026, 8, 7),
        trigger="cron",
        delivered_at=NOW,
        item_ids=["evt-1", "wi-214"],
        context_hash="abc123",
        prompt_version="pulse_writer.v1",
    )
    scope.add(delivery)
    scope.commit()

    fetched = pg_session.get(PulseDelivery, delivery.id)
    assert fetched.item_ids == ["evt-1", "wi-214"]

    dup = PulseDelivery(
        id=str(uuid.uuid4()),
        owner_user_id=scope.owner_user_id,
        ritual="pulse",
        local_date=datetime.date(2026, 8, 7),
        trigger="cron",
        delivered_at=NOW,
        item_ids=[],
        context_hash="different",
        prompt_version="pulse_writer.v1",
    )
    pg_session.add(dup)
    with pytest.raises(IntegrityError):
        pg_session.commit()
    pg_session.rollback()


def test_feedback_event_round_trip(pg_session, make_scope):
    scope = make_scope()
    delivery = PulseDelivery(
        id=str(uuid.uuid4()),
        ritual="pulse",
        local_date=datetime.date(2026, 8, 7),
        trigger="pull",
        delivered_at=NOW,
        item_ids=["evt-1"],
        context_hash="abc123",
        prompt_version="pulse_writer.v1",
    )
    scope.add(delivery)
    scope.commit()

    feedback = FeedbackEvent(
        id=str(uuid.uuid4()),
        pulse_delivery_id=delivery.id,
        item_id="evt-1",
        item_type="event",
        signal="up",
        created_at=NOW,
    )
    scope.add(feedback)
    scope.commit()

    fetched = pg_session.get(FeedbackEvent, feedback.id)
    assert fetched.signal == "up"
    assert fetched.item_type == "event"
    assert fetched.consolidated_at is None


def test_raw_ingest_ref_round_trip(pg_session, make_scope):
    scope = make_scope()
    ref = RawIngestRef(
        id=str(uuid.uuid4()),
        source="calendar",
        external_id="evt-1",
        canonical_table="events",
        canonical_id="some-event-id",
        fetched_at=NOW,
        checksum="deadbeef",
    )
    scope.add(ref)
    scope.commit()

    fetched = pg_session.get(RawIngestRef, ref.id)
    assert fetched.canonical_table == "events"


def test_user_pulse_preferences_round_trip(pg_session):
    user = User(
        id=str(uuid.uuid4()),
        created_at=NOW,
        tz="Europe/Paris",
        pulse_fire_time_local=datetime.time(8, 30),
        late_cutoff_local=datetime.time(21, 0),
    )
    pg_session.add(user)
    pg_session.commit()

    fetched = pg_session.get(User, user.id)
    assert fetched.tz == "Europe/Paris"
    assert fetched.pulse_fire_time_local == datetime.time(8, 30)
    assert fetched.late_cutoff_local == datetime.time(21, 0)


def test_new_tables_are_owner_scoped_isolated(pg_session, make_scope):
    scope_a = make_scope()
    scope_b = make_scope()

    scope_a.add(
        Suppression(
            id=str(uuid.uuid4()),
            scope="global",
            target_ref="global",
            reason="quiet",
            created_at=NOW,
            expires_at=NOW + datetime.timedelta(days=1),
            created_by="user",
        )
    )
    scope_a.commit()

    rows_a = pg_session.execute(scope_a.query(Suppression)).scalars().all()
    rows_b = pg_session.execute(scope_b.query(Suppression)).scalars().all()

    assert len(rows_a) == 1
    assert len(rows_b) == 0
