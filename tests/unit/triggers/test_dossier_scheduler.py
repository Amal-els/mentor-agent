import datetime
import uuid

import pytest

from app.core.clock import FrozenClock
from app.core.models import User
from app.triggers.dossier.dossier_scheduler import (
    _get_dossier_scheduled_report_user_ids,
    _is_t_minus_15,
    poll_dossier_window_once,
)

NOW = datetime.datetime(2026, 8, 19, 9, 0, 0, tzinfo=datetime.UTC)


def test_whitelist_env_var_required(monkeypatch):
    monkeypatch.delenv("DOSSIER_SCHEDULED_REPORT_USER_IDS", raising=False)
    with pytest.raises(RuntimeError):
        _get_dossier_scheduled_report_user_ids()


def test_whitelist_env_var_parses_csv(monkeypatch):
    monkeypatch.setenv("DOSSIER_SCHEDULED_REPORT_USER_IDS", "a,b, c")
    assert _get_dossier_scheduled_report_user_ids() == ["a", "b", "c"]


@pytest.mark.parametrize(
    "delta_seconds,expected",
    [
        (900, True),  # exactly T-15
        (899, True),  # T-14:59
        (901, False),  # T-15:01
        (-60, False),  # meeting already started
    ],
)
def test_is_t_minus_15_boundaries(delta_seconds, expected):
    starts_at = (NOW + datetime.timedelta(seconds=delta_seconds)).isoformat()
    assert _is_t_minus_15(starts_at, NOW) is expected


class _FixtureCalendarClient:
    def __init__(self, events):
        self._events = events

    def fetch(self, window, owner_user_id):
        return self._events


def _factory_for(events):
    return lambda session, report_user_id: _FixtureCalendarClient(events)


def test_poll_dedupes_across_two_calls(pg_session):
    user = User(id=str(uuid.uuid4()), created_at=NOW)
    pg_session.add(user)
    pg_session.commit()

    events = [
        {
            "external_id": "evt-dedup-1",
            "title": "1:1",
            "starts_at": (NOW + datetime.timedelta(minutes=15)).isoformat(),
            "ends_at": (NOW + datetime.timedelta(minutes=45)).isoformat(),
            "attendees": [],
        }
    ]
    seen: list[str] = []

    def session_factory():
        return pg_session

    poll_dossier_window_once(
        session_factory,
        [user.id],
        clock=FrozenClock(at=NOW),
        calendar_client_factory=_factory_for(events),
        on_qualifying_event=lambda report_user_id, event: seen.append(event["external_id"]),
    )
    poll_dossier_window_once(
        session_factory,
        [user.id],
        clock=FrozenClock(at=NOW),
        calendar_client_factory=_factory_for(events),
        on_qualifying_event=lambda report_user_id, event: seen.append(event["external_id"]),
    )
    assert seen == ["evt-dedup-1"]


def test_one_callback_failure_does_not_stop_other_events_and_is_retried(pg_session):
    # finding 5: an exception from on_qualifying_event for one event must
    # not (a) abort processing of other qualifying events/owners in the
    # same poll cycle, and (b) must not mark the failed event "seen" — a
    # follow-up call with the same event must fire the callback again.
    user = User(id=str(uuid.uuid4()), created_at=NOW)
    pg_session.add(user)
    pg_session.commit()

    events = [
        {
            "external_id": "evt-fails",
            "title": "Will fail",
            "starts_at": (NOW + datetime.timedelta(minutes=10)).isoformat(),
            "attendees": [],
        },
        {
            "external_id": "evt-ok",
            "title": "Will succeed",
            "starts_at": (NOW + datetime.timedelta(minutes=12)).isoformat(),
            "attendees": [],
        },
    ]
    calls: list[str] = []

    def _on_qualifying_event(report_user_id, event):
        calls.append(event["external_id"])
        if event["external_id"] == "evt-fails":
            raise RuntimeError("transient LLM failure")

    def session_factory():
        return pg_session

    poll_dossier_window_once(
        session_factory,
        [user.id],
        clock=FrozenClock(at=NOW),
        calendar_client_factory=_factory_for(events),
        on_qualifying_event=_on_qualifying_event,
    )

    # (a) the failure on evt-fails didn't stop evt-ok from being processed.
    assert calls == ["evt-fails", "evt-ok"]

    # (b) evt-fails was NOT marked seen -- a follow-up poll fires it again.
    poll_dossier_window_once(
        session_factory,
        [user.id],
        clock=FrozenClock(at=NOW),
        calendar_client_factory=_factory_for(events),
        on_qualifying_event=_on_qualifying_event,
    )
    assert calls == ["evt-fails", "evt-ok", "evt-fails"]
