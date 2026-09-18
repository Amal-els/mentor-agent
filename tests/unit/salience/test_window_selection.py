"""Decision 5 (docs/plans/morning-pulse.md): window selection.
1. An un-elapsed item remaining today -> show today, regardless of clock.
2. Else now >= late_cutoff_local -> show tomorrow (late_cutoff).
3. Else (before cutoff, nothing left today) -> show tomorrow (today_exhausted).
"""

import datetime
import uuid

from app.core.clock import FrozenClock
from app.core.models import Event, User
from app.salience.pulse.assemble import select_window

OWNER_TZ = "Europe/Paris"


def _user(**overrides):
    defaults = {
        "id": "usr_amal",
        "created_at": datetime.datetime.now(datetime.UTC),
        "tz": OWNER_TZ,
        "pulse_fire_time_local": datetime.time(8, 30),
        "late_cutoff_local": datetime.time(21, 0),
    }
    defaults.update(overrides)
    return User(**defaults)


def _event(starts_at, ends_at):
    return Event(
        id=str(uuid.uuid4()),
        owner_user_id="usr_amal",
        actor_reference_key="calendar:x",
        source="calendar",
        external_id="evt",
        title="x",
        starts_at=starts_at,
        ends_at=ends_at,
        status="confirmed",
    )


def _at(hour, minute):
    return datetime.datetime(
        2026, 8, 7, hour, minute, tzinfo=datetime.timezone(datetime.timedelta(hours=2))
    )


def test_2340_with_nothing_left_shows_tomorrow_late_cutoff():
    user = _user()
    clock = FrozenClock(at=_at(23, 40))

    window_date, reason = select_window(user, clock, events_today=[])

    assert window_date == datetime.date(2026, 8, 8)
    assert reason == "late_cutoff"


def test_2105_with_2130_event_shows_today():
    user = _user()
    clock = FrozenClock(at=_at(21, 5))
    events_today = [_event(_at(21, 30), _at(22, 0))]

    window_date, reason = select_window(user, clock, events_today)

    assert window_date == datetime.date(2026, 8, 7)
    assert reason == "today"


def test_2055_with_empty_remainder_shows_tomorrow_today_exhausted():
    user = _user()
    clock = FrozenClock(at=_at(20, 55))
    events_today = [_event(_at(9, 0), _at(9, 30))]  # already ended

    window_date, reason = select_window(user, clock, events_today)

    assert window_date == datetime.date(2026, 8, 8)
    assert reason == "today_exhausted"


def test_0900_normal_shows_today():
    user = _user()
    clock = FrozenClock(at=_at(9, 0))
    events_today = [_event(_at(9, 0), _at(9, 30))]

    window_date, reason = select_window(user, clock, events_today)

    assert window_date == datetime.date(2026, 8, 7)
    assert reason == "today"


def test_per_user_cutoff_override_moves_the_boundary():
    early_cutoff_user = _user(late_cutoff_local=datetime.time(18, 0))
    clock = FrozenClock(at=_at(19, 0))

    window_date, reason = select_window(early_cutoff_user, clock, events_today=[])

    assert window_date == datetime.date(2026, 8, 8)
    assert reason == "late_cutoff"


def test_default_cutoff_user_at_same_clock_time_is_unaffected():
    default_cutoff_user = _user()  # 21:00 default
    clock = FrozenClock(at=_at(19, 0))

    window_date, reason = select_window(default_cutoff_user, clock, events_today=[])

    assert window_date == datetime.date(2026, 8, 8)
    assert reason == "today_exhausted"
