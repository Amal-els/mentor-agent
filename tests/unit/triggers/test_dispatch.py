import datetime

from app.core.models import PulseDelivery, WorkItem
from app.core.scope import OwnerScope
from app.delivery.cards import build_card, card_to_dict
from app.ingest.fixture_source import (
    FixtureCalendarClient,
    FixtureLinearClient,
    FixtureSlackClient,
    load_day_fixture,
)
from app.ingest.seed import seed_fixture
from app.salience.pulse.checklist import record_completion
from app.triggers.dispatch import handle_trigger
from app.triggers.pulse_trigger import build_cron_trigger, build_pull_trigger


def _clients(fixture_name):
    fixture = load_day_fixture(fixture_name)
    return [
        FixtureCalendarClient(fixture),
        FixtureSlackClient(fixture),
        FixtureLinearClient(fixture),
    ]


def _clock_for(fixture_name):
    from app.core.clock import FrozenClock

    fixture = load_day_fixture(fixture_name)
    return FrozenClock(at=datetime.datetime.fromisoformat(fixture["now"]))


def test_first_cron_fire_delivers_and_persists(pg_session):
    counts = seed_fixture(pg_session, "normal_day")
    scope = OwnerScope(owner_user_id=counts["owner_user_id"], session=pg_session)
    clock = _clock_for("normal_day")
    event = build_cron_trigger(scope.owner_user_id, clock.now())

    result = handle_trigger(scope, clock, _clients("normal_day"), event)

    assert result.delivered is True
    assert result.idempotent_skip is False
    assert result.rendered is not None

    rows = pg_session.execute(scope.query(PulseDelivery)).scalars().all()
    assert len(rows) == 1
    assert rows[0].trigger == "cron"
    assert rows[0].local_date == result.local_date
    assert result.delivery_id == rows[0].id


def test_retried_cron_fire_is_idempotent(pg_session):
    counts = seed_fixture(pg_session, "normal_day")
    scope = OwnerScope(owner_user_id=counts["owner_user_id"], session=pg_session)
    clock = _clock_for("normal_day")
    event = build_cron_trigger(scope.owner_user_id, clock.now())

    first = handle_trigger(scope, clock, _clients("normal_day"), event)
    second = handle_trigger(scope, clock, _clients("normal_day"), event)

    assert first.delivered is True
    assert second.delivered is False
    assert second.idempotent_skip is True
    assert second.rendered is None

    rows = pg_session.execute(scope.query(PulseDelivery)).scalars().all()
    assert len(rows) == 1


def test_cron_and_pull_are_independently_idempotent(pg_session):
    counts = seed_fixture(pg_session, "normal_day")
    scope = OwnerScope(owner_user_id=counts["owner_user_id"], session=pg_session)
    clock = _clock_for("normal_day")

    cron_event = build_cron_trigger(scope.owner_user_id, clock.now())
    pull_event = build_pull_trigger(
        scope.owner_user_id, scope.owner_user_id, clock.now()
    )

    cron_result = handle_trigger(scope, clock, _clients("normal_day"), cron_event)
    pull_result = handle_trigger(scope, clock, _clients("normal_day"), pull_event)

    assert cron_result.delivered is True
    assert pull_result.delivered is True  # different trigger, not deduped together

    rows = pg_session.execute(scope.query(PulseDelivery)).scalars().all()
    assert len(rows) == 2


def test_second_pull_same_day_is_not_idempotent_skipped(pg_session):
    # pulse.md's edge case: "Called twice in one day | Second call is fine
    # (pull)." — unlike cron, pull is user-initiated each time and must
    # never be silently blocked. (M6 will render it as a delta; M5 just
    # must not refuse it.)
    counts = seed_fixture(pg_session, "normal_day")
    scope = OwnerScope(owner_user_id=counts["owner_user_id"], session=pg_session)
    clock = _clock_for("normal_day")
    event = build_pull_trigger(scope.owner_user_id, scope.owner_user_id, clock.now())

    first = handle_trigger(scope, clock, _clients("normal_day"), event)
    second = handle_trigger(scope, clock, _clients("normal_day"), event)

    assert first.delivered is True
    assert second.delivered is True
    assert second.idempotent_skip is False
    assert second.rendered is not None

    # still only one delivery row for the day — the second pull updates it
    # in place rather than violating the unique constraint or duplicating
    rows = pg_session.execute(scope.query(PulseDelivery)).scalars().all()
    assert len(rows) == 1
    assert first.delivery_id == second.delivery_id == rows[0].id


