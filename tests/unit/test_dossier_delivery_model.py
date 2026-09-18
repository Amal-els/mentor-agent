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


def test_dossier_delivery_stores_candidate_score(pg_session):
    # New column (finding 2 of the whole-branch review fix wave):
    # run_dossier_flow reads this back to build real history_scores for
    # apply_dossier_gate's future decisions. Nullable -- rows written
    # before this column existed, and pull-path deliveries (which never
    # go through the gate), have no score at all.
    user = User(id=str(uuid.uuid4()), created_at=NOW)
    pg_session.add(user)
    pg_session.flush()
    delivery = DossierDelivery(
        id=str(uuid.uuid4()), owner_user_id=user.id,
        event_external_id=f"evt-{uuid.uuid4()}", sent_at=NOW,
        prompt_version="dossier_synthesize@v1", talking_points_source="fresh",
        card_ref=None, feedback="none", feedback_at=None, created_at=NOW,
        candidate_score=3.75,
    )
    pg_session.add(delivery)
    pg_session.commit()
    fetched = pg_session.get(DossierDelivery, delivery.id)
    assert fetched.candidate_score == 3.75


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
