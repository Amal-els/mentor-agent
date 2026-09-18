import datetime

import pytest

from app.core.clock import FrozenClock
from app.core.models import (
    Commitment,
    Event,
    Message,
    PulseDelivery,
    Suppression,
    User,
    WorkItem,
)
from app.core.scope import OwnerScope
from app.ingest.base import Healthy, Unauthorized
from app.ingest.seed import seed_fixture, seed_live


def test_seed_normal_day_creates_expected_rows(pg_session):
    counts = seed_fixture(pg_session, "normal_day")

    assert counts["events"] == 3
    assert counts["work_items"] == 2
    assert counts["messages"] == 1
    assert counts["commitments"] == 1

    owner_id = counts["owner_user_id"]
    assert pg_session.get(User, owner_id) is not None

    scope = OwnerScope(owner_user_id=owner_id, session=pg_session)
    assert len(pg_session.execute(scope.query(Event)).scalars().all()) == 3
    assert len(pg_session.execute(scope.query(WorkItem)).scalars().all()) == 2
    assert len(pg_session.execute(scope.query(Message)).scalars().all()) == 1
    assert len(pg_session.execute(scope.query(Commitment)).scalars().all()) == 1


def test_seed_is_idempotent(pg_session):
    first = seed_fixture(pg_session, "normal_day")
    second = seed_fixture(pg_session, "normal_day")

    owner_id = first["owner_user_id"]
    scope = OwnerScope(owner_user_id=owner_id, session=pg_session)

    assert second["events"] == first["events"]
    assert len(pg_session.execute(scope.query(Event)).scalars().all()) == 3
    assert len(pg_session.execute(scope.query(Commitment)).scalars().all()) == 1


def test_seed_resolves_the_roster_actor(pg_session):
    counts = seed_fixture(pg_session, "normal_day")
    owner_id = counts["owner_user_id"]
    scope = OwnerScope(owner_user_id=owner_id, session=pg_session)

    evt1 = (
        pg_session.execute(scope.query(Event).where(Event.external_id == "evt-1"))
        .scalars()
        .one()
    )
    assert evt1.resolved_person_id is not None


def test_seed_clear_day_has_no_calendar_or_work_item_rows(pg_session):
    counts = seed_fixture(pg_session, "clear_day")

    assert counts["events"] == 0
    assert counts["work_items"] == 0
    assert counts["messages"] == 0
    assert counts["commitments"] == 1


def test_seed_skips_unhealthy_sources_and_reports_them(pg_session):
    counts = seed_fixture(pg_session, "degraded_source")

    # calendar is unauthorized and linear is stale in this fixture — neither
    # should be fetched even though the fixture *has* a work_items row.
    assert counts["work_items"] == 0
    assert counts["messages"] == 1
    assert {d["source"] for d in counts["degraded_sources"]} == {"calendar", "linear"}

    calendar_status = next(
        d for d in counts["degraded_sources"] if d["source"] == "calendar"
    )
    assert calendar_status["status"] == "unauthorized"

    linear_status = next(
        d for d in counts["degraded_sources"] if d["source"] == "linear"
    )
    assert linear_status["status"] == "error"
    assert linear_status["stale_as_of"] is not None


def test_seed_healthy_fixture_reports_no_degraded_sources(pg_session):
    counts = seed_fixture(pg_session, "normal_day")

    assert counts["degraded_sources"] == []


def test_reseeding_a_different_fixture_for_the_same_owner_does_not_accumulate(
    pg_session,
):
    # normal_day and series_suppression both use owner usr_amal — this is
    # the fixture-authoring reality (a single simulated user), so seed must
    # reset the owner's ingested rows on each call rather than merge fixtures
    # together. Otherwise a "clear day" seeded after a busy one wouldn't be
    # clear.
    seed_fixture(pg_session, "normal_day")
    counts = seed_fixture(pg_session, "clear_day")

    owner_id = counts["owner_user_id"]
    scope = OwnerScope(owner_user_id=owner_id, session=pg_session)

    assert counts["events"] == 0
    assert len(pg_session.execute(scope.query(Event)).scalars().all()) == 0
    assert len(pg_session.execute(scope.query(WorkItem)).scalars().all()) == 0
    # normal_day's commitment must not leak into clear_day's result either
    assert len(pg_session.execute(scope.query(Commitment)).scalars().all()) == 1


