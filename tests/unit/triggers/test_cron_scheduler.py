import datetime
import pickle

import pytest

from app.core.clock import FrozenClock
from app.core.models import GoogleCredential, User
from app.triggers import cron_scheduler
from app.triggers.cron_scheduler import (
    _get_scheduled_owner_ids,
    _maybe_consolidate_weights,
    _poll_once,
    _should_fire,
    _should_run_nightly_consolidation,
    run_cron_pulse_for_user,
)


def test_should_fire_once_local_time_reaches_the_fire_time():
    fire_time = datetime.time(8, 30)

    assert _should_fire(datetime.time(8, 29), fire_time) is False
    assert _should_fire(datetime.time(8, 30), fire_time) is True
    assert _should_fire(datetime.time(14, 0), fire_time) is True


class _FakeUser:
    def __init__(self, id, slack_user_id, tz="UTC"):
        self.id = id
        self.slack_user_id = slack_user_id
        self.tz = tz


class _FakeTriggerResult:
    def __init__(self, idempotent_skip):
        self.idempotent_skip = idempotent_skip
        self.rendered = object() if not idempotent_skip else None


def test_run_cron_pulse_for_user_seeds_then_delivers(monkeypatch):
    calls = []

    monkeypatch.setattr(
        "app.triggers.cron_scheduler.seed_live",
        lambda session, owner_id, clock=None: calls.append(("seed_live", owner_id)),
    )

    def fake_handle_trigger(scope, clock, source_clients, event, dry_run=False):
        calls.append(("handle_trigger", event.trigger, event.owner_user_id))
        return _FakeTriggerResult(idempotent_skip=False)

    monkeypatch.setattr(
        "app.triggers.cron_scheduler.handle_trigger", fake_handle_trigger
    )
    monkeypatch.setattr(
        "app.triggers.cron_scheduler.build_card", lambda trigger_result: "card"
    )
    monkeypatch.setattr(
        "app.triggers.cron_scheduler.render_blocks", lambda card: ["block"]
    )

    class _FakeDeliverer:
        def __init__(self):
            self.enabled = True

        def deliver(self, slack_user_id, blocks, channel_id=None):
            calls.append(("deliver", slack_user_id, channel_id))
            return {"sent": True}

    monkeypatch.setattr("app.triggers.cron_scheduler.SlackDeliverer", _FakeDeliverer)

    class _FakeSession:
        def get(self, model, owner_id):
            if model is GoogleCredential:
                return None
            return _FakeUser(owner_id, slack_user_id="U0CRON1")

    clock = FrozenClock(at=datetime.datetime(2026, 8, 10, 8, 30, 0))
    run_cron_pulse_for_user(_FakeSession(), "usr_cron1", clock=clock)

    assert ("seed_live", "usr_cron1") in calls
    assert ("handle_trigger", "cron", "usr_cron1") in calls
    assert ("deliver", "U0CRON1", None) in calls


def test_run_cron_pulse_for_user_delivers_audio_threaded_under_the_card(monkeypatch):
    monkeypatch.setattr(
        "app.triggers.cron_scheduler.seed_live",
        lambda session, owner_id, clock=None: None,
    )
    monkeypatch.setattr(
        "app.triggers.cron_scheduler.handle_trigger",
        lambda scope, clock, source_clients, event, dry_run=False: _FakeTriggerResult(
            idempotent_skip=False
        ),
    )
    monkeypatch.setattr(
        "app.triggers.cron_scheduler.build_card", lambda trigger_result: "card"
    )
    monkeypatch.setattr(
        "app.triggers.cron_scheduler.render_blocks", lambda card: ["block"]
    )

    class _FakeDeliverer:
        def __init__(self):
            self.enabled = True

        def deliver(self, slack_user_id, blocks, channel_id=None):
            return {"sent": True, "dm_ts": "777.333", "dm_channel": "D0CRONAUDIO"}

    monkeypatch.setattr("app.triggers.cron_scheduler.SlackDeliverer", _FakeDeliverer)

    audio_calls = []
    monkeypatch.setattr(
        "app.triggers.cron_scheduler.deliver_pulse_audio",
        lambda deliverer, card, channel_id, thread_ts: audio_calls.append(
            (channel_id, thread_ts)
        ),
    )

    class _FakeSession:
        def get(self, model, owner_id):
            if model is GoogleCredential:
                return None
            return _FakeUser(owner_id, slack_user_id="U0CRONAUDIO")

    clock = FrozenClock(at=datetime.datetime(2026, 8, 10, 8, 30, 0))
    run_cron_pulse_for_user(_FakeSession(), "usr_cron_audio", clock=clock)

    assert audio_calls == [("D0CRONAUDIO", "777.333")]


