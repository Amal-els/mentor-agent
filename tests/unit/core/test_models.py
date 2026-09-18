import datetime
import uuid

from app.core.models import Event, User


def test_event_round_trips_owner_and_actor_reference_key(pg_session):
    user = User(id=str(uuid.uuid4()), created_at=datetime.datetime.now(datetime.UTC))
    pg_session.add(user)
    pg_session.commit()

    event = Event(
        id=str(uuid.uuid4()),
        owner_user_id=user.id,
        actor_reference_key="slack:U123",
        resolved_person_id=None,
    )
    pg_session.add(event)
    pg_session.commit()

    fetched = pg_session.get(Event, event.id)
    assert fetched.owner_user_id == user.id
    assert fetched.actor_reference_key == "slack:U123"
    assert fetched.resolved_person_id is None