def test_seed_ingests_declared_suppressions(pg_session):
    counts = seed_fixture(pg_session, "series_suppression")
    scope = OwnerScope(owner_user_id=counts["owner_user_id"], session=pg_session)

    rows = pg_session.execute(scope.query(Suppression)).scalars().all()
    assert len(rows) == 1
    assert rows[0].scope == "series"
    assert rows[0].target_ref == "series:standup"


def test_reseeding_does_not_accumulate_suppressions(pg_session):
    seed_fixture(pg_session, "series_suppression")
    counts = seed_fixture(pg_session, "series_suppression")
    scope = OwnerScope(owner_user_id=counts["owner_user_id"], session=pg_session)

    rows = pg_session.execute(scope.query(Suppression)).scalars().all()
    assert len(rows) == 1


class _FakeLiveClient:
    def __init__(self, source, health, rows):
        self.source = source
        self._health = health
        self._rows = rows

    def health(self):
        return self._health

    def fetch(self, window, owner_user_id):
        return self._rows


def _ensure_test_user(session, user_id: str) -> None:
    # get-or-create, like seed_fixture._ensure_user — these tests share a
    # database across runs (no per-test rollback), so a fixed id must be
    # idempotent rather than a raw insert.
    user = session.get(User, user_id)
    if user is None:
        session.add(User(id=user_id, created_at=datetime.datetime.now(datetime.UTC)))
        session.commit()
    elif user.last_live_seed_at is not None:
        # Explicitly reset, not left alone — several tests here use a
        # FIXED FrozenClock instant (not tied to wall-clock "now"), so a
        # PRIOR run's real last_live_seed_at value (same fixed id, same
        # fixed instant) can land inside CALENDAR_SEED_TTL_SECONDS on a
        # later run even though that run's own seed_live call is really
        # the "first" one as far as the test itself is concerned.
        user.last_live_seed_at = None
        session.commit()


def test_seed_live_requires_an_existing_user(pg_session):
    # unlike seed_fixture there's no fixture to source
    # pulse_fire_time_local/tz/late_cutoff_local from — live seeding can only
    # update a user that already exists.
    with pytest.raises(ValueError):
        seed_live(pg_session, "does_not_exist")


def test_seed_live_ingests_from_live_clients_and_reports_degraded(
    pg_session, monkeypatch
):
    _ensure_test_user(pg_session, "usr_live")

    monkeypatch.setattr(
        "app.ingest.seed.calendar_client_for",
        lambda session, owner_user_id: _FakeLiveClient(
            "calendar",
            Healthy(),
            [
                {
                    "external_id": "evt-live-1",
                    "source": "calendar",
                    "title": "Live event",
                    "starts_at": "2026-08-08T09:00:00",
                    "ends_at": "2026-08-08T09:30:00",
                    "status": "confirmed",
                    "actor_reference_key": "calendar:x@example.com",
                }
            ],
        ),
    )
    monkeypatch.setattr(
        "app.ingest.seed.google_docs_client_for",
        lambda session, owner_user_id: _FakeLiveClient("google_docs", Unauthorized(), []),
    )
    monkeypatch.setattr(
        "app.ingest.seed.gmail_client_for",
        lambda session, owner_user_id: _FakeLiveClient("gmail", Unauthorized(), []),
    )

    clock = FrozenClock(at=datetime.datetime(2026, 8, 8, 12, 0, 0))
    counts = seed_live(pg_session, "usr_live", clock=clock)

    assert counts["events"] == 1
    assert counts["messages"] == 0
    assert counts["work_items"] == 0
    # goals/one_on_one_notes no longer come from seed_live at all — moved
    # to app.triggers.agenda.agenda_scheduler's own background sync job.
    assert "goals" not in counts
    assert "one_on_one_notes" not in counts
    assert {d["source"] for d in counts["degraded_sources"]} == {
        "google_docs",
        "gmail",
    }

    scope = OwnerScope(owner_user_id="usr_live", session=pg_session)
    assert len(pg_session.execute(scope.query(Event)).scalars().all()) == 1


