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
