import datetime
import uuid

import pytest

from app.core.clock import FrozenClock
from app.core.models import FeedbackEvent, PulseDelivery, Suppression, User
from app.core.scope import OwnerScope
from app.delivery.affordances import record_feedback, snooze_item

NOW = datetime.datetime(2026, 8, 7, 9, 0, tzinfo=datetime.UTC)
CLOCK = FrozenClock(at=NOW)


@pytest.fixture
def scope(pg_session):
    uid = str(uuid.uuid4())
    pg_session.add(User(id=uid, created_at=NOW))
    pg_session.commit()
    return OwnerScope(owner_user_id=uid, session=pg_session)


def test_snooze_item_writes_an_instance_suppression(pg_session, scope):
    row = snooze_item(scope, CLOCK, item_id="evt-1")

    assert row.scope == "instance"
    assert row.target_ref == "evt-1"
    assert row.expires_at > NOW

    rows = pg_session.execute(scope.query(Suppression)).scalars().all()
    assert len(rows) == 1
    assert rows[0].id == row.id


def test_snooze_item_expires_after_the_configured_ttl(pg_session, scope):
    row = snooze_item(scope, CLOCK, item_id="evt-1")

    assert (row.expires_at - NOW).days == 1


def test_record_feedback_up_writes_a_feedback_event(pg_session, scope):
    delivery = PulseDelivery(
        id=str(uuid.uuid4()),
        ritual="pulse",
        local_date=NOW.date(),
        trigger="pull",
        delivered_at=NOW,
        item_ids=["evt-1"],
        context_hash="abc",
        prompt_version="pulse_ranker.v1",
    )
    scope.add(delivery)
    scope.commit()

    row = record_feedback(scope, CLOCK, delivery.id, "evt-1", "event", "up")

    assert row.signal == "up"
    assert row.item_type == "event"
    assert row.pulse_delivery_id == delivery.id
    rows = pg_session.execute(scope.query(FeedbackEvent)).scalars().all()
    assert len(rows) == 1


def test_record_feedback_rejects_invalid_signal(pg_session, scope):
    with pytest.raises(ValueError):
        record_feedback(scope, CLOCK, "some-delivery-id", "evt-1", "event", "sideways")