def test_seed_live_catches_fetch_failures_as_degraded(pg_session, monkeypatch):
    _ensure_test_user(pg_session, "usr_live2")

    class _BoomCalendarClient:
        source = "calendar"

        def health(self):
            return Healthy()

        def fetch(self, window, owner_user_id):
            raise RuntimeError("calendar fetch failed: boom")

    monkeypatch.setattr(
        "app.ingest.seed.calendar_client_for",
        lambda session, owner_user_id: _BoomCalendarClient(),
    )
    monkeypatch.setattr(
        "app.ingest.seed.google_docs_client_for",
        lambda session, owner_user_id: _FakeLiveClient("google_docs", Unauthorized(), []),
    )
    monkeypatch.setattr(
        "app.ingest.seed.gmail_client_for",
        lambda session, owner_user_id: _FakeLiveClient("gmail", Unauthorized(), []),
    )

    counts = seed_live(pg_session, "usr_live2")

    calendar_status = next(
        d for d in counts["degraded_sources"] if d["source"] == "calendar"
    )
    assert calendar_status["status"] == "error"


def test_seed_live_does_not_duplicate_or_lose_events_across_repeated_calls(
    pg_session, monkeypatch
):
    _ensure_test_user(pg_session, "usr_live3")

    monkeypatch.setattr(
        "app.ingest.seed.calendar_client_for",
        lambda session, owner_user_id: _FakeLiveClient(
            "calendar",
            Healthy(),
            [
                {
                    "external_id": "evt-live-1",
                    "source": "calendar",
                    "title": "Live event",
                    "starts_at": "2026-08-08T09:00:00",
                    "ends_at": "2026-08-08T09:30:00",
                    "status": "confirmed",
                    "actor_reference_key": "calendar:x@example.com",
                }
            ],
        ),
    )
    monkeypatch.setattr(
        "app.ingest.seed.google_docs_client_for",
        lambda session, owner_user_id: _FakeLiveClient("google_docs", Unauthorized(), []),
    )
    monkeypatch.setattr(
        "app.ingest.seed.gmail_client_for",
        lambda session, owner_user_id: _FakeLiveClient("gmail", Unauthorized(), []),
    )

    # Same instant for both calls, deterministically — a real second call
    # this close together now falls inside CALENDAR_SEED_TTL_SECONDS
    # (see seed_live's own docstring), so Calendar itself is correctly
    # skipped the second time; what this test actually verifies is that
    # skipping doesn't lose or duplicate the row the first call already
    # ingested.
    # tz-aware, unlike some other FrozenClock uses in this file — real
    # callers always use SystemClock (always UTC-aware); a naive clock
    # here would work for the first call but blow up subtracting against
    # last_live_seed_at on the second, since Postgres always returns a
    # timestamptz column as aware regardless of what was written.
    clock = FrozenClock(at=datetime.datetime(2026, 8, 8, 12, 0, 0, tzinfo=datetime.UTC))
    first = seed_live(pg_session, "usr_live3", clock=clock)
    second = seed_live(pg_session, "usr_live3", clock=clock)

    assert first["events"] == 1
    assert first["calendar_skipped"] is False
    assert second["events"] == 0
    assert second["calendar_skipped"] is True
    scope = OwnerScope(owner_user_id="usr_live3", session=pg_session)
    assert len(pg_session.execute(scope.query(Event)).scalars().all()) == 1


