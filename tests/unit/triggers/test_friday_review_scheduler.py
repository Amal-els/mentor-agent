import datetime
import uuid

import pytest

from app.core.clock import FrozenClock
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
    # A Friday, well past any reasonable fire time — deliberately injected
    # via `clock` (not real wall-clock `datetime.now()`, unlike
    # cron_scheduler.py's own _poll_once) so this test's pass/fail doesn't
    # depend on which real-world weekday the suite happens to run on. The
    # extra weekday==Friday term in _should_fire (absent from pulse's own
    # day-agnostic gate) is exactly why that real-wall-clock convention
    # isn't safe to copy here.
    friday_afternoon = datetime.datetime(2026, 8, 28, 17, 0, 0, tzinfo=datetime.UTC)
    linked = User(
        id=str(uuid.uuid4()), created_at=friday_afternoon,
        slack_user_id=f"U{uuid.uuid4().hex[:10]}", tz="UTC",
        friday_review_fire_time_local=datetime.time(16, 0),
    )
    unlinked = User(
        id=str(uuid.uuid4()), created_at=friday_afternoon,
        slack_user_id=None, tz="UTC",
        friday_review_fire_time_local=datetime.time(16, 0),
    )
    pg_session.add_all([linked, unlinked])
    pg_session.commit()

    called_with = []
    monkeypatch.setattr(
        "app.triggers.friday_review_scheduler.run_friday_review_flow",
        lambda scope, clock: called_with.append(scope.owner_user_id),
    )

    _poll_once(lambda: pg_session, [linked.id, unlinked.id], clock=FrozenClock(at=friday_afternoon))

    assert called_with == [linked.id]


def test_poll_once_skips_users_before_their_local_fire_time(pg_session, monkeypatch):
    friday_morning = datetime.datetime(2026, 8, 28, 10, 0, 0, tzinfo=datetime.UTC)
    user = User(
        id=str(uuid.uuid4()), created_at=friday_morning,
        slack_user_id=f"U{uuid.uuid4().hex[:10]}", tz="UTC",
        friday_review_fire_time_local=datetime.time(16, 0),
    )
    pg_session.add(user)
    pg_session.commit()

    called_with = []
    monkeypatch.setattr(
        "app.triggers.friday_review_scheduler.run_friday_review_flow",
        lambda scope, clock: called_with.append(scope.owner_user_id),
    )

    _poll_once(lambda: pg_session, [user.id], clock=FrozenClock(at=friday_morning))

    assert called_with == []
