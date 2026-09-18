import datetime

from app.core.clock import FrozenClock, SystemClock


def test_frozen_clock_returns_fixed_time():
    fixed = datetime.datetime(2026, 8, 5, 8, 30, tzinfo=datetime.UTC)
    clock = FrozenClock(at=fixed)
    assert clock.now() == fixed
    assert clock.now() == fixed  # calling twice never advances


def test_system_clock_returns_utc_aware_time():
    clock = SystemClock()
    now = clock.now()
    assert now.tzinfo is not None