def test_seed_live_does_not_wipe_webhook_fed_work_items(pg_session, monkeypatch):
    # GitHub/Linear/Jira are webhook-fed now (app/triggers/webhook_router.py),
    # not pulled here — seed_live must not delete their WorkItem rows, since
    # nothing in its own fetch_plan would ever refill them. A live run
    # caught the risk here: reusing seed_fixture's old reset-everything
    # behavior would wipe whatever the webhooks had already populated in
    # the background on every single /mentor pulse.
    _ensure_test_user(pg_session, "usr_live5")
    scope = OwnerScope(owner_user_id="usr_live5", session=pg_session)
    if pg_session.get(WorkItem, "wi-webhook-1") is None:
        scope.add(
            WorkItem(
                id="wi-webhook-1",
                source="github",
                external_id="acme/repo#1",
                actor_reference_key="github:amal",
                title="Fix flaky roster test",
                status="open",
            )
        )
        scope.commit()

    monkeypatch.setattr(
        "app.ingest.seed.calendar_client_for",
        lambda session, owner_user_id: _FakeLiveClient("calendar", Unauthorized(), []),
    )
    monkeypatch.setattr(
        "app.ingest.seed.google_docs_client_for",
        lambda session, owner_user_id: _FakeLiveClient("google_docs", Unauthorized(), []),
    )
    monkeypatch.setattr(
        "app.ingest.seed.gmail_client_for",
        lambda session, owner_user_id: _FakeLiveClient("gmail", Unauthorized(), []),
    )

    seed_live(pg_session, "usr_live5")

    rows = pg_session.execute(scope.query(WorkItem)).scalars().all()
    assert len(rows) == 1
    assert rows[0].id == "wi-webhook-1"


def test_seed_live_does_not_wipe_push_persisted_slack_messages(pg_session, monkeypatch):
    # Slack messages are pushed in real time now
    # (app/triggers/slack_socket_listener.py), not pulled here — seed_live
    # must not delete them, since fetch_plan no longer has a Slack source
    # to refill them. Found live: a pulse-alias DM triggered both the push
    # persist and a seed_live run from the same events_api envelope, and
    # seed_live's old reset-everything Message scope deleted the
    # just-persisted DM before it could ever be read back, with no pull
    # path left to restore it.
    _ensure_test_user(pg_session, "usr_live6")
    scope = OwnerScope(owner_user_id="usr_live6", session=pg_session)
    if pg_session.get(Message, "msg-webhook-1") is None:
        scope.add(
            Message(
                id="msg-webhook-1",
                source="slack",
                external_id="1700000000.000200",
                channel="D0DMCHANNEL",
                body_ref="slack://D0DMCHANNEL/1700000000.000200",
                actor_reference_key="slack:U1",
            )
        )
        scope.commit()

    monkeypatch.setattr(
        "app.ingest.seed.calendar_client_for",
        lambda session, owner_user_id: _FakeLiveClient("calendar", Unauthorized(), []),
    )
    monkeypatch.setattr(
        "app.ingest.seed.google_docs_client_for",
        lambda session, owner_user_id: _FakeLiveClient("google_docs", Unauthorized(), []),
    )
    monkeypatch.setattr(
        "app.ingest.seed.gmail_client_for",
        lambda session, owner_user_id: _FakeLiveClient("gmail", Unauthorized(), []),
    )

    seed_live(pg_session, "usr_live6")

    rows = pg_session.execute(scope.query(Message)).scalars().all()
    assert len(rows) == 1
    assert rows[0].id == "msg-webhook-1"


def test_seed_live_does_not_wipe_meeting_synthesized_commitments(pg_session, monkeypatch):
    """REAL, LIVE DATA-LOSS BUG FOUND AND FIXED (confirmed live): a
    Commitment written by meeting-synthesis's own LLM tool (or the
    ledger_event trigger, or manual "add to ledger") is a real,
    independent write path — nothing in seed_live's own fetch_plan ever
    populates Commitment, so the old unconditional reset before every
    single call just destroyed it with nothing left to refill it.
    Confirmed live: cron_scheduler's 60s poll wiped a just-written
    Commitment inside a minute, while the agenda's own "commitment_ledger"
    mirror item survived, left pointing at a source_link for a row that
    no longer existed."""
    _ensure_test_user(pg_session, "usr_live_commit")
    scope = OwnerScope(owner_user_id="usr_live_commit", session=pg_session)
    if pg_session.get(Commitment, "commit-from-synthesis-1") is None:
        scope.add(
            Commitment(
                id="commit-from-synthesis-1",
                description="Share the draft RFC by Thursday",
                source_reference_key="agenda:usr_live_commit",
                promised_at=datetime.datetime.now(datetime.UTC),
                status="open",
            )
        )
        scope.commit()

    monkeypatch.setattr(
        "app.ingest.seed.calendar_client_for",
        lambda session, owner_user_id: _FakeLiveClient("calendar", Unauthorized(), []),
    )
    monkeypatch.setattr(
        "app.ingest.seed.google_docs_client_for",
        lambda session, owner_user_id: _FakeLiveClient("google_docs", Unauthorized(), []),
    )
    monkeypatch.setattr(
        "app.ingest.seed.gmail_client_for",
        lambda session, owner_user_id: _FakeLiveClient("gmail", Unauthorized(), []),
    )

    seed_live(pg_session, "usr_live_commit")

    rows = pg_session.execute(scope.query(Commitment)).scalars().all()
    assert len(rows) == 1
    assert rows[0].id == "commit-from-synthesis-1"


