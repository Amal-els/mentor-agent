import datetime
import uuid

from app.core.clock import FrozenClock
from app.core.models import Weight
from app.salience.pulse.weights import get_effective_weight


def test_missing_weight_defaults_to_neutral(make_scope):
    scope = make_scope()
    clock = FrozenClock(at=datetime.datetime(2026, 8, 7, tzinfo=datetime.UTC))

    assert get_effective_weight(scope, "type:event", clock) == 1.0


def test_fresh_weight_applies_in_full(pg_session, make_scope):
    scope = make_scope()
    now = datetime.datetime(2026, 8, 7, tzinfo=datetime.UTC)
    clock = FrozenClock(at=now)
    scope.add(Weight(id=str(uuid.uuid4()), key="type:event", value=2.0, updated_at=now))
    scope.commit()

    assert get_effective_weight(scope, "type:event", clock) == 2.0


def test_stale_weight_decays_toward_neutral(pg_session, make_scope):
    scope = make_scope()
    written_at = datetime.datetime(2026, 7, 1, tzinfo=datetime.UTC)
    now = datetime.datetime(2026, 7, 31, tzinfo=datetime.UTC)  # 30 days later
    clock = FrozenClock(at=now)
    scope.add(
        Weight(id=str(uuid.uuid4()), key="type:event", value=3.0, updated_at=written_at)
    )
    scope.commit()

    effective = get_effective_weight(scope, "type:event", clock)

    # fully decayed at the decay window boundary — back to neutral
    assert effective == 1.0


def test_weight_is_clamped_even_before_decay(pg_session, make_scope):
    scope = make_scope()
    now = datetime.datetime(2026, 8, 7, tzinfo=datetime.UTC)
    clock = FrozenClock(at=now)
    scope.add(Weight(id=str(uuid.uuid4()), key="type:event", value=9.0, updated_at=now))
    scope.commit()

    assert get_effective_weight(scope, "type:event", clock) == 3.0


def test_weight_is_scoped_per_owner(pg_session, make_scope):
    scope_a = make_scope()
    scope_b = make_scope()
    now = datetime.datetime(2026, 8, 7, tzinfo=datetime.UTC)
    clock = FrozenClock(at=now)
    scope_a.add(
        Weight(id=str(uuid.uuid4()), key="type:event", value=2.0, updated_at=now)
    )
    scope_a.commit()

    assert get_effective_weight(scope_b, "type:event", clock) == 1.0
