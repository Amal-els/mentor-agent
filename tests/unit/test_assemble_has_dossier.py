import datetime
import uuid

from app.core.clock import FrozenClock
from app.core.models import DossierDelivery, Event, User
from app.core.scope import OwnerScope
from app.salience.pulse.assemble import assemble_and_score

NOW = datetime.datetime(2026, 8, 19, 9, 0, 0, tzinfo=datetime.UTC)
LATER = datetime.datetime(2026, 8, 19, 10, 0, 0, tzinfo=datetime.UTC)


def test_event_with_dossier_delivery_is_flagged(pg_session):
    user = User(id=str(uuid.uuid4()), created_at=NOW)
    pg_session.add(user)
    pg_session.flush()
    event = Event(
        id=str(uuid.uuid4()), owner_user_id=user.id, source="calendar",
        external_id="evt-flag-1", title="1:1", starts_at=NOW, ends_at=LATER,
        actor_reference_key="cal#1", url="https://calendar.google.com/event?eid=abc",
    )
    pg_session.add(event)
    dossier_id = str(uuid.uuid4())
    pg_session.add(
        DossierDelivery(
            id=dossier_id, owner_user_id=user.id, event_external_id="evt-flag-1",
            sent_at=NOW, prompt_version="dossier_synthesize@v1",
            talking_points_source="fresh", card_ref=None, feedback="none",
            feedback_at=None, created_at=NOW, who_summary="", why_now="",
        )
    )
    pg_session.commit()

    scope = OwnerScope(owner_user_id=user.id, session=pg_session)
    clock = FrozenClock(at=NOW)

    context = assemble_and_score(scope, clock, source_clients=[])
    assert context.day_events[0].has_dossier is True
    # dossier_id links straight to the delivered dossier — a /ui consumer
    # (app/salience/checklist.py's build_checklist, /webhooks/checklist's
    # own "day" field) doesn't have to re-match event_external_id itself.
    assert context.day_events[0].dossier_id == dossier_id
    assert context.day_events[0].url == "https://calendar.google.com/event?eid=abc"


def test_event_without_dossier_delivery_is_not_flagged(pg_session):
    user = User(id=str(uuid.uuid4()), created_at=NOW)
    pg_session.add(user)
    pg_session.flush()
    event = Event(
        id=str(uuid.uuid4()), owner_user_id=user.id, source="calendar",
        external_id="evt-flag-2", title="1:1", starts_at=NOW, ends_at=LATER,
        actor_reference_key="cal#2",
    )
    pg_session.add(event)
    pg_session.commit()

    scope = OwnerScope(owner_user_id=user.id, session=pg_session)
    clock = FrozenClock(at=NOW)

    context = assemble_and_score(scope, clock, source_clients=[])
    assert context.day_events[0].has_dossier is False
    assert context.day_events[0].dossier_id is None