def test_seed_live_does_not_wipe_a_snoozed_item_suppression(pg_session, monkeypatch):
    """Same fix, applied to Suppression: app/delivery/affordances.py's
    snooze_item writes a real, independent Suppression with a multi-day
    TTL meant to survive many pulse cycles — the old unconditional reset
    destroyed it on the very next seed_live call instead."""
    _ensure_test_user(pg_session, "usr_live_snooze")
    scope = OwnerScope(owner_user_id="usr_live_snooze", session=pg_session)
    now = datetime.datetime.now(datetime.UTC)
    if pg_session.get(Suppression, "suppress-1") is None:
        scope.add(
            Suppression(
                id="suppress-1",
                scope="instance",
                target_ref="item-123",
                reason="snoozed by user",
                created_at=now,
                expires_at=now + datetime.timedelta(days=7),
                created_by="user",
            )
        )
        scope.commit()

    monkeypatch.setattr(
        "app.ingest.seed.calendar_client_for",
        lambda session, owner_user_id: _FakeLiveClient("calendar", Unauthorized(), []),
    )
    monkeypatch.setattr(
        "app.ingest.seed.google_docs_client_for",
        lambda session, owner_user_id: _FakeLiveClient("google_docs", Unauthorized(), []),
    )
    monkeypatch.setattr(
        "app.ingest.seed.gmail_client_for",
        lambda session, owner_user_id: _FakeLiveClient("gmail", Unauthorized(), []),
    )

    seed_live(pg_session, "usr_live_snooze")

    rows = pg_session.execute(scope.query(Suppression)).scalars().all()
    assert len(rows) == 1
    assert rows[0].id == "suppress-1"


