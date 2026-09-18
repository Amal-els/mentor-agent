"""Fixture-backed SourceClient implementations (app/ingest/base.py). Never
touch the network — config selects these over a live client (see
app/ingest/config.py). Each fixture item is already close to canonical
shape (app/fixtures/pulse/README.md's contract); L3 normalize.py does the
rest of the mapping."""

import datetime
from pathlib import Path

import yaml

from app.ingest.base import Error, Healthy, Unauthorized, Window

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "pulse"


def load_day_fixture(name: str) -> dict:
    return yaml.safe_load((FIXTURES_DIR / f"{name}.yaml").read_text(encoding="utf-8"))


def _health_for(fixture: dict, source: str) -> Healthy | Unauthorized | Error:
    spec = fixture.get("unhealthy_sources", {}).get(source)
    if spec is None:
        return Healthy()
    if spec["status"] == "unauthorized":
        return Unauthorized()
    return Error(stale_as_of=datetime.datetime.fromisoformat(spec["stale_as_of"]))


def _in_window(timestamp: str | None, window: Window) -> bool:
    if timestamp is None:
        return True
    return window.start <= datetime.datetime.fromisoformat(timestamp) <= window.end


class FixtureCalendarClient:
    source = "calendar"

    def __init__(self, fixture: dict):
        self._fixture = fixture

    def fetch(self, window: Window, owner_user_id: str) -> list[dict]:
        return [
            event
            for event in self._fixture.get("events", [])
            if _in_window(event["starts_at"], window)
        ]

    def health(self):
        return _health_for(self._fixture, self.source)


class FixtureSlackClient:
    source = "slack"

    def __init__(self, fixture: dict):
        self._fixture = fixture

    def fetch(self, window: Window, owner_user_id: str) -> list[dict]:
        return [
            message
            for message in self._fixture.get("messages", [])
            if _in_window(message["sent_at"], window)
        ]

    def health(self):
        return _health_for(self._fixture, self.source)


class FixtureLinearClient:
    """Work items are status-driven, not time-windowed — a blocked item due
    next week still matters today. window is accepted (protocol shape) but
    not used to filter. Fixture work_items carry their own "source" field
    (per app/fixtures/pulse/README.md's contract) since the same list is
    shared across every tracker fixture client — filtered here so a fixture
    exercising both Linear and Jira doesn't double-count the other's rows."""

    source = "linear"

    def __init__(self, fixture: dict):
        self._fixture = fixture

    def fetch(self, window: Window, owner_user_id: str) -> list[dict]:
        return [
            wi
            for wi in self._fixture.get("work_items", [])
            if wi.get("source") == self.source
        ]

    def health(self):
        return _health_for(self._fixture, self.source)


class FixtureJiraClient:
    """Same status-driven, source-filtered pattern as FixtureLinearClient —
    see its docstring."""

    source = "jira"

    def __init__(self, fixture: dict):
        self._fixture = fixture

    def fetch(self, window: Window, owner_user_id: str) -> list[dict]:
        return [
            wi
            for wi in self._fixture.get("work_items", [])
            if wi.get("source") == self.source
        ]

    def health(self):
        return _health_for(self._fixture, self.source)


class FixtureGoalClient:
    """Objectives/Key Results/Career Goals are status-driven, not
    time-windowed (an active OKR due next quarter still matters today) —
    same pattern as FixtureLinearClient/FixtureJiraClient. Straight
    passthrough of the fixture's "goals" list, each item already shaped
    like LiveNotionGoalsClient's own adapter output (goal_type/
    parent_external_id/etc. — see app/fixtures/pulse/README.md)."""

    source = "notion"

    def __init__(self, fixture: dict):
        self._fixture = fixture

    def fetch(self, window: Window, owner_user_id: str) -> list[dict]:
        return self._fixture.get("goals", [])

    def health(self):
        return _health_for(self._fixture, self.source)


class FixtureOneOnOneNoteClient:
    """Same passthrough shape as FixtureGoalClient, for the "1:1 Notes"
    Notion db (kept a separate fixture list/client from goals — a meeting
    note is not a goal, see OneOnOneNote's own docstring)."""

    source = "notion"

    def __init__(self, fixture: dict):
        self._fixture = fixture

    def fetch(self, window: Window, owner_user_id: str) -> list[dict]:
        return self._fixture.get("one_on_one_notes", [])

    def health(self):
        return _health_for(self._fixture, self.source)