def test_run_cron_pulse_for_user_skips_delivery_when_already_delivered(monkeypatch):
    monkeypatch.setattr(
        "app.triggers.cron_scheduler.seed_live",
        lambda session, owner_id, clock=None: None,
    )
    monkeypatch.setattr(
        "app.triggers.cron_scheduler.handle_trigger",
        lambda scope, clock, source_clients, event, dry_run=False: _FakeTriggerResult(
            idempotent_skip=True
        ),
    )

    class _FakeDeliverer:
        def __init__(self):
            self.enabled = True

        def deliver(self, *args, **kwargs):
            raise AssertionError("must not deliver on an idempotent skip")

    monkeypatch.setattr("app.triggers.cron_scheduler.SlackDeliverer", _FakeDeliverer)

    class _FakeSession:
        def get(self, model, owner_id):
            if model is GoogleCredential:
                return None
            return _FakeUser(owner_id, slack_user_id="U0CRON2")

    clock = FrozenClock(at=datetime.datetime(2026, 8, 10, 8, 30, 0))
    run_cron_pulse_for_user(_FakeSession(), "usr_cron2", clock=clock)  # must not raise


def test_run_cron_pulse_for_user_skips_delivery_when_slack_not_configured(
    monkeypatch,
):
    monkeypatch.setattr(
        "app.triggers.cron_scheduler.seed_live",
        lambda session, owner_id, clock=None: None,
    )
    monkeypatch.setattr(
        "app.triggers.cron_scheduler.handle_trigger",
        lambda scope, clock, source_clients, event, dry_run=False: _FakeTriggerResult(
            idempotent_skip=False
        ),
    )
    monkeypatch.setattr(
        "app.triggers.cron_scheduler.build_card", lambda trigger_result: "card"
    )
    monkeypatch.setattr(
        "app.triggers.cron_scheduler.render_blocks", lambda card: ["block"]
    )

    class _DisabledDeliverer:
        def __init__(self):
            self.enabled = False

        def deliver(self, *args, **kwargs):
            raise AssertionError("must not deliver when disabled")

    monkeypatch.setattr(
        "app.triggers.cron_scheduler.SlackDeliverer", _DisabledDeliverer
    )

    class _FakeSession:
        def get(self, model, owner_id):
            if model is GoogleCredential:
                return None
            return _FakeUser(owner_id, slack_user_id="U0CRON3")

    clock = FrozenClock(at=datetime.datetime(2026, 8, 10, 8, 30, 0))
    run_cron_pulse_for_user(_FakeSession(), "usr_cron3", clock=clock)  # must not raise


def test_poll_once_only_processes_users_in_the_allowlist(
    pg_session, ensure_test_user, monkeypatch
):
    # a live run caught this for real: without an explicit allowlist,
    # _poll_once processed *every* User row with a slack_user_id set —
    # including leftover test-fixture users sharing this same dev
    # database, about to fire a real Slack post for a fake id.
    ensure_test_user("usr_allow", slack_user_id="U0ALLOW")
    ensure_test_user("usr_not_allowed", slack_user_id="U0NOTALLOWED")
    for uid in ("usr_allow", "usr_not_allowed"):
        user = pg_session.get(User, uid)
        user.pulse_fire_time_local = datetime.time(0, 0)  # always already "reached"
    pg_session.commit()

    processed = []
    monkeypatch.setattr(
        "app.triggers.cron_scheduler.run_cron_pulse_for_user",
        lambda session, owner_id: processed.append(owner_id),
    )

    _poll_once(lambda: pg_session, owner_user_ids=["usr_allow"])

    assert processed == ["usr_allow"]


def test_poll_once_ignores_allowlisted_ids_with_no_linked_slack_user(
    pg_session, ensure_test_user, monkeypatch
):
    ensure_test_user("usr_unlinked", slack_user_id=None)
    user = pg_session.get(User, "usr_unlinked")
    user.pulse_fire_time_local = datetime.time(0, 0)
    pg_session.commit()

    processed = []
    monkeypatch.setattr(
        "app.triggers.cron_scheduler.run_cron_pulse_for_user",
        lambda session, owner_id: processed.append(owner_id),
    )

    _poll_once(lambda: pg_session, owner_user_ids=["usr_unlinked"])

    assert processed == []