def test_seed_live_clears_stale_google_docs_messages_but_keeps_gmail_and_slack(
    pg_session, monkeypatch
):
    # Found live: after LiveGmailClient started filtering out newsletters,
    # a second seed_live still showed the old unfiltered gmail rows,
    # because Message as a whole was excluded from the reset (to protect
    # push-persisted Slack DMs — see the other test above) with nothing
    # left to ever delete a stale pull-sourced row. That's still true for
    # google_docs, which keeps the ordinary reset-then-refetch treatment
    # — but ONLY once its fetch actually succeeds (delete-after-success,
    # see seed_live's own docstring on the live OOM-crash bug this fixed)
    # — google_docs' fake client below is deliberately Healthy() with an
    # empty result, a genuine successful-but-empty fetch, not
    # Unauthorized/degraded, so this test exercises the real "no longer
    # returned by the live source" deletion path rather than accidentally
    # testing the (now different) degraded-source path. gmail is
    # DELIBERATELY different now (see seed_live's own docstring): it's
    # upsert-only, relying on its narrowed incremental fetch window to
    # naturally stop returning stale rows rather than a reset ever
    # deleting them outright — so a stale gmail row (like slack's
    # push-only rows) is expected to survive an empty fetch here, not get
    # wiped.
    _ensure_test_user(pg_session, "usr_live7")
    scope = OwnerScope(owner_user_id="usr_live7", session=pg_session)
    if pg_session.get(Message, "msg-stale-gmail") is None:
        scope.add(
            Message(
                id="msg-stale-gmail",
                source="gmail",
                external_id="stale-1",
                channel="inbox",
                body_ref="gmail://stale-1",
                actor_reference_key="gmail:newsletter@example.com",
            )
        )
    if pg_session.get(Message, "msg-stale-docs") is None:
        scope.add(
            Message(
                id="msg-stale-docs",
                source="google_docs",
                external_id="stale-doc-1",
                channel=None,
                body_ref="google_docs://stale-doc-1",
                actor_reference_key="google_docs:x@example.com",
            )
        )
    if pg_session.get(Message, "msg-pushed-slack") is None:
        scope.add(
            Message(
                id="msg-pushed-slack",
                source="slack",
                external_id="1700000000.000300",
                channel="D0DMCHANNEL2",
                body_ref="slack://D0DMCHANNEL2/1700000000.000300",
                actor_reference_key="slack:U2",
            )
        )
    scope.commit()

    monkeypatch.setattr(
        "app.ingest.seed.calendar_client_for",
        lambda session, owner_user_id: _FakeLiveClient("calendar", Unauthorized(), []),
    )
    monkeypatch.setattr(
        "app.ingest.seed.google_docs_client_for",
        lambda session, owner_user_id: _FakeLiveClient("google_docs", Healthy(), []),
    )
    monkeypatch.setattr(
        "app.ingest.seed.gmail_client_for",
        lambda session, owner_user_id: _FakeLiveClient("gmail", Healthy(), []),
    )

    seed_live(pg_session, "usr_live7")

    rows = pg_session.execute(scope.query(Message)).scalars().all()
    assert {r.id for r in rows} == {"msg-pushed-slack", "msg-stale-gmail"}


def test_seed_live_preserves_google_docs_messages_when_that_fetch_fails(
    pg_session, monkeypatch
):
    """REAL BUG FOUND AND FIXED (confirmed live): Google Docs' own MCP
    subprocess (a real Node process) genuinely crashed with an
    out-of-memory error in production. seed_live catches that cleanly
    (reported as a degraded source, not a crash) — but the OLD reset-
    before-fetch ordering had already deleted every existing Google Docs
    comment by the time the fetch failed, with nothing left to replace
    them. This is the regression test for the fix: a failed fetch must
    leave prior data in place, stale but not gone."""
    _ensure_test_user(pg_session, "usr_live_docs_boom")
    scope = OwnerScope(owner_user_id="usr_live_docs_boom", session=pg_session)
    if pg_session.get(Message, "msg-survives-docs-crash") is None:
        scope.add(
            Message(
                id="msg-survives-docs-crash",
                source="google_docs",
                external_id="doc-1",
                channel=None,
                body_ref="google_docs://doc-1",
                actor_reference_key="google_docs:x@example.com",
            )
        )
        scope.commit()

    class _BoomGoogleDocsClient:
        source = "google_docs"

        def health(self):
            return Healthy()

        def fetch(self, window, owner_user_id):
            raise RuntimeError("google_docs fetch failed: out of memory")

    monkeypatch.setattr(
        "app.ingest.seed.calendar_client_for",
        lambda session, owner_user_id: _FakeLiveClient("calendar", Unauthorized(), []),
    )
    monkeypatch.setattr(
        "app.ingest.seed.google_docs_client_for",
        lambda session, owner_user_id: _BoomGoogleDocsClient(),
    )
    monkeypatch.setattr(
        "app.ingest.seed.gmail_client_for",
        lambda session, owner_user_id: _FakeLiveClient("gmail", Unauthorized(), []),
    )

    counts = seed_live(pg_session, "usr_live_docs_boom")

    docs_status = next(
        d for d in counts["degraded_sources"] if d["source"] == "google_docs"
    )
    assert docs_status["status"] == "error"

    rows = pg_session.execute(scope.query(Message)).scalars().all()
    assert {r.id for r in rows} == {"msg-survives-docs-crash"}


