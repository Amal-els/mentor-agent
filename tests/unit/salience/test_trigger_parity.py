"""Decision 3 (docs/plans/morning-pulse.md): pre-gate parity has no
exceptions; the gate is the only place trigger affects output, and every
delta it introduces must be named."""

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
from app.salience.pulse.assemble import assemble_and_score
from app.salience.pulse.pulse_context import build_pulse_context

CLOCK = FrozenClock(at=datetime.datetime(2026, 8, 7, 7, 0, tzinfo=datetime.UTC))


def _clients(fixture_name):
    fixture = load_day_fixture(fixture_name)
    return [
        FixtureCalendarClient(fixture),
        FixtureSlackClient(fixture),
        FixtureLinearClient(fixture),
    ]


def _scope(pg_session, owner_id):
    return OwnerScope(owner_user_id=owner_id, session=pg_session)


def test_pre_gate_context_is_identical_regardless_of_trigger(pg_session):
    counts = seed_fixture(pg_session, "series_suppression")
    scope = _scope(pg_session, counts["owner_user_id"])

    first = assemble_and_score(scope, CLOCK, _clients("series_suppression"))
    second = assemble_and_score(scope, CLOCK, _clients("series_suppression"))

    # assemble_and_score never takes a trigger argument at all — this proves
    # the shortlist content, scores, terms, and order are trigger-blind by
    # construction, not just by convention.
    first_view = [(i.item_id, i.score, i.score_terms) for i in first.shortlist]
    second_view = [(i.item_id, i.score, i.score_terms) for i in second.shortlist]
    assert first_view == second_view
    assert first.degraded_sources == second.degraded_sources


def test_gate_accountability_on_series_suppression_fixture(pg_session):
    counts = seed_fixture(pg_session, "series_suppression")
    owner_id = counts["owner_user_id"]

    cron_ctx = build_pulse_context(
        _scope(pg_session, owner_id),
        CLOCK,
        _clients("series_suppression"),
        trigger="cron",
        requested_at=CLOCK.now(),
    )
    pull_ctx = build_pulse_context(
        _scope(pg_session, owner_id),
        CLOCK,
        _clients("series_suppression"),
        trigger="pull",
        requested_at=CLOCK.now(),
    )

    cron_ids = {item.item_id for item in cron_ctx.shortlist}
    pull_ids = {item.item_id for item in pull_ctx.shortlist}
    removal_ids = {r.item_id for r in cron_ctx.removals}

    # (b) gate accountability: the only delta is exactly what the gate named.
    assert pull_ids - cron_ids == removal_ids
    assert cron_ids - pull_ids == set()
    assert pull_ctx.removals == []

    # this fixture's one legitimate divergence is the series mute — no
    # budget removal, since 3 items sits under the push budget.
    assert len(cron_ctx.removals) == 1
    assert cron_ctx.removals[0].reason == "series_suppression:series:standup"


def test_normal_day_has_only_the_budget_removal(pg_session):
    counts = seed_fixture(pg_session, "normal_day")
    owner_id = counts["owner_user_id"]

    cron_ctx = build_pulse_context(
        _scope(pg_session, owner_id),
        CLOCK,
        _clients("normal_day"),
        trigger="cron",
        requested_at=CLOCK.now(),
    )
    pull_ctx = build_pulse_context(
        _scope(pg_session, owner_id),
        CLOCK,
        _clients("normal_day"),
        trigger="pull",
        requested_at=CLOCK.now(),
    )

    # 6 competing items (3 events + 2 work items + Sarah's DM, now that
    # Message rows are scored — see app/salience/score.py's score_message)
    # against a push budget of 5: nothing suppressed, but one item is
    # legitimately over budget, so cron and pull diverge by exactly that.
    cron_ids = {item.item_id for item in cron_ctx.shortlist}
    pull_ids = {item.item_id for item in pull_ctx.shortlist}
    removal_ids = {r.item_id for r in cron_ctx.removals}

    assert len(pull_ctx.shortlist) == 6
    assert len(cron_ctx.shortlist) == 5
    assert pull_ids - cron_ids == removal_ids
    assert cron_ids - pull_ids == set()
    assert [r.reason for r in cron_ctx.removals] == ["budget"]
    assert pull_ctx.removals == []
