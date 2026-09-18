import datetime

import pytest

from app.triggers.pulse_trigger import (
    CrossUserPulseRequestError,
    TriggerEvent,
    build_cron_trigger,
    build_pull_trigger,
)

NOW = datetime.datetime(2026, 8, 7, 8, 30, tzinfo=datetime.UTC)


def test_build_cron_trigger_shape():
    event = build_cron_trigger("usr_amal", NOW)

    assert event == TriggerEvent(
        kind="pulse",
        trigger="cron",
        owner_user_id="usr_amal",
        requested_at=NOW,
        params={},
    )


def test_build_pull_trigger_for_self_succeeds():
    event = build_pull_trigger("usr_amal", "usr_amal", NOW)

    assert event.trigger == "pull"
    assert event.owner_user_id == "usr_amal"
    assert event.params == {}


def test_build_pull_trigger_with_explicit_date():
    event = build_pull_trigger(
        "usr_amal", "usr_amal", NOW, date=datetime.date(2026, 8, 10)
    )

    assert event.params == {"date": "2026-08-10"}


def test_build_pull_trigger_for_another_user_refuses():
    with pytest.raises(CrossUserPulseRequestError):
        build_pull_trigger("usr_marc", "usr_amal", NOW)


def test_cross_user_refusal_has_no_grant_escape_hatch():
    # decision 4: no delegation mechanism exists at all — confirm there is
    # no optional "grant" or "override" parameter that bypasses the check
    import inspect

    params = inspect.signature(build_pull_trigger).parameters
    assert "grant" not in params
    assert "override" not in params
    assert "allow" not in params