def test_seed_live_preserves_events_when_calendar_fetch_fails(pg_session, monkeypatch):
    """Same fix, applied to Calendar/Event — a genuinely unhealthy or
    failing Calendar fetch must not delete today's already-known events;
    only a SUCCESSFUL refetch is allowed to replace them."""
    _ensure_test_user(pg_session, "usr_live_cal_boom")
    scope = OwnerScope(owner_user_id="usr_live_cal_boom", session=pg_session)
    if pg_session.get(Event, "evt-survives-cal-crash") is None:
        scope.add(
            Event(
                id="evt-survives-cal-crash",
                source="calendar",
                external_id="evt-1",
                title="Existing meeting",
                starts_at=datetime.datetime(2026, 8, 8, 9, 0, tzinfo=datetime.UTC),
                ends_at=datetime.datetime(2026, 8, 8, 9, 30, tzinfo=datetime.UTC),
                status="confirmed",
                actor_reference_key="calendar:x@example.com",
            )
        )
        scope.commit()

    class _BoomCalendarClient:
        source = "calendar"

        def health(self):
            return Healthy()

        def fetch(self, window, owner_user_id):
            raise RuntimeError("calendar fetch failed: boom")

    monkeypatch.setattr(
        "app.ingest.seed.calendar_client_for",
        lambda session, owner_user_id: _BoomCalendarClient(),
    )
    monkeypatch.setattr(
        "app.ingest.seed.google_docs_client_for",
        lambda session, owner_user_id: _FakeLiveClient("google_docs", Unauthorized(), []),
    )
    monkeypatch.setattr(
        "app.ingest.seed.gmail_client_for",
        lambda session, owner_user_id: _FakeLiveClient("gmail", Unauthorized(), []),
    )

    clock = FrozenClock(at=datetime.datetime(2026, 8, 8, 12, 0, 0))
    counts = seed_live(pg_session, "usr_live_cal_boom", clock=clock)

    calendar_status = next(
        d for d in counts["degraded_sources"] if d["source"] == "calendar"
    )
    assert calendar_status["status"] == "error"

    rows = pg_session.execute(scope.query(Event)).scalars().all()
    assert {r.id for r in rows} == {"evt-survives-cal-crash"}


def test_seed_live_does_not_wipe_an_existing_pulse_delivery(pg_session, monkeypatch):
    # a live run of app/triggers/cron_scheduler.py caught this for real:
    # seed_live() reused seed_fixture's _reset_owner_ingested_data(), which
    # deletes PulseDelivery — the cron idempotency record itself. Since
    # seed_live() runs at the top of every cron poll (every 60s), it wiped
    # its own "already delivered today" guard on every single poll,
    # causing the pipeline to re-run and redeliver a real Slack DM every
    # 60 seconds indefinitely, never actually respecting idempotency.
    if pg_session.get(User, "usr_live4") is None:
        pg_session.add(
            User(id="usr_live4", created_at=datetime.datetime.now(datetime.UTC))
        )
        pg_session.commit()

    scope = OwnerScope(owner_user_id="usr_live4", session=pg_session)
    existing = pg_session.get(PulseDelivery, "pd-1")
    if existing is None:
        scope.add(
            PulseDelivery(
                id="pd-1",
                ritual="pulse",
                local_date=datetime.date(2026, 8, 10),
                trigger="cron",
                delivered_at=datetime.datetime.now(datetime.UTC),
                item_ids=["evt-1"],
                context_hash="abc123",
                prompt_version="pulse_ranker.v1",
            )
        )
        scope.commit()

    monkeypatch.setattr(
        "app.ingest.seed.calendar_client_for",
        lambda session, owner_user_id: _FakeLiveClient("calendar", Unauthorized(), []),
    )
    monkeypatch.setattr(
        "app.ingest.seed.google_docs_client_for",
        lambda session, owner_user_id: _FakeLiveClient("google_docs", Unauthorized(), []),
    )
    monkeypatch.setattr(
        "app.ingest.seed.gmail_client_for",
        lambda session, owner_user_id: _FakeLiveClient("gmail", Unauthorized(), []),
    )

    seed_live(pg_session, "usr_live4")

    rows = pg_session.execute(scope.query(PulseDelivery)).scalars().all()
    assert len(rows) == 1
    assert rows[0].id == "pd-1"
