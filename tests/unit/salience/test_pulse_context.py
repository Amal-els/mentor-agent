import datetime

from app.core.clock import FrozenClock
from app.core.scope import OwnerScope
from app.ingest.fixture_source import (
    FixtureCalendarClient,
    FixtureLinearClient,
    FixtureSlackClient,
    load_day_fixture,
)
from app.ingest.seed import seed_fixture
from app.salience.pulse.pulse_context import build_pulse_context


def _clients(fixture_name):
    fixture = load_day_fixture(fixture_name)
    return [
        FixtureCalendarClient(fixture),
        FixtureSlackClient(fixture),
        FixtureLinearClient(fixture),
    ]


def test_shortlist_limit_unset_is_uncapped_by_default(pg_session):
    """No pre-ranker cap anymore — every scored candidate reaches L6, not
    a pre-cut top N (see app/salience/config.py's note on the removal)."""
    counts = seed_fixture(pg_session, "normal_day")
    scope = OwnerScope(owner_user_id=counts["owner_user_id"], session=pg_session)
    clock = FrozenClock(at=datetime.datetime(2026, 8, 7, 7, 0, tzinfo=datetime.UTC))

    ctx = build_pulse_context(
        scope, clock, _clients("normal_day"), trigger="pull", requested_at=clock.now()
    )

    assert len(ctx.shortlist) == 6  # normal_day's full candidate count


def test_shortlist_limit_explicit_int_is_still_honored(pg_session):
    """shortlist_limit stays a real pass-through param, for callers that
    do want a bounded view."""
    counts = seed_fixture(pg_session, "normal_day")
    scope = OwnerScope(owner_user_id=counts["owner_user_id"], session=pg_session)
    clock = FrozenClock(at=datetime.datetime(2026, 8, 7, 7, 0, tzinfo=datetime.UTC))

    ctx = build_pulse_context(
        scope,
        clock,
        _clients("normal_day"),
        trigger="pull",
        requested_at=clock.now(),
        shortlist_limit=2,
    )

    assert len(ctx.shortlist) == 2