def test_checked_off_item_is_absent_from_a_new_pulse(pg_session):
    """Manual checklist completion suppresses the item before card render."""
    counts = seed_fixture(pg_session, "normal_day")
    scope = OwnerScope(owner_user_id=counts["owner_user_id"], session=pg_session)
    clock = _clock_for("normal_day")
    work_item = pg_session.execute(scope.query(WorkItem)).scalars().first()
    assert work_item is not None

    record_completion(
        scope,
        clock,
        "work_item",
        (work_item.source, work_item.external_id),
        "Done.",
    )
    result = handle_trigger(
        scope,
        clock,
        _clients("normal_day"),
        build_pull_trigger(scope.owner_user_id, scope.owner_user_id, clock.now()),
    )

    assert result.rendered is not None
    assert work_item.id not in {
        item.item_id for item in result.rendered.context.shortlist
    }
    assert result.rendered.context.shortlist


def test_first_delivery_of_the_day_has_no_previous_snapshot(pg_session):
    counts = seed_fixture(pg_session, "normal_day")
    scope = OwnerScope(owner_user_id=counts["owner_user_id"], session=pg_session)
    clock = _clock_for("normal_day")
    event = build_pull_trigger(scope.owner_user_id, scope.owner_user_id, clock.now())

    result = handle_trigger(scope, clock, _clients("normal_day"), event)

    assert result.previous_item_ids is None
    assert result.previous_delivered_at is None


def test_second_pull_carries_the_first_deliverys_snapshot(pg_session):
    counts = seed_fixture(pg_session, "normal_day")
    scope = OwnerScope(owner_user_id=counts["owner_user_id"], session=pg_session)
    clock = _clock_for("normal_day")
    event = build_pull_trigger(scope.owner_user_id, scope.owner_user_id, clock.now())

    first = handle_trigger(scope, clock, _clients("normal_day"), event)
    second = handle_trigger(scope, clock, _clients("normal_day"), event)

    assert second.previous_item_ids == first.rendered.ordered_item_ids
    assert second.previous_delivered_at == first.rendered.context.requested_at


def test_pulse_delivery_persists_card_json_matching_build_card(pg_session):
    counts = seed_fixture(pg_session, "normal_day")
    scope = OwnerScope(owner_user_id=counts["owner_user_id"], session=pg_session)
    clock = _clock_for("normal_day")
    event = build_cron_trigger(scope.owner_user_id, clock.now())

    result = handle_trigger(scope, clock, _clients("normal_day"), event)

    row = pg_session.execute(scope.query(PulseDelivery)).scalar_one()
    # build_card(result) here is the same call every real caller
    # (cron_scheduler.py/slack_command.py/etc.) makes to render Slack
    # blocks — this asserts dispatch.py's own internal snapshot (built
    # from a throwaway SimpleNamespace, not the real TriggerResult) landed
    # on the row with identical content, not just "something non-empty."
    assert row.card_json == card_to_dict(build_card(result))
    assert row.card_json["focus"]
    assert row.card_json["total_count"] == len(result.rendered.context.shortlist)


def test_second_pull_updates_card_json_in_place(pg_session):
    counts = seed_fixture(pg_session, "normal_day")
    scope = OwnerScope(owner_user_id=counts["owner_user_id"], session=pg_session)
    clock = _clock_for("normal_day")
    event = build_pull_trigger(scope.owner_user_id, scope.owner_user_id, clock.now())

    first = handle_trigger(scope, clock, _clients("normal_day"), event)
    second = handle_trigger(scope, clock, _clients("normal_day"), event)

    row = pg_session.execute(scope.query(PulseDelivery)).scalar_one()
    assert row.card_json == card_to_dict(build_card(second))
    # since_note reflects the first delivery's timestamp on the second
    # pull (build_card's own since_note logic) — confirms this is really
    # the SECOND render, not a stale copy of the first.
    assert row.card_json["since_note"] is not None
    assert card_to_dict(build_card(first))["since_note"] is None


def test_dry_run_never_persists_or_dedupes(pg_session):
    counts = seed_fixture(pg_session, "normal_day")
    scope = OwnerScope(owner_user_id=counts["owner_user_id"], session=pg_session)
    clock = _clock_for("normal_day")
    event = build_cron_trigger(scope.owner_user_id, clock.now())

    first = handle_trigger(scope, clock, _clients("normal_day"), event, dry_run=True)
    second = handle_trigger(scope, clock, _clients("normal_day"), event, dry_run=True)

    assert first.delivered is True
    assert second.delivered is True  # dry-run never sees a prior delivery
    assert second.idempotent_skip is False

    rows = pg_session.execute(scope.query(PulseDelivery)).scalars().all()
    assert rows == []
