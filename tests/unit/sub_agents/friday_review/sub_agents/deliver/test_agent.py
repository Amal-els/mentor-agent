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
    slack_user_id = f"U{uuid.uuid4().hex[:10]}"
    user = User(id=str(uuid.uuid4()), created_at=NOW, slack_user_id=slack_user_id)
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
