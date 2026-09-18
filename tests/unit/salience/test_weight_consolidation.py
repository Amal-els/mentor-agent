import datetime
import uuid

from app.core.clock import FrozenClock
from app.core.models import FeedbackEvent, PulseDelivery, Weight
from app.salience.pulse.config import WEIGHT_CLAMP_MAX, WEIGHT_CLAMP_MIN, WEIGHT_NEUTRAL
from app.salience.pulse.weight_consolidation import consolidate_weights

NOW = datetime.datetime(2026, 8, 12, 3, 0, tzinfo=datetime.UTC)
CLOCK = FrozenClock(at=NOW)


def _delivery(scope):
    row = PulseDelivery(
        id=str(uuid.uuid4()),
        ritual="pulse",
        local_date=NOW.date(),
        trigger="pull",
        delivered_at=NOW,
        item_ids=[],
        context_hash="abc",
        prompt_version="pulse_ranker.v1",
    )
    scope.add(row)
    scope.commit()
    return row.id


def _event(scope, delivery_id, item_id, item_type, signal, consolidated_at=None):
    row = FeedbackEvent(
        id=str(uuid.uuid4()),
        pulse_delivery_id=delivery_id,
        item_id=item_id,
        item_type=item_type,
        signal=signal,
        created_at=NOW,
        consolidated_at=consolidated_at,
    )
    scope.add(row)
    scope.commit()
    return row


def _weight_for(scope, key):
    return scope.session.execute(
        scope.query(Weight).where(Weight.key == key)
    ).scalar_one_or_none()


def test_consolidate_creates_no_weight_for_an_owner_with_no_feedback(
    pg_session, make_scope
):
    # tests/unit/salience/conftest.py's pg_session is shared across the
    # whole test run (no per-test rollback — same pattern as
    # tests/unit/ingest/test_seed.py), so a prior test in this file may
    # already have unconsolidated events from other owners sitting in the
    # table; asserting a literal zero global count would be flaky. A fresh
    # owner_user_id has no rows of its own regardless of what else ran.
    scope = make_scope()

    consolidate_weights(scope.session, CLOCK)

    assert _weight_for(scope, "type:event") is None


def test_all_up_signals_push_the_weight_above_neutral(pg_session, make_scope):
    scope = make_scope()
    delivery_id = _delivery(scope)
    _event(scope, delivery_id, "evt-1", "event", "up")
    _event(scope, delivery_id, "evt-2", "event", "up")

    consolidate_weights(scope.session, CLOCK)

    row = _weight_for(scope, "type:event")
    assert row is not None
    assert row.value > WEIGHT_NEUTRAL
    assert row.n_signals == 2


def test_all_down_signals_push_the_weight_below_neutral(pg_session, make_scope):
    scope = make_scope()
    delivery_id = _delivery(scope)
    _event(scope, delivery_id, "evt-1", "event", "down")

    consolidate_weights(scope.session, CLOCK)

    row = _weight_for(scope, "type:event")
    assert row.value < WEIGHT_NEUTRAL


def test_equal_up_and_down_signals_cancel_out_to_neutral(pg_session, make_scope):
    scope = make_scope()
    delivery_id = _delivery(scope)
    _event(scope, delivery_id, "evt-1", "event", "up")
    _event(scope, delivery_id, "evt-2", "event", "down")

    consolidate_weights(scope.session, CLOCK)

    row = _weight_for(scope, "type:event")
    assert row.value == WEIGHT_NEUTRAL


def test_events_are_marked_consolidated(pg_session, make_scope):
    scope = make_scope()
    delivery_id = _delivery(scope)
    event = _event(scope, delivery_id, "evt-1", "event", "up")

    consolidate_weights(scope.session, CLOCK)

    scope.session.refresh(event)
    assert event.consolidated_at == NOW


def test_already_consolidated_events_are_ignored(pg_session, make_scope):
    scope = make_scope()
    delivery_id = _delivery(scope)
    _event(scope, delivery_id, "evt-1", "event", "up", consolidated_at=NOW)

    # a shared, non-rolled-back dev DB (see the noop test's comment) means
    # other unrelated events may exist — assert on this owner's own weight,
    # not the global result counts, which aren't scoped to this test.
    consolidate_weights(scope.session, CLOCK)

    assert _weight_for(scope, "type:event") is None


def test_a_second_run_does_not_double_count_the_first_batch(pg_session, make_scope):
    scope = make_scope()
    delivery_id = _delivery(scope)
    _event(scope, delivery_id, "evt-1", "event", "up")

    consolidate_weights(scope.session, CLOCK)
    first_value = _weight_for(scope, "type:event").value

    consolidate_weights(scope.session, CLOCK)  # no new events since

    assert _weight_for(scope, "type:event").value == first_value


def test_a_weight_with_more_history_moves_less_from_the_same_batch(
    pg_session, make_scope
):
    """AGENT.md L5: weight movement is 'gated by n_signals' — a fresh
    weight should swing more from one batch of feedback than one that
    already has a long history, since the new batch is folded in as a
    weighted average, not an overwrite."""
    fresh_scope = make_scope()
    seasoned_scope = make_scope()

    fresh_delivery = _delivery(fresh_scope)
    _event(fresh_scope, fresh_delivery, "evt-1", "event", "up")

    seasoned_delivery = _delivery(seasoned_scope)
    now = datetime.datetime(2026, 8, 1, tzinfo=datetime.UTC)
    seasoned_scope.add(
        Weight(
            id=str(uuid.uuid4()),
            key="type:event",
            value=WEIGHT_NEUTRAL,
            n_signals=98,
            updated_at=now,
        )
    )
    seasoned_scope.commit()
    _event(seasoned_scope, seasoned_delivery, "evt-2", "event", "up")

    consolidate_weights(fresh_scope.session, CLOCK)
    consolidate_weights(seasoned_scope.session, CLOCK)

    fresh_delta = _weight_for(fresh_scope, "type:event").value - WEIGHT_NEUTRAL
    seasoned_delta = _weight_for(seasoned_scope, "type:event").value - WEIGHT_NEUTRAL
    assert fresh_delta > seasoned_delta > 0


def test_weight_value_is_clamped_to_configured_bounds(pg_session, make_scope):
    scope = make_scope()
    delivery_id = _delivery(scope)
    for i in range(20):
        _event(scope, delivery_id, f"evt-{i}", "event", "up")

    consolidate_weights(scope.session, CLOCK)

    row = _weight_for(scope, "type:event")
    assert WEIGHT_CLAMP_MIN <= row.value <= WEIGHT_CLAMP_MAX


def test_different_item_types_update_separate_weight_keys(pg_session, make_scope):
    scope = make_scope()
    delivery_id = _delivery(scope)
    _event(scope, delivery_id, "evt-1", "event", "up")
    _event(scope, delivery_id, "wi-1", "work_item", "down")

    consolidate_weights(scope.session, CLOCK)

    assert _weight_for(scope, "type:event").value > WEIGHT_NEUTRAL
    assert _weight_for(scope, "type:work_item").value < WEIGHT_NEUTRAL


def test_different_owners_update_separate_weight_rows(pg_session, make_scope):
    scope_a = make_scope()
    scope_b = make_scope()
    delivery_a = _delivery(scope_a)
    _event(scope_a, delivery_a, "evt-1", "event", "up")

    consolidate_weights(scope_a.session, CLOCK)

    assert _weight_for(scope_a, "type:event") is not None
    assert _weight_for(scope_b, "type:event") is None