def test_persisted_scheduler_job_arguments_are_pickleable():
    """The SQLAlchemy job store pickles job state at scheduler.start()."""
    pickle.dumps((cron_scheduler._poll_once_job, ["usr_allow"]))
    pickle.dumps((cron_scheduler._run_consolidation_job, ()))


def test_get_scheduled_owner_ids_refuses_when_unset(monkeypatch):
    # deliberately does NOT call main() — main() calls load_dotenv() (which
    # would silently re-populate this from the real .env, defeating the
    # test) and then an infinite `while True` loop. A live run of this
    # exact test hung the entire suite for good by calling main() directly
    # for precisely that reason. _get_scheduled_owner_ids() is the pure,
    # side-effect-free piece worth testing in isolation.
    monkeypatch.delenv("PULSE_SCHEDULED_OWNER_IDS", raising=False)

    with pytest.raises(RuntimeError, match="PULSE_SCHEDULED_OWNER_IDS"):
        _get_scheduled_owner_ids()


def test_get_scheduled_owner_ids_parses_a_comma_separated_list(monkeypatch):
    monkeypatch.setenv("PULSE_SCHEDULED_OWNER_IDS", "usr_a, usr_b ,usr_c")

    assert _get_scheduled_owner_ids() == ["usr_a", "usr_b", "usr_c"]


def test_should_run_nightly_consolidation_before_0300_is_false():
    now = datetime.datetime(2026, 8, 12, 2, 59)

    assert _should_run_nightly_consolidation(now, last_run_date=None) is False


def test_should_run_nightly_consolidation_at_or_after_0300_is_true_if_not_run_today():
    now = datetime.datetime(2026, 8, 12, 3, 0)

    assert _should_run_nightly_consolidation(now, last_run_date=None) is True
    assert (
        _should_run_nightly_consolidation(now, last_run_date=datetime.date(2026, 8, 11))
        is True
    )


def test_should_run_nightly_consolidation_is_false_if_already_run_today():
    now = datetime.datetime(2026, 8, 12, 5, 0)

    assert (
        _should_run_nightly_consolidation(now, last_run_date=datetime.date(2026, 8, 12))
        is False
    )


def test_maybe_consolidate_weights_skips_before_0300(monkeypatch):
    monkeypatch.setattr(cron_scheduler, "_last_consolidation_date", None)
    called = []
    monkeypatch.setattr(
        "app.triggers.cron_scheduler.consolidate_weights",
        lambda session, clock: called.append(True),
    )
    clock = FrozenClock(at=datetime.datetime(2026, 8, 12, 2, 0))

    _maybe_consolidate_weights(lambda: object(), clock)

    assert called == []


def test_maybe_consolidate_weights_runs_once_after_0300_and_updates_last_run_date(
    monkeypatch,
):
    monkeypatch.setattr(cron_scheduler, "_last_consolidation_date", None)
    called = []
    monkeypatch.setattr(
        "app.triggers.cron_scheduler.consolidate_weights",
        lambda session, clock: called.append(True)
        or {"events_consolidated": 0, "weights_updated": 0},
    )

    class _FakeSession:
        def close(self):
            pass

    clock = FrozenClock(at=datetime.datetime(2026, 8, 12, 3, 30))

    _maybe_consolidate_weights(lambda: _FakeSession(), clock)
    _maybe_consolidate_weights(lambda: _FakeSession(), clock)  # same day, must not rerun

    assert len(called) == 1
    assert cron_scheduler._last_consolidation_date == datetime.date(2026, 8, 12)


def test_maybe_consolidate_weights_does_not_advance_last_run_date_on_failure(
    monkeypatch,
):
    monkeypatch.setattr(cron_scheduler, "_last_consolidation_date", None)

    def _boom(session, clock):
        raise RuntimeError("db down")

    monkeypatch.setattr("app.triggers.cron_scheduler.consolidate_weights", _boom)

    class _FakeSession:
        def close(self):
            pass

    clock = FrozenClock(at=datetime.datetime(2026, 8, 12, 3, 30))

    _maybe_consolidate_weights(lambda: _FakeSession(), clock)  # must not raise

    assert cron_scheduler._last_consolidation_date is None
