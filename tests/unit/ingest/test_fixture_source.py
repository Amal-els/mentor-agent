import datetime

from app.ingest.base import Error, Healthy, Unauthorized, Window
from app.ingest.fixture_source import (
    FixtureCalendarClient,
    FixtureJiraClient,
    FixtureLinearClient,
    FixtureSlackClient,
    load_day_fixture,
)

FULL_DAY_WINDOW = Window(
    start=datetime.datetime.fromisoformat("2026-08-07T00:00:00+02:00"),
    end=datetime.datetime.fromisoformat("2026-08-07T23:59:59+02:00"),
)


def test_load_day_fixture_reads_normal_day():
    fixture = load_day_fixture("normal_day")
    assert fixture["name"] == "normal_day"
    assert len(fixture["events"]) == 3


def test_calendar_client_returns_events_in_window():
    fixture = load_day_fixture("normal_day")
    client = FixtureCalendarClient(fixture)

    rows = client.fetch(FULL_DAY_WINDOW, owner_user_id="usr_amal")

    assert {r["external_id"] for r in rows} == {"evt-1", "evt-2", "evt-3"}


def test_calendar_client_excludes_events_outside_window():
    fixture = load_day_fixture("normal_day")
    client = FixtureCalendarClient(fixture)
    narrow_window = Window(
        start=datetime.datetime.fromisoformat("2026-08-07T08:45:00+02:00"),
        end=datetime.datetime.fromisoformat("2026-08-07T09:15:00+02:00"),
    )

    rows = client.fetch(narrow_window, owner_user_id="usr_amal")

    assert {r["external_id"] for r in rows} == {"evt-1"}


def test_slack_client_returns_messages():
    fixture = load_day_fixture("normal_day")
    client = FixtureSlackClient(fixture)

    rows = client.fetch(FULL_DAY_WINDOW, owner_user_id="usr_amal")

    assert {r["external_id"] for r in rows} == {"1699999999.000100"}


def test_linear_client_returns_work_items_regardless_of_window():
    fixture = load_day_fixture("normal_day")
    client = FixtureLinearClient(fixture)
    narrow_window = Window(
        start=datetime.datetime.fromisoformat("2026-08-07T00:00:00+02:00"),
        end=datetime.datetime.fromisoformat("2026-08-07T00:01:00+02:00"),
    )

    rows = client.fetch(narrow_window, owner_user_id="usr_amal")

    assert {r["external_id"] for r in rows} == {"MENT-214", "MENT-201"}


def test_client_health_defaults_to_healthy():
    fixture = load_day_fixture("normal_day")
    assert FixtureCalendarClient(fixture).health() == Healthy()


def test_client_health_reports_unauthorized_from_fixture():
    fixture = load_day_fixture("degraded_source")
    assert FixtureCalendarClient(fixture).health() == Unauthorized()


def test_client_health_reports_error_with_stale_as_of():
    fixture = load_day_fixture("degraded_source")
    health = FixtureLinearClient(fixture).health()
    assert isinstance(health, Error)
    assert health.stale_as_of == datetime.datetime.fromisoformat(
        "2026-08-06T20:00:00+02:00"
    )


def test_jira_client_returns_only_jira_work_items():
    fixture = load_day_fixture("normal_day")
    client = FixtureJiraClient(fixture)

    rows = client.fetch(FULL_DAY_WINDOW, owner_user_id="usr_amal")

    assert rows == []


def test_linear_client_excludes_jira_work_items():
    fixture = {
        "work_items": [
            {"external_id": "MENT-1", "source": "linear", "title": "Linear item"},
            {"external_id": "JIR-1", "source": "jira", "title": "Jira item"},
        ]
    }

    linear_rows = FixtureLinearClient(fixture).fetch(
        FULL_DAY_WINDOW, owner_user_id="usr_amal"
    )
    jira_rows = FixtureJiraClient(fixture).fetch(
        FULL_DAY_WINDOW, owner_user_id="usr_amal"
    )

    assert {r["external_id"] for r in linear_rows} == {"MENT-1"}
    assert {r["external_id"] for r in jira_rows} == {"JIR-1"}
